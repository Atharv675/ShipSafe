"""Stage 4 replay: event format, tenant/ring hashing, producer order, live + shadow consumers."""

from __future__ import annotations

import threading
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import orjson
import pytest

from sentinel.config import (
    LiveConsumerConfig,
    RingsConfig,
    ShadowConsumerConfig,
    load_replay_config,
)
from sentinel.data.pipeline import Layout
from sentinel.data.splits import load_manifest, read_weeks
from sentinel.replay.events import (
    FileEvent,
    make_event_id,
    ring_for,
    tenant_for,
)
from sentinel.replay.live_consumer import request_body, run_live, score_with_retry
from sentinel.replay.producer import load_events, publish
from sentinel.replay.shadow_consumer import (
    INSERT_SHADOW,
    SHADOW_COLUMNS,
    ShadowRow,
    run_shadow,
    score_events,
)
from sentinel.serving.models import ModelManager
from tests.serving_support import DIM, NAMES, SCHEMA, FakeSource, loaded
from tests.unit.test_train import FT, splits  # noqa: F401  (pytest fixture)

RINGS = RingsConfig(ring0_percent=5, ring1_percent=25)
LIVE = LiveConsumerConfig(
    group_id="live",
    api_url="http://api",
    request_timeout_seconds=1,
    batch_size=4,
    workers=2,
    retry_backoff_seconds=0.001,
    max_backoff_seconds=0.002,
)
SHADOW = ShadowConsumerConfig(
    group_id="shadow",
    alias="candidate",
    batch_size=10,
    poll_timeout_seconds=0.01,
    model_poll_seconds=0.01,
    db_retry_seconds=0.001,
)


def _event(i: int, schema: str = SCHEMA["Win64"], dim: int = DIM) -> FileEvent:
    rng = np.random.default_rng(i)
    return FileEvent(
        event_id=make_event_id("Win64", f"file{i}"),
        file_id=f"file{i}",
        file_type="Win64",
        tenant_id=tenant_for(f"file{i}", 200),
        ring=ring_for(tenant_for(f"file{i}", 200), RINGS),
        week=53,
        first_seen="2024-09-22T00:00:00+00:00",
        feature_schema_version=schema,
        features=rng.normal(size=dim).astype(np.float32),
    )


class Msg:
    def __init__(self, payload: bytes | None, err: Any = None) -> None:
        self._p, self._e = payload, err

    def value(self) -> bytes | None:
        return self._p

    def error(self) -> Any:
        return self._e


class FakeConsumer:
    def __init__(self, payloads: list[bytes]) -> None:
        self.pending = [Msg(p) for p in payloads]
        self.committed = 0  # messages covered by commits
        self.delivered = 0
        self.consume_calls = 0

    def consume(self, num_messages: int, timeout: float) -> Sequence[Msg]:
        self.consume_calls += 1
        batch = self.pending[self.committed : self.committed + num_messages]
        self.delivered = self.committed + len(batch)
        return batch

    def commit(self, asynchronous: bool = True) -> None:
        self.committed = self.delivered


# ------------------------------------------------------------------ events


def test_event_round_trip_is_exact_and_carries_no_label() -> None:
    ev = _event(1)
    raw = ev.encode()
    back = FileEvent.decode(raw)
    assert back.event_id == ev.event_id
    assert back.features.dtype == np.float32
    assert np.array_equal(back.features, ev.features)
    assert (back.tenant_id, back.ring, back.week) == (ev.tenant_id, ev.ring, ev.week)
    assert "label" not in orjson.loads(raw)
    with pytest.raises(ValueError, match="unsupported event version"):
        FileEvent.decode(orjson.dumps({**orjson.loads(raw), "v": 99}))


