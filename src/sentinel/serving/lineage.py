"""Prediction lineage: one row per scoring decision in PostgreSQL `predictions`.

Rows go into a bounded in-memory queue and a background thread inserts them in batches
(by size or time). A slow or unavailable database never blocks or fails a request: a full
queue drops the row, and a batch that keeps failing is dropped after `max_retries`; both are
counted (metric `lineage_dropped`) and logged.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import astuple, dataclass
from datetime import datetime
from typing import Protocol

from sentinel.config import LineageConfig
from sentinel.logging import get_logger
from sentinel.monitoring.metrics import MetricsRecorder

log = get_logger(__name__)


@dataclass(frozen=True)
class PredictionRecord:
    request_id: str
    file_id: str
    tenant_id: str
    model_version: str
    feature_schema_version: str
    threshold: float
    score: float
    verdict: str
    override_applied: bool
    ts: datetime
    event_id: str | None = None  # set for stream events; unique with model_version
    ring: str | None = None  # Stage 6: the tenant's rollout ring that chose the model


COLUMNS = tuple(PredictionRecord.__dataclass_fields__)
INSERT_PREDICTIONS = (
    f"INSERT INTO predictions ({', '.join(COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(COLUMNS))}) ON CONFLICT DO NOTHING"
)

BatchWriter = Callable[[Sequence[PredictionRecord]], None]
_STOP_POLL_SECONDS = 0.05  # how quickly the writer thread notices close()


class PredictionSink(Protocol):
    def submit(self, record: PredictionRecord) -> None: ...

    def close(self) -> None: ...


class InMemorySink:
    def __init__(self) -> None:
        self.records: list[PredictionRecord] = []
        self._lock = threading.Lock()

    def submit(self, record: PredictionRecord) -> None:
        with self._lock:
            self.records.append(record)

    def close(self) -> None:
        return None


class BatchedLineageWriter:
    """Background batch inserter. `submit` never blocks and never raises."""

    def __init__(
        self,
        write: BatchWriter,
        cfg: LineageConfig,
        metrics: MetricsRecorder | None = None,
    ) -> None:
        self._write = write
        self._cfg = cfg
        self._metrics = metrics
        self._q: queue.Queue[PredictionRecord] = queue.Queue(maxsize=cfg.queue_max)
        self._stop = threading.Event()
        self.written = 0
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name="lineage-writer", daemon=True)
        self._thread.start()

    def _drop(self, n: int, why: str) -> None:
        self.dropped += n
        if self._metrics:
            self._metrics.inc("lineage_dropped", {"reason": why}, n)

    def submit(self, record: PredictionRecord) -> None:
        try:
            self._q.put_nowait(record)
        except queue.Full:
            self._drop(1, "queue_full")

    def _take(self, deadline: float) -> list[PredictionRecord]:
        batch: list[PredictionRecord] = []
        while len(batch) < self._cfg.batch_size:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break
            try:
                batch.append(self._q.get(timeout=min(timeout, _STOP_POLL_SECONDS)))
            except queue.Empty:
                if self._stop.is_set():  # shutting down: flush now, do not wait for the timer
                    break
        return batch

    def _flush(self, batch: list[PredictionRecord]) -> None:
        for attempt in range(self._cfg.max_retries + 1):
            try:
                self._write(batch)
                self.written += len(batch)
                return
            except Exception as exc:
                log.error(
                    "lineage_write_failed",
                    rows=len(batch),
                    attempt=attempt + 1,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if self._stop.wait(min(self._cfg.flush_seconds * (attempt + 1), 5.0)):
                    continue  # shutting down: still try the remaining attempts quickly
        self._drop(len(batch), "write_failed")

    def _run(self) -> None:
        while not (self._stop.is_set() and self._q.empty()):
            batch = self._take(time.monotonic() + self._cfg.flush_seconds)
            if batch:
                self._flush(batch)

    def close(self, timeout: float = 10.0) -> None:
        """Flush what is queued (bounded by `timeout`) and stop the thread."""
        self._stop.set()
        self._thread.join(timeout)


def postgres_batch_writer(dsn: str, connect_timeout: int = 5) -> BatchWriter:
    """One reused connection; reconnects after an error."""
    import psycopg

    state: dict[str, psycopg.Connection[tuple[object, ...]] | None] = {"conn": None}

    def write(batch: Sequence[PredictionRecord]) -> None:
        conn = state["conn"]
        try:
            if conn is None or conn.closed:
                conn = state["conn"] = psycopg.connect(dsn, connect_timeout=connect_timeout)
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(INSERT_PREDICTIONS, [astuple(r) for r in batch])
        except Exception:
            if conn is not None:
                conn.close()
            state["conn"] = None
            raise

    return write
