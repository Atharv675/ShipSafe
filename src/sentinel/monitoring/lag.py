"""Stage 8 consumer lag: for each consumer group, latest offset minus committed offset,
summed over the topic's partitions. Read every `interval_seconds` by the alert worker and
recorded as the gauge `consumer_lag` (labels: group, topic) in ops_metrics.

A partition the group has never committed counts from the partition's low watermark (the
group would start there with auto.offset.reset=earliest). A failed broker call skips the
reading: a missing point is honest, a guessed one is not.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from sentinel.logging import get_logger
from sentinel.monitoring.metrics import MetricsRecorder

log = get_logger(__name__)


def group_lag(committed: Mapping[int, int], watermarks: Mapping[int, tuple[int, int]]) -> int:
    """Sum over partitions of high watermark - committed offset (never negative).
    `committed` < 0 means "no commit yet" (librdkafka's OFFSET_INVALID is -1001)."""
    total = 0
    for p, (low, high) in watermarks.items():
        c = committed.get(p, -1)
        start = c if c >= 0 else low
        total += max(high - start, 0)
    return total


class LagSource(Protocol):
    def read(self, group: str) -> int | None:
        """The group's lag on the topic now; None if it cannot be read."""
        ...


def record_lag(
    source: LagSource, groups: Sequence[str], topic: str, metrics: MetricsRecorder
) -> dict[str, int | None]:
    out: dict[str, int | None] = {}
    for g in groups:
        try:
            lag = source.read(g)
        except Exception as exc:
            log.warning("consumer_lag_unavailable", group=g, error=type(exc).__name__)
            lag = None
        out[g] = lag
        if lag is not None:
            metrics.observe("consumer_lag", float(lag), {"group": g, "topic": topic})
    return out


class KafkaLagSource:  # pragma: no cover - needs a broker (exercised on the stack)
    """One non-subscribing consumer per group: reading committed offsets does not join the
    group, so it never triggers a rebalance of the real consumers."""

    def __init__(self, bootstrap: str, topic: str, timeout: float) -> None:
        self._bootstrap, self._topic, self._timeout = bootstrap, topic, timeout
        self._consumers: dict[str, Any] = {}

    def _consumer(self, group: str) -> Any:
        from confluent_kafka import Consumer

        c = self._consumers.get(group)
        if c is None:
            c = self._consumers[group] = Consumer(
                {
                    "bootstrap.servers": self._bootstrap,
                    "group.id": group,
                    "enable.auto.commit": False,
                }
            )
        return c

    def read(self, group: str) -> int | None:
        from confluent_kafka import TopicPartition

        c = self._consumer(group)
        md = c.list_topics(self._topic, timeout=self._timeout)
        topic = md.topics.get(self._topic)
        if topic is None or topic.error is not None or not topic.partitions:
            return None  # topic not created yet (no replay so far)
        parts = [TopicPartition(self._topic, p) for p in sorted(topic.partitions)]
        committed = {tp.partition: tp.offset for tp in c.committed(parts, timeout=self._timeout)}
        marks: dict[int, tuple[int, int]] = {}
        for tp in parts:
            low, high = c.get_watermark_offsets(tp, timeout=self._timeout)
            marks[tp.partition] = (int(low), int(high))
        return group_lag(committed, marks)

    def close(self) -> None:
        for c in self._consumers.values():
            c.close()
