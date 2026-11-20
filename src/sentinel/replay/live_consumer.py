"""Consumer A (live): every event is scored by the serving API — these verdicts count.

    python -m sentinel.replay.live_consumer

At-least-once + idempotent = each event stored once:
- offsets are committed only after every event in the batch got an answer;
- server errors and connection failures are retried with backoff, never skipped (no gaps);
- a 4xx (e.g. 409 schema mismatch) is a permanent answer: counted, logged, not retried;
- the API stores lineage with unique (event_id, model_version), so a re-delivered event does
  not create a second row.
"""

from __future__ import annotations

import signal
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol, cast

from sentinel.config import LiveConsumerConfig
from sentinel.logging import get_logger
from sentinel.replay.events import FileEvent

log = get_logger(__name__)

PostFn = Callable[[dict[str, Any]], int]  # returns the HTTP status; raises on transport errors


class Message(Protocol):
    def value(self) -> bytes | None: ...

    def error(self) -> Any: ...


class ConsumerLike(Protocol):
    def consume(self, num_messages: int, timeout: float) -> Sequence[Message]: ...

    def commit(self, asynchronous: bool = ...) -> Any: ...


def request_body(ev: FileEvent) -> dict[str, Any]:
    return {
        "file_id": ev.file_id,
        "tenant_id": ev.tenant_id,
        "features": ev.features.tolist(),
        "feature_schema_version": ev.feature_schema_version,
        "event_id": ev.event_id,
    }


def score_with_retry(
    ev: FileEvent,
    post: PostFn,
    cfg: LiveConsumerConfig,
    stop: threading.Event,
    sleep: Callable[[float], bool] | None = None,
) -> str:
    """'ok' | 'rejected:<status>' | 'stopped'. Retries 5xx/transport errors until success."""
    wait = sleep or stop.wait
    backoff = cfg.retry_backoff_seconds
    body = request_body(ev)
    while not stop.is_set():
        try:
            status = post(body)
        except Exception as exc:
            log.warning("live_score_retry", event_id=ev.event_id, error=type(exc).__name__)
            status = -1
        if status == 200:
            return "ok"
        if 400 <= status < 500:
            log.error("live_score_rejected", event_id=ev.event_id, status=status)
            return f"rejected:{status}"
        if wait(backoff):
            break
        backoff = min(backoff * 2, cfg.max_backoff_seconds)
    return "stopped"


def run_live(
    consumer: ConsumerLike,
    post: PostFn,
    cfg: LiveConsumerConfig,
    stop: threading.Event,
    max_batches: int | None = None,
    poll_timeout: float = 1.0,
) -> Counter[str]:
    totals: Counter[str] = Counter()
    batches = 0
    with ThreadPoolExecutor(max_workers=cfg.workers) as pool:
        while not stop.is_set() and (max_batches is None or batches < max_batches):
            msgs = consumer.consume(num_messages=cfg.batch_size, timeout=poll_timeout)
            if not msgs:
                batches += 1 if max_batches is not None else 0
                continue
            events: list[FileEvent] = []
            for m in msgs:
                if m.error() is not None:
                    log.error("live_consume_error", error=str(m.error()))
                    totals["broker_error"] += 1
                    continue
                try:
                    events.append(FileEvent.decode(m.value() or b""))
                except Exception as exc:  # a poison message must not block the stream
                    log.error("live_bad_event", error=f"{type(exc).__name__}: {exc}")
                    totals["bad_event"] += 1
            outcomes = list(pool.map(lambda e: score_with_retry(e, post, cfg, stop), events))
            if "stopped" in outcomes:
                break  # do NOT commit: the unfinished events will be re-delivered
            if any(m.error() is None for m in msgs):  # broker-only errors: nothing to commit
                consumer.commit(asynchronous=False)
            totals.update(o.split(":")[0] for o in outcomes)
            batches += 1
    return totals


def main() -> None:  # pragma: no cover - runs in the live-consumer container
    import httpx
    from confluent_kafka import Consumer

    from sentinel.config import Settings, load_replay_config
    from sentinel.logging import configure_logging

    settings = Settings()
    configure_logging(settings.log_level)
    cfg = load_replay_config()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    consumer = Consumer(
        {
            "bootstrap.servers": cfg.bootstrap_servers,
            "group.id": cfg.live.group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )
    consumer.subscribe([cfg.topic])
    client = httpx.Client(
        base_url=cfg.live.api_url,
        timeout=cfg.live.request_timeout_seconds,
        limits=httpx.Limits(max_connections=cfg.live.workers),
    )

    def post(body: dict[str, Any]) -> int:
        return client.post("/v1/score", json=body).status_code

    log.info("live_consumer_started", group=cfg.live.group_id, topic=cfg.topic)
    t0 = time.time()
    try:
        totals = run_live(cast(ConsumerLike, consumer), post, cfg.live, stop)
    finally:
        consumer.close()
        client.close()
    log.info("live_consumer_stopped", seconds=round(time.time() - t0, 1), **totals)


if __name__ == "__main__":  # pragma: no cover
    main()