def test_tenant_and_ring_are_stable_and_match_the_split() -> None:
    assert tenant_for("abc", 200) == tenant_for("abc", 200)
    assert make_event_id("Win64", "abc") == "Win64:abc"
    tenants = [f"tenant-{i:05d}" for i in range(20_000)]
    rings = Counter(ring_for(t, RINGS) for t in tenants)
    assert abs(rings["ring0"] / 20_000 - 0.05) < 0.01
    assert abs(rings["ring1"] / 20_000 - 0.25) < 0.015
    assert abs(rings["ring2"] / 20_000 - 0.70) < 0.015
    assert ring_for("tenant-0001", RINGS) == ring_for("tenant-0001", RINGS)
    assert all(
        ring_for(t, RingsConfig(ring0_percent=0, ring1_percent=0)) == "ring2" for t in tenants[:100]
    )


def test_replay_config_file_is_valid() -> None:
    cfg = load_replay_config()
    assert (cfg.rings.ring0_percent, cfg.rings.ring1_percent) == (5, 25)
    assert cfg.live.group_id != cfg.shadow.group_id  # separate consumer groups
    assert cfg.shadow.alias == "candidate"
    assert cfg.file_types == ["Win64", "Win32"]  # shadow loads a candidate for each


# ------------------------------------------------------------------ producer


def test_load_events_is_time_ordered_complete_and_deterministic(splits: Layout) -> None:  # noqa: F811
    cfg = load_replay_config().model_copy(update={"tenants": 7})
    evs = list(load_events(splits.splits_root, FT, [7, 6], cfg))
    df = read_weeks(splits.splits_root, FT, [6, 7])
    assert len(evs) == len(df)
    assert len({e.event_id for e in evs}) == len(evs)
    times = [e.first_seen for e in evs]
    assert times == sorted(times)
    again = [e.event_id for e in load_events(splits.splits_root, FT, [6, 7], cfg)]
    assert again == [e.event_id for e in evs]
    schema = load_manifest(splits.splits_root, FT)["feature_schema_version"]
    assert {e.feature_schema_version for e in evs} == {schema}
    row = df.set_index("sha256").loc[evs[0].file_id]
    cols = [c for c in df.columns if c.startswith("f_")]
    assert np.array_equal(evs[0].features, row[cols].to_numpy(dtype=np.float32))
    assert {e.tenant_id for e in evs} <= {f"tenant-{i:04d}" for i in range(7)}


class FakeProducer:
    def __init__(self, fail_every: int = 0, buffer_errors: int = 0) -> None:
        self.sent: list[tuple[bytes, bytes]] = []
        self.callbacks: list[Any] = []
        self.fail_every = fail_every
        self.buffer_errors = buffer_errors

    def produce(self, topic: str, value: bytes, key: bytes, on_delivery: Any) -> None:
        if self.buffer_errors:
            self.buffer_errors -= 1
            raise BufferError
        self.sent.append((key, value))
        self.callbacks.append(on_delivery)

    def poll(self, timeout: float) -> int:
        return 0

    def flush(self, timeout: float) -> int:
        for i, cb in enumerate(self.callbacks):
            err = "boom" if self.fail_every and (i + 1) % self.fail_every == 0 else None
            cb(err, None)
        self.callbacks = []
        return 0


def test_publish_keeps_order_keys_by_file_and_paces() -> None:
    evs = [_event(i) for i in range(5)]
    prod = FakeProducer(buffer_errors=2)
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(s)
        now[0] += s

    out = publish(
        evs, prod, "t", events_per_second=10, flush_timeout=1, sleep=sleep, clock=lambda: now[0]
    )
    assert out == {"sent": 5}
    assert [k for k, _ in prod.sent] == [e.file_id.encode() for e in evs]
    assert [FileEvent.decode(v).event_id for _, v in prod.sent] == [e.event_id for e in evs]
    assert sum(slept) == pytest.approx(0.4)  # 5 events at 10/s: due at 0, .1, .2, .3, .4


def test_publish_fails_loudly_on_lost_deliveries() -> None:
    with pytest.raises(RuntimeError, match="not delivered"):
        publish([_event(i) for i in range(4)], FakeProducer(fail_every=2), "t", 0, 1)


# ------------------------------------------------------------------ live consumer


