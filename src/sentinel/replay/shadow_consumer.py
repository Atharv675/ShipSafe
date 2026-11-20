"""Consumer B (shadow): a SEPARATE process scores every event with the `candidate` model.

    python -m sentinel.replay.shadow_consumer

- Its own consumer group, container and database login (`sentinel_shadow`, which can only
  write `shadow_predictions`): a crash, lag or bug here cannot touch live verdicts.
- Loads the candidate via the registry alias (same ModelManager as serving, other alias);
  until a candidate exists it waits without consuming, so nothing is skipped.
- Scores whole batches at once; rows are written with primary key (event_id, model_version)
  and ON CONFLICT DO NOTHING; offsets are committed only after the rows are stored.
"""

from __future__ import annotations

import signal
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import astuple, dataclass
from datetime import UTC, datetime
from typing import cast

import numpy as np

from sentinel.config import ShadowConsumerConfig
from sentinel.logging import get_logger
from sentinel.replay.events import FileEvent
from sentinel.replay.live_consumer import ConsumerLike
from sentinel.serving.models import ModelManager

log = get_logger(__name__)


@dataclass(frozen=True)
class ShadowRow:
    event_id: str
    model_version: str
    file_id: str
    tenant_id: str
    ring: str
    week: int
    feature_schema_version: str
    threshold: float
    score: float
    verdict: str
    ts: datetime


SHADOW_COLUMNS = tuple(ShadowRow.__dataclass_fields__)
INSERT_SHADOW = (
    f"INSERT INTO shadow_predictions ({', '.join(SHADOW_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(SHADOW_COLUMNS))}) ON CONFLICT DO NOTHING"
)
RowWriter = Callable[[Sequence[ShadowRow]], None]


def score_events(events: Sequence[FileEvent], models: ModelManager) -> tuple[list[ShadowRow], int]:
    """Vectorised scoring per feature schema; events no candidate serves are counted."""
    rows: list[ShadowRow] = []
    skipped = 0
    now = datetime.now(UTC)
    by_schema: dict[str, list[FileEvent]] = {}
    for ev in events:
        by_schema.setdefault(ev.feature_schema_version, []).append(ev)
    for schema, evs in by_schema.items():
        model = models.get(schema)
        if model is None or any(e.features.shape != (model.n_features,) for e in evs):
            skipped += len(evs)
            log.error("shadow_unscorable", schema=schema, events=len(evs))
            continue
        scores = model.score(np.stack([e.features for e in evs]))
        for e, s in zip(evs, scores, strict=True):
            rows.append(
                ShadowRow(
                    event_id=e.event_id,
                    model_version=model.label,
                    file_id=e.file_id,
                    tenant_id=e.tenant_id,
                    ring=e.ring,
                    week=e.week,
                    feature_schema_version=schema,
                    threshold=model.threshold,
                    score=float(s),
                    verdict="malicious" if s > model.threshold else "benign",
                    ts=now,
                )
            )
    return rows, skipped


def run_shadow(
    consumer: ConsumerLike,
    models: ModelManager,
    write: RowWriter,
    cfg: ShadowConsumerConfig,
    stop: threading.Event,
    max_batches: int | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Counter[str]:
    totals: Counter[str] = Counter()
    batches = 0
    last_refresh = clock()
    while not stop.is_set() and (max_batches is None or batches < max_batches):
        if not models.ready or clock() - last_refresh >= cfg.model_poll_seconds:
            models.refresh()
            last_refresh = clock()
            if not models.ready:
                totals["waiting_for_candidate"] += 1
                batches += 1 if max_batches is not None else 0
                stop.wait(cfg.model_poll_seconds)
                continue
        msgs = consumer.consume(num_messages=cfg.batch_size, timeout=cfg.poll_timeout_seconds)
        if not msgs:
            batches += 1 if max_batches is not None else 0
            continue
        events: list[FileEvent] = []
        for m in msgs:
            if m.error() is not None:
                totals["broker_error"] += 1
                continue
            try:
                events.append(FileEvent.decode(m.value() or b""))
            except Exception:
                totals["bad_event"] += 1
        rows, skipped = score_events(events, models)
        totals["skipped"] += skipped
        while True:  # store, THEN commit; retry the database, never drop
            try:
                if rows:
                    write(rows)
                break
            except Exception as exc:
                totals["db_retry"] += 1
                log.error("shadow_write_failed", rows=len(rows), error=type(exc).__name__)
                if stop.wait(cfg.db_retry_seconds):
                    return totals  # not committed: re-delivered after restart
        if any(m.error() is None for m in msgs):  # broker-only errors: nothing to commit
            consumer.commit(asynchronous=False)
        totals["scored"] += len(rows)
        batches += 1
    return totals


def postgres_row_writer(dsn: str) -> RowWriter:  # pragma: no cover - integration-tested
    import psycopg

    state: dict[str, psycopg.Connection[tuple[object, ...]] | None] = {"conn": None}

    def write(rows: Sequence[ShadowRow]) -> None:
        conn = state["conn"]
        try:
            if conn is None or conn.closed:
                conn = state["conn"] = psycopg.connect(dsn, connect_timeout=5)
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(INSERT_SHADOW, [astuple(r) for r in rows])
        except Exception:
            if conn is not None:
                conn.close()
            state["conn"] = None
            raise

    return write


def main() -> None:  # pragma: no cover - runs in the shadow-consumer container
    import os

    from confluent_kafka import Consumer

    from sentinel.config import Settings, load_replay_config, load_train_config
    from sentinel.logging import configure_logging
    from sentinel.serving.models import MlflowModelSource

    settings = Settings()
    configure_logging(settings.log_level)
    cfg = load_replay_config()
    dsn = os.environ["SENTINEL_SHADOW_DSN"]
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    name = load_train_config().mlflow.model_name(cfg.file_type)
    source = MlflowModelSource(settings.tracking_uri, {name: cfg.file_type}, predict_threads=0)
    models = ModelManager(source, {cfg.file_type: name}, cfg.shadow.alias)
    consumer = Consumer(
        {
            "bootstrap.servers": cfg.bootstrap_servers,
            "group.id": cfg.shadow.group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([cfg.topic])
    log.info("shadow_consumer_started", group=cfg.shadow.group_id, alias=cfg.shadow.alias)
    try:
        totals = run_shadow(
            cast(ConsumerLike, consumer), models, postgres_row_writer(dsn), cfg.shadow, stop
        )
    finally:
        consumer.close()
    log.info("shadow_consumer_stopped", **totals)


if __name__ == "__main__":  # pragma: no cover
    main()
