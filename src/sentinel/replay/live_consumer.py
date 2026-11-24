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

from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode

from sentinel.config import LiveConsumerConfig
from sentinel.logging import get_logger
from sentinel.monitoring.tracing import headers_for_current_span
from sentinel.replay.events import FileEvent

log = get_logger(__name__)

# (body, headers) -> HTTP status; raises on transport errors. Headers carry `traceparent`.
PostFn = Callable[[dict[str, Any], dict[str, str]], int]
tracer = trace.get_tracer("sentinel.live_consumer")


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
    """'ok' | 'rejected:<status>' | 'stopped'. Retries 5xx/transport errors until success.
    Stage 8: one `consume` span per event (retries included); the API's `score` span joins
    it through the `traceparent` header."""
    wait = sleep or stop.wait
    backoff = cfg.retry_backoff_seconds
    body = request_body(ev)
    with tracer.start_as_current_span(
        "consume",
        kind=SpanKind.CONSUMER,
        attributes={"sentinel.event_id": ev.event_id, "sentinel.file_id": ev.file_id},
    ) as span:
        headers = headers_for_current_span()
        attempts = 0
        while not stop.is_set():
            attempts += 1
            try:
                status = post(body, headers)
            except Exception as exc:
                log.warning("live_score_retry", event_id=ev.event_id, error=type(exc).__name__)
                status = -1
            span.set_attribute("sentinel.attempts", attempts)
            if status == 200:
                return "ok"
            if 400 <= status < 500:
                log.error("live_score_rejected", event_id=ev.event_id, status=status)
                span.set_status(StatusCode.ERROR, f"rejected {status}")
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

    from sentinel.config import Settings, load_monitoring_config, load_replay_config
    from sentinel.logging import configure_logging
    from sentinel.monitoring.tracing import setup_tracing

    settings = Settings()
    configure_logging(settings.log_level)
    cfg = load_replay_config()
    tracing = load_monitoring_config().tracing
    setup_tracing(tracing.live_consumer_service_name, tracing, settings.otlp_endpoint)
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

    def post(body: dict[str, Any], headers: dict[str, str]) -> int:
        return client.post("/v1/score", json=body, headers=headers).status_code

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