def test_live_retries_server_errors_until_success_and_never_skips() -> None:
    statuses = iter([503, -2, 500, 200])
    calls: list[dict[str, Any]] = []

    def post(body: dict[str, Any], headers: dict[str, str]) -> int:
        calls.append(body)
        s = next(statuses)
        if s == -2:
            raise ConnectionError("api restarting")
        return s

    ev = _event(1)
    assert score_with_retry(ev, post, LIVE, threading.Event()) == "ok"
    assert len(calls) == 4
    assert calls[0] == request_body(ev)
    assert calls[0]["event_id"] == ev.event_id
    assert "label" not in calls[0]


def test_live_does_not_retry_permanent_rejections() -> None:
    calls = []

    def post(body: dict[str, Any], headers: dict[str, str]) -> int:
        calls.append(1)
        return 409

    assert score_with_retry(_event(1), post, LIVE, threading.Event()) == "rejected:409"
    assert len(calls) == 1


def test_live_commits_only_after_the_whole_batch_is_answered() -> None:
    payloads = [_event(i).encode() for i in range(10)] + [b"not json"]
    consumer = FakeConsumer(payloads)
    seen: list[str] = []
    lock = threading.Lock()

    def post(body: dict[str, Any], headers: dict[str, str]) -> int:
        with lock:
            seen.append(body["event_id"])
        return 200

    totals = run_live(consumer, post, LIVE, threading.Event(), max_batches=3)
    assert totals == Counter({"ok": 10, "bad_event": 1})
    assert consumer.committed == 11
    assert sorted(seen) == sorted(_event(i).event_id for i in range(10))


def test_live_stop_during_retry_leaves_batch_uncommitted() -> None:
    consumer = FakeConsumer([_event(i).encode() for i in range(3)])
    stop = threading.Event()

    def post(body: dict[str, Any], headers: dict[str, str]) -> int:
        stop.set()  # shut down while the API is failing
        return 503

    run_live(consumer, post, LIVE, stop, max_batches=1)
    assert consumer.committed == 0  # will be re-delivered: no gap


# ------------------------------------------------------------------ shadow consumer


def _candidate_manager(with_candidate: bool = True) -> tuple[ModelManager, FakeSource]:
    src = FakeSource()
    if with_candidate:
        src.add(loaded("Win64", "2", threshold=0.5, seed=5))
    return ModelManager(src, {"Win64": NAMES["Win64"]}, "candidate"), src


def test_shadow_scores_in_batches_and_matches_the_model() -> None:
    models, _ = _candidate_manager()
    models.refresh()
    evs = [_event(i) for i in range(5)] + [_event(99, schema="other-schema")]
    rows, skipped = score_events(evs, models)
    assert skipped == 1
    m = models.get(SCHEMA["Win64"])
    assert m is not None
    expected = m.score(np.stack([e.features for e in evs[:5]]))
    assert [r.score for r in rows] == pytest.approx(expected.tolist())
    assert {r.model_version for r in rows} == {"sentinel-win64/2"}
    assert all(r.verdict == ("malicious" if r.score > 0.5 else "benign") for r in rows)
    assert rows[0].ring == evs[0].ring
    assert INSERT_SHADOW.count("%s") == len(SHADOW_COLUMNS)
    assert "ON CONFLICT DO NOTHING" in INSERT_SHADOW


def test_shadow_waits_for_a_candidate_without_consuming() -> None:
    models, src = _candidate_manager(with_candidate=False)
    src.alias[NAMES["Win64"]] = "missing"
    src.fail.add((NAMES["Win64"], "missing"))
    consumer = FakeConsumer([_event(1).encode()])
    totals = run_shadow(
        consumer, models, lambda rows: None, SHADOW, threading.Event(), max_batches=3
    )
    assert totals["waiting_for_candidate"] == 3
    assert consumer.consume_calls == 0 and consumer.committed == 0


