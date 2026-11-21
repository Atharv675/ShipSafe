"""Publish cleaned week files to `file-events` in first-seen order, at a configurable speed.

    python -m sentinel.replay.producer --weeks 53 54

Events are keyed by file_id (one file -> one partition). The producer is idempotent at the
broker (enable.idempotence) and the event ids are deterministic, so replaying a week again
re-sends the same ids and consumers store nothing twice.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from sentinel.config import ReplayConfig, Settings, load_replay_config
from sentinel.data.schema import FIRST_SEEN_COLUMN, ID_COLUMN, WEEK_COLUMN
from sentinel.data.splits import feature_columns, load_manifest
from sentinel.logging import configure_logging, get_logger
from sentinel.replay.events import FileEvent, make_event_id, ring_for, tenant_for

log = get_logger(__name__)


def load_events(
    splits_root: Path, file_type: str, weeks: Iterable[int], cfg: ReplayConfig
) -> Iterator[FileEvent]:
    """All rows of the given weeks as events, ordered by (first_seen, file_id)."""
    manifest = load_manifest(splits_root, file_type)
    features = feature_columns(manifest)
    schema = str(manifest["feature_schema_version"])
    ids: list[str] = []
    wk: list[np.ndarray[Any, Any]] = []
    secs: list[np.ndarray[Any, Any]] = []
    xs: list[np.ndarray[Any, Any]] = []
    for w in sorted(set(weeks)):
        t = pq.read_table(
            splits_root / file_type / f"week={w:02d}" / "part.parquet",
            columns=[ID_COLUMN, WEEK_COLUMN, FIRST_SEEN_COLUMN, *features],
        )
        ids += [str(v) for v in t.column(ID_COLUMN).to_pylist()]
        wk.append(t.column(WEEK_COLUMN).to_numpy())
        secs.append(
            t.column(FIRST_SEEN_COLUMN)
            .cast(pa.timestamp("s", tz="UTC"))
            .cast(pa.int64())
            .to_numpy()
        )
        x = np.empty((t.num_rows, len(features)), dtype=np.float32)
        for j, name in enumerate(features):
            x[:, j] = t.column(name).to_numpy()
        xs.append(x)
    if not ids:
        return
    all_secs = np.concatenate(secs)
    all_weeks = np.concatenate(wk)
    all_x = np.concatenate(xs)
    order = np.lexsort((np.asarray(ids), all_secs))  # primary: first_seen; ties: file id
    for i in order:
        tenant = tenant_for(ids[i], cfg.tenants)
        yield FileEvent(
            event_id=make_event_id(file_type, ids[i]),
            file_id=ids[i],
            file_type=file_type,
            tenant_id=tenant,
            ring=ring_for(tenant, cfg.rings),
            week=int(all_weeks[i]),
            first_seen=datetime.fromtimestamp(int(all_secs[i]), UTC).isoformat(),
            feature_schema_version=schema,
            features=all_x[i],
        )


class KafkaProducerLike(Protocol):
    def produce(self, topic: str, value: bytes, key: bytes, on_delivery: Any) -> None: ...

    def poll(self, timeout: float) -> int: ...

    def flush(self, timeout: float) -> int: ...


def publish(
    events: Iterable[FileEvent],
    producer: KafkaProducerLike,
    topic: str,
    events_per_second: float,
    flush_timeout: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """Send events in order; pace to `events_per_second` (0 = unpaced). Raises if any
    delivery failed (a replay with holes would make later comparisons meaningless)."""
    failed: list[str] = []
    sent = 0

    def on_delivery(err: Any, msg: Any) -> None:
        if err is not None:
            failed.append(str(err))

    start = clock()
    for ev in events:
        if events_per_second > 0:
            due = start + sent / events_per_second
            wait = due - clock()
            if wait > 0:
                sleep(wait)
        while True:
            try:
                producer.produce(
                    topic, value=ev.encode(), key=ev.file_id.encode(), on_delivery=on_delivery
                )
                break
            except BufferError:  # local queue full: let it drain
                producer.poll(0.5)
        sent += 1
        producer.poll(0)
    left = producer.flush(flush_timeout)
    if left or failed:
        raise RuntimeError(
            f"{len(failed) + left} of {sent} events were not delivered: {failed[:3]}"
        )
    return {"sent": sent}


def ensure_topic(bootstrap: str, topic: str, partitions: int) -> None:  # pragma: no cover
    from confluent_kafka.admin import AdminClient, NewTopic  # type: ignore[attr-defined]

    admin = AdminClient({"bootstrap.servers": bootstrap})
    if topic in admin.list_topics(timeout=10).topics:
        return
    for _, fut in admin.create_topics([NewTopic(topic, partitions, 1)]).items():
        try:
            fut.result()
        except Exception as exc:
            if "TOPIC_ALREADY_EXISTS" not in str(exc):
                raise


def main() -> None:  # pragma: no cover - exercised by `make replay`
    from confluent_kafka import Producer

    settings = Settings()
    cfg = load_replay_config()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--weeks", type=int, nargs="+", required=True)
    p.add_argument("--file-type", default=cfg.file_types[0], choices=cfg.file_types)
    p.add_argument("--events-per-second", type=float, default=cfg.producer.events_per_second)
    args = p.parse_args()
    configure_logging(settings.log_level)
    ensure_topic(cfg.bootstrap_servers, cfg.topic, cfg.partitions)
    producer = Producer(
        {
            "bootstrap.servers": cfg.bootstrap_servers,
            "enable.idempotence": True,
            "linger.ms": cfg.producer.linger_ms,
            "compression.type": "lz4",
        }
    )
    t0 = time.time()
    out = publish(
        load_events(settings.splits_dir, args.file_type, args.weeks, cfg),
        cast(KafkaProducerLike, producer),
        cfg.topic,
        args.events_per_second,
        cfg.producer.flush_timeout_seconds,
    )
    log.info("replay_published", weeks=args.weeks, seconds=round(time.time() - t0, 1), **out)


if __name__ == "__main__":  # pragma: no cover
    main()