def test_shadow_retries_the_database_then_commits_and_stays_idempotent() -> None:
    models, _ = _candidate_manager()
    store: dict[tuple[str, str], ShadowRow] = {}  # primary key (event_id, model_version)
    fails = [1]

    def write(rows: Sequence[ShadowRow]) -> None:
        if fails[0]:
            fails[0] -= 1
            raise ConnectionError("db down")
        for r in rows:
            store.setdefault((r.event_id, r.model_version), r)

    payloads = [_event(i).encode() for i in range(10)]
    consumer = FakeConsumer(payloads)
    totals = run_shadow(consumer, models, write, SHADOW, threading.Event(), max_batches=2)
    assert totals["db_retry"] == 1
    assert consumer.committed == 10
    assert len(store) == 10
    # the same week replayed again (new consumer, same ids): still 10 rows
    run_shadow(FakeConsumer(payloads), models, write, SHADOW, threading.Event(), max_batches=2)
    assert len(store) == 10


def test_shadow_stop_while_db_is_down_commits_nothing() -> None:
    models, _ = _candidate_manager()
    stop = threading.Event()

    def write(rows: Sequence[ShadowRow]) -> None:
        stop.set()
        raise ConnectionError("db down")

    consumer = FakeConsumer([_event(i).encode() for i in range(3)])
    run_shadow(consumer, models, write, SHADOW, stop, max_batches=1)
    assert consumer.committed == 0


# ------------------------------------------------------------------ candidate registration


def test_register_candidate_sets_alias_and_leaves_production_alone(
    splits: Layout,  # noqa: F811
    tmp_path: Path,
) -> None:
    from mlflow import MlflowClient
    from mlflow.exceptions import MlflowException

    from sentinel.models.registry import register_candidate
    from sentinel.models.retrain import retrain_candidate
    from sentinel.serving.models import MlflowModelSource
    from tests.unit.test_retrain import _rcfg
    from tests.unit.test_train import _cfg

    bundle = tmp_path / "cand"
    retrain_candidate(
        FT,
        splits.splits_root,
        splits.never_break,
        bundle,
        _cfg(),
        _rcfg(as_of_week=6),
        lgbm_threads=1,
    )
    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    with pytest.raises(ValueError, match="production alias"):
        register_candidate(bundle, uri, tmp_path / "artifacts", _cfg(), "production")
    reg = register_candidate(bundle, uri, tmp_path / "artifacts", _cfg(), "candidate")
    assert (reg["model_name"], reg["alias"]) == ("sentinel-win64", "candidate")
    client = MlflowClient(tracking_uri=uri)
    mv = client.get_model_version_by_alias("sentinel-win64", "candidate")
    assert mv.tags["kind"] == "candidate"
    assert mv.tags["as_of_week"] == "6"
    with pytest.raises(MlflowException):
        client.get_model_version_by_alias("sentinel-win64", "production")
    src = MlflowModelSource(uri, {"sentinel-win64": FT}, predict_threads=1)
    m = src.load("sentinel-win64", src.resolve("sentinel-win64", "candidate"))
    m.warm_up()
    assert m.file_type == FT


class StrictConsumer(FakeConsumer):
    """Like the real client: committing with no stored offset raises (_NO_OFFSET)."""

    def commit(self, asynchronous: bool = True) -> None:
        if not any(m.error() is None for m in self.pending[self.committed : self.delivered]):
            raise RuntimeError("Commit failed: Local: No offset stored")
        super().commit(asynchronous)


def test_consumers_survive_broker_only_errors_before_the_topic_exists() -> None:
    errors = [Msg(None, err="UNKNOWN_TOPIC_OR_PART")]
    live = StrictConsumer([])
    live.pending = list(errors)
    totals = run_live(live, lambda b, h: 200, LIVE, threading.Event(), max_batches=1)
    assert totals["broker_error"] == 1
    models, _ = _candidate_manager()
    shadow = StrictConsumer([])
    shadow.pending = list(errors)
    totals = run_shadow(shadow, models, lambda r: None, SHADOW, threading.Event(), max_batches=1)
    assert totals["broker_error"] == 1
