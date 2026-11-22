"""Stage 6: ring routing, rollout state machine, watch signals, kill switch (no stack)."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sentinel.config import RingsConfig, RolloutConfig, load_rollout_config, load_serving_config
from sentinel.models.evaluate import rate_increase_check, wilson_interval
from sentinel.monitoring.metrics import MetricsRecorder
from sentinel.replay.events import ring_for
from sentinel.rollout.controller import (
    Controller,
    RingTraffic,
    RolloutError,
    ServiceHealth,
    State,
    Transition,
    TransitionConflictError,
    WatchSignals,
    canary_tenant,
    desired_aliases,
    evaluate,
)
from sentinel.rollout.run import kill_and_time
from sentinel.rollout.store import (
    canary_event_id,
    emergency_rollback,
    health_from_stats,
    like_prefix,
    timeline_markdown,
    timeline_rows,
)
from sentinel.serving.app import create_app
from sentinel.serving.lineage import InMemorySink
from sentinel.serving.models import LoadedModel, ModelManager
from sentinel.serving.overrides import InMemoryOverrideStore
from sentinel.serving.rings import RingRouter, SharedModelSource
from tests.serving_support import DIM, NAMES, SCHEMA, loaded, serving_cfg

CFG = load_rollout_config()
RINGS = RingsConfig(ring0_percent=5, ring1_percent=25)
NAME = NAMES["Win64"]
T0 = datetime(2026, 11, 21, 10, 0, tzinfo=UTC)


# ------------------------------------------------------------------ fakes


class Registry:
    """In-memory registry used BOTH by serving (ModelSource) and by the controller."""

    def __init__(self) -> None:
        self.alias: dict[tuple[str, str], str] = {}
        self.models: dict[tuple[str, str], LoadedModel] = {}
        self.tags: dict[tuple[str, str], dict[str, str]] = {}
        self.loads = 0
        self.moves: list[tuple[str, str, str]] = []

    def add(self, m: LoadedModel, *aliases: str, tags: dict[str, str] | None = None) -> None:
        self.models[(m.name, m.version)] = m
        self.tags[(m.name, m.version)] = tags or {}
        for a in aliases:
            self.alias[(m.name, a)] = m.version

    # ModelSource
    def resolve(self, name: str, alias: str) -> str:
        return self.alias[(name, alias)]  # KeyError is a LookupError -> next alias

    def load(self, name: str, version: str) -> LoadedModel:
        self.loads += 1
        return self.models[(name, version)]

    # controller Registry
    def aliases(self, name: str) -> dict[str, str]:
        return {a: v for (n, a), v in self.alias.items() if n == name}

    def set_alias(self, name: str, alias: str, version: str) -> None:
        self.alias[(name, alias)] = version
        self.moves.append((name, alias, version))

    def version_tags(self, name: str, version: str) -> dict[str, str]:
        return self.tags[(name, version)]


class Store:
    def __init__(self) -> None:
        self.rows: list[Transition] = []
        self.promotes: dict[tuple[str, str, str], str] = {}
        self.clock = T0

    def latest(self, file_type: str) -> Transition | None:
        rows = [t for t in self.rows if t.file_type == file_type]
        return rows[-1] if rows else None

    def first(self, rollout_id: str) -> Transition:
        return next(t for t in self.rows if t.rollout_id == rollout_id)

    def append(self, prev: Transition | None, **kw: Any) -> Transition:
        if self.latest(kw["file_type"]) != prev:
            raise TransitionConflictError("moved")
        same = prev is not None and prev.rollout_id == kw["rollout_id"]
        self.clock += timedelta(seconds=10)
        t = Transition(
            seq=prev.seq + 1 if same and prev else 1,
            from_state=prev.to_state if same and prev else None,
            ts=self.clock,
            signals=dict(kw.pop("signals")),
            **kw,
        )
        self.rows.append(t)
        return t

    def promote_decision(self, file_type: str, candidate: str, production: str) -> str | None:
        return self.promotes.get((file_type, candidate, production))


class Meter:
    """Traffic scripted per ring; health fixed unless overridden."""

    def __init__(self, traffic: Mapping[str, RingTraffic], health: ServiceHealth | None) -> None:
        self.by_ring = dict(traffic)
        self._health = health

    def traffic(self, current: Transition, rollout_start: Transition, ring: str) -> RingTraffic:
        return self.by_ring[ring]

    def health(self, candidate_label: str, production_label: str) -> ServiceHealth | None:
        return self._health


class Canaries:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send(self, ring: str, file_type: str, candidate_version: str) -> int:
        self.calls.append((ring, file_type, candidate_version))
        return CFG.canaries.required_per_ring


def healthy(ring: str, events: int = 10_000, flagged_share: float = 0.45) -> RingTraffic:
    return RingTraffic(
        ring=ring,
        candidate_events=events,
        candidate_flagged=int(events * flagged_share),
        production_events=50_000,
        production_flagged=int(50_000 * 0.45),
        canaries_in_ring=CFG.canaries.required_per_ring,
        canary_hits=0,
    )


GOOD_HEALTH = ServiceHealth(5_000, 0, 4.0, 5_000, 3.5)


def setup(
    traffic: Mapping[str, RingTraffic] | None = None, health: ServiceHealth | None = GOOD_HEALTH
) -> tuple[Controller, Store, Registry, Meter, Canaries]:
    reg = Registry()
    reg.add(loaded("Win64", "1", seed=1), "production", "ring0_model", "ring1_model", "ring2_model")
    reg.add(loaded("Win64", "7", seed=3), tags={"demo": "true"})
    reg.add(loaded("Win64", "8", seed=4))  # a gate-approved candidate (set per test)
    store = Store()
    meter = Meter(traffic or {r: healthy(r) for r in ("ring0", "ring1", "ring2")}, health)
    canaries = Canaries()
    return (
        Controller(store, reg, meter, canaries, CFG, "candidate_approved"),
        store,
        reg,
        meter,
        canaries,
    )


def ring_aliases(reg: Registry) -> dict[str, str]:
    a = reg.aliases(NAME)
    return {r: a[CFG.ring_aliases[r]] for r in ("ring0", "ring1", "ring2")}  # type: ignore[index]


# ------------------------------------------------------------------ statistics


def test_rate_increase_needs_clear_separation() -> None:
    small = rate_increase_check(6, 10, 450, 1000, 0.10)  # 60% of 10 vs 45%: could be noise
    assert not small.breach
    clear = rate_increase_check(700, 1000, 450, 1000, 0.10)
    assert clear.breach
    assert clear.candidate_ci == wilson_interval(700, 1000)
    assert clear.limit == pytest.approx(wilson_interval(450, 1000)[1] * 1.10)
    assert clear.as_dict()["candidate_rate"] == 0.7
    nothing = rate_increase_check(0, 0, 450, 1000, 0.10)
    assert not nothing.breach and nothing.as_dict()["candidate_rate"] is None
    with pytest.raises(ValueError):
        rate_increase_check(1, 2, 1, 2, -0.1)


def test_flag_rate_hides_a_6x_false_alarm_rise_on_balanced_traffic() -> None:
    """The finding behind the canaries: measured on Win64 week 56, a 0.5 cut-off moves the
    flag rate only 45.4% -> 49.5% (balanced traffic), which a ring0-sized sample cannot tell
    apart from production even though false alarms rose 6x."""
    ring0 = rate_increase_check(int(0.495 * 500), 500, int(0.454 * 9_500), 9_500, 0.10)
    assert not ring0.breach


# ------------------------------------------------------------------ config


def test_rollout_config_file_is_valid() -> None:
    assert CFG.ring_aliases == {
        "ring0": "ring0_model",
        "ring1": "ring1_model",
        "ring2": "ring2_model",
    }
    assert CFG.fallback_alias == load_serving_config().model_alias == "production"
    assert CFG.signals.never_break_hits_allowed == 0
    assert CFG.min_events["ring0"] < CFG.min_events["ring1"] < CFG.min_events["ring2"]


@pytest.mark.parametrize(
    "change",
    [
        {"ring_aliases": {"ring0": "a", "ring1": "b"}},
        {"ring_aliases": {"ring0": "a", "ring1": "a", "ring2": "c"}},
        {"ring_aliases": {"ring0": "production", "ring1": "b", "ring2": "c"}},
        {"min_events": {"ring0": 0, "ring1": 1, "ring2": 1}},
    ],
)
def test_rollout_config_rejects_bad_rings(change: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RolloutConfig.model_validate({**CFG.model_dump(), **change})


# ------------------------------------------------------------------ state machine


def test_desired_aliases_per_state() -> None:
    def d(s: State) -> dict[str, str]:
        return desired_aliases(s, "9", "1", CFG)

    assert d(State.APPROVED) == {"ring0_model": "1", "ring1_model": "1", "ring2_model": "1"}
    assert d(State.RING0) == {"ring0_model": "9", "ring1_model": "1", "ring2_model": "1"}
    assert d(State.RING1) == {"ring0_model": "9", "ring1_model": "9", "ring2_model": "1"}
    assert d(State.RING2) == {"ring0_model": "9", "ring1_model": "9", "ring2_model": "9"}
    assert d(State.COMPLETE) == {
        "ring0_model": "9",
        "ring1_model": "9",
        "ring2_model": "9",
        "production": "9",
    }
    assert d(State.ROLLED_BACK) == d(State.APPROVED)


def test_canary_tenant_lands_in_its_ring() -> None:
    for r in ("ring0", "ring1", "ring2"):
        t = canary_tenant(r, RINGS, CFG.canaries.tenant_prefix)
        assert t.startswith("canary-") and ring_for(t, RINGS) == r
    assert canary_event_id("Win64", "ring0", "abc") == "Win64:canary:ring0:abc"


def test_evaluate_advance_hold_rollback() -> None:
    ok = evaluate(WatchSignals(healthy("ring0"), GOOD_HEALTH), CFG)
    assert ok.action == "advance" and ok.reason.startswith("ring0 healthy")
    few = evaluate(WatchSignals(healthy("ring0", events=10), GOOD_HEALTH), CFG)
    assert few.action == "hold" and "[volume]" in few.reason
    no_api = evaluate(WatchSignals(healthy("ring0"), None), CFG)
    assert no_api.action == "hold" and "API metrics unavailable" in no_api.reason
    hit = replace(healthy("ring0", events=10), canary_hits=1)  # breach beats low volume
    rb = evaluate(WatchSignals(hit, GOOD_HEALTH), CFG)
    assert rb.action == "rollback" and "[never_break]" in rb.reason
    flags = evaluate(WatchSignals(healthy("ring0", flagged_share=0.70), GOOD_HEALTH), CFG)
    assert flags.action == "rollback" and "[flag_rate]" in flags.reason
    errs = evaluate(WatchSignals(healthy("ring0"), replace(GOOD_HEALTH, candidate_errors=500)), CFG)
    assert errs.action == "rollback" and "[errors]" in errs.reason
    slow = evaluate(
        WatchSignals(healthy("ring0"), replace(GOOD_HEALTH, candidate_p99_ms=80.0)), CFG
    )
    assert slow.action == "rollback" and "[latency]" in slow.reason
    # last ring: nothing runs on production alongside it, so flag rate is not compared there
    last = evaluate(WatchSignals(healthy("ring2", flagged_share=0.70), GOOD_HEALTH), CFG)
    assert last.action == "advance" and "not compared in ring2" in last.reason
    canaries_missing = evaluate(
        WatchSignals(replace(healthy("ring0"), canaries_in_ring=10), GOOD_HEALTH), CFG
    )
    assert canaries_missing.action == "hold" and "canaries" in canaries_missing.reason
    assert {c["name"] for c in ok.as_dict()["checks"]} == {
        "never_break",
        "flag_rate",
        "errors",
        "latency",
        "volume",
    }


# ------------------------------------------------------------------ controller


def test_bad_model_is_rolled_back_in_ring0_before_ring1() -> None:
    bad = replace(healthy("ring0", events=40), canary_hits=1)
    ctl, store, reg, _, canaries = setup({"ring0": bad})
    ctl.start("Win64", NAME, "7", T0, demo=True)
    t, _ = ctl.tick("Win64")
    assert t.to_state is State.RING0
    assert ring_aliases(reg) == {"ring0": "7", "ring1": "1", "ring2": "1"}
    t, verdict = ctl.tick("Win64")
    assert t.to_state is State.ROLLED_BACK
    assert verdict is not None and "[never_break]" in verdict.reason
    assert ring_aliases(reg) == {"ring0": "1", "ring1": "1", "ring2": "1"}
    assert reg.aliases(NAME)["production"] == "1"
    assert (NAME, "ring1_model", "7") not in reg.moves  # ring1 never saw the bad model
    assert [r.to_state for r in store.rows] == [State.APPROVED, State.RING0, State.ROLLED_BACK]
    assert store.rows[-1].signals["verdict"]["action"] == "rollback"
    assert canaries.calls == []  # canaries already complete in the scripted traffic
    assert ctl.tick("Win64") == (store.rows[-1], None)  # terminal: nothing more happens


def test_good_model_reaches_complete_and_becomes_production() -> None:
    ctl, store, reg, _, _ = setup()
    first = ctl.start("Win64", NAME, "7", T0, demo=True)
    assert "DEMO approval" in first.reason
    states = [first.to_state]
    for _ in range(10):
        t, _ = ctl.tick("Win64")
        if t.to_state != states[-1]:
            states.append(t.to_state)
        if t.to_state is State.COMPLETE:
            break
    assert states == [State.APPROVED, State.RING0, State.RING1, State.RING2, State.COMPLETE]
    assert ring_aliases(reg) == {"ring0": "7", "ring1": "7", "ring2": "7"}
    assert reg.aliases(NAME)["production"] == "7"
    assert [r.seq for r in store.rows] == [1, 2, 3, 4, 5]


def test_hold_until_enough_events_and_canaries_are_sent() -> None:
    thin = replace(healthy("ring0", events=50), canaries_in_ring=0)
    ctl, store, _, _, canaries = setup({"ring0": thin})
    ctl.start("Win64", NAME, "7", T0, demo=True)
    ctl.tick("Win64")
    t, verdict = ctl.tick("Win64")
    assert t.to_state is State.RING0 and verdict is not None and verdict.action == "hold"
    assert canaries.calls == [("ring0", "Win64", "7")]
    assert len(store.rows) == 2  # a hold writes nothing


def test_start_needs_gate_promote_or_demo_tag() -> None:
    ctl, store, reg, _, _ = setup()
    with pytest.raises(RolloutError, match="candidate_approved"):
        ctl.start("Win64", NAME, "8", T0)
    reg.set_alias(NAME, "candidate_approved", "8")
    with pytest.raises(RolloutError, match="no gate PROMOTE"):
        ctl.start("Win64", NAME, "8", T0)
    store.promotes[("Win64", f"{NAME}/8", f"{NAME}/1")] = "Win64-v8-vs-v1-x"
    t = ctl.start("Win64", NAME, "8", T0)
    assert t.reason == "gate decision Win64-v8-vs-v1-x: PROMOTE"
    with pytest.raises(RolloutError, match="still APPROVED"):
        ctl.start("Win64", NAME, "7", T0, demo=True)


def test_start_refusals() -> None:
    ctl, _, _, _, _ = setup()
    with pytest.raises(RolloutError, match="tagged demo=true"):
        ctl.start("Win64", NAME, "8", T0, demo=True)
    with pytest.raises(RolloutError, match="already production"):
        ctl.start("Win64", NAME, "1", T0, demo=True)
    with pytest.raises(RolloutError, match="no rollout"):
        ctl.tick("Win32")


def test_kill_switch_racing_the_controller_wins() -> None:
    ctl, store, reg, _, _ = setup()
    ctl.start("Win64", NAME, "7", T0, demo=True)
    ctl.tick("Win64")  # RING0

    real_append = store.append

    def racing_append(prev: Transition | None, **kw: Any) -> Transition:
        # the kill switch lands between the controller's read and its write
        emergency_rollback(store, reg, {"Win64": NAME}, CFG, "kill_switch", "operator")
        store.append = real_append  # type: ignore[method-assign]
        return real_append(prev, **kw)

    store.append = racing_append  # type: ignore[method-assign]
    t, verdict = ctl.tick("Win64")
    assert t.to_state is State.ROLLED_BACK and t.actor == "kill_switch" and verdict is None
    assert ring_aliases(reg) == {"ring0": "1", "ring1": "1", "ring2": "1"}


def test_emergency_rollback_without_rollout_and_when_recording_fails() -> None:
    _, store, reg, _, _ = setup()
    reg.set_alias(NAME, "ring1_model", "7")  # someone left a ring on another version
    out = emergency_rollback(store, reg, {"Win64": NAME}, CFG, "kill_switch", "drill")
    assert out["Win64"]["recorded"] is None and out["Win64"]["moved"] == {"ring1_model": "1"}

    class Down(Store):
        def latest(self, file_type: str) -> Transition | None:
            raise ConnectionError("db down")

    reg.set_alias(NAME, "ring0_model", "7")
    out = emergency_rollback(Down(), reg, {"Win64": NAME}, CFG, "kill_switch", "db down")
    assert "record_error" in out["Win64"]
    assert ring_aliases(reg) == {"ring0": "1", "ring1": "1", "ring2": "1"}  # safety first
    missing = emergency_rollback(store, Registry(), {"Win64": NAME}, CFG, "k", "r")
    assert "no 'production' alias" in missing["Win64"]["error"]


def test_timeline_and_helpers() -> None:
    ctl, store, _, _, _ = setup()
    ctl.start("Win64", NAME, "7", T0, demo=True)
    ctl.tick("Win64")
    rows = timeline_rows(store.rows)
    assert [r["t_seconds"] for r in rows] == [0.0, 10.0]
    assert rows[1]["from"] == "APPROVED" and rows[1]["to"] == "RING0"
    md = timeline_markdown(store.rows)
    assert md.startswith("| t (s) |") and "**RING0**" in md
    assert timeline_rows([]) == []
    assert like_prefix("canary_x%") == "canary\\_x\\%%"
    stats = [
        {"metric": "requests", "labels": {"model_version": "m/7", "status": "200"}, "count": 90},
        {"metric": "requests", "labels": {"model_version": "m/7", "status": "500"}, "count": 3},
        {"metric": "requests", "labels": {"model_version": "m/1", "status": "500"}, "count": 9},
        {"metric": "latency_ms", "labels": {"model_version": "m/7"}, "count": 93, "p99": 7.5},
        {"metric": "latency_ms", "labels": {"model_version": "m/1"}, "count": 999, "p99": 5.0},
    ]
    assert health_from_stats(stats, "m/7", "m/1") == ServiceHealth(93, 3, 7.5, 93, 5.0)


# ------------------------------------------------------------------ serving: rings


def _router(reg: Registry, metrics: MetricsRecorder | None = None) -> RingRouter:
    return RingRouter.build(reg, NAMES, CFG, RINGS, metrics)


def _two_types(reg: Registry, *aliases: str) -> None:
    reg.add(loaded("Win64", "1", seed=1), *aliases)
    reg.add(loaded("Win32", "1", seed=2), *aliases)


def test_missing_ring_aliases_fall_back_to_production_and_share_one_load() -> None:
    reg = Registry()
    _two_types(reg, "production")
    router = _router(reg)
    out = router.refresh()
    assert out["ring0"] == {"Win64": "loaded:1", "Win32": "loaded:1"}
    assert router.ready
    assert reg.loads == 2  # one per file type, not one per ring
    assert router.serving_versions() == {r: {"Win64": "1", "Win32": "1"} for r in out}
    assert all(s["aliases"][-1] == "production" for s in router.status())


def test_ring_alias_routes_only_that_rings_tenants() -> None:
    reg = Registry()
    _two_types(reg, "production", "ring0_model", "ring1_model", "ring2_model")
    reg.add(loaded("Win64", "7", seed=3))
    router = _router(reg)
    router.refresh()
    reg.set_alias(NAME, "ring0_model", "7")
    assert router.refresh()["ring0"]["Win64"] == "loaded:7"
    t0 = canary_tenant("ring0", RINGS, "t-")
    t2 = canary_tenant("ring2", RINGS, "t-")
    ring, m = router.get(t0, SCHEMA["Win64"])
    assert (ring, m and m.version) == ("ring0", "7")
    ring, m = router.get(t2, SCHEMA["Win64"])
    assert (ring, m and m.version) == ("ring2", "1")
    assert router.ring_of(t0) == "ring0"
    assert router.expected_schemas() == {SCHEMA["Win64"]: "Win64", SCHEMA["Win32"]: "Win32"}


def test_shared_source_frees_unused_versions() -> None:
    class Fresh(Registry):  # builds a new object per load, like the MLflow source
        def load(self, name: str, version: str) -> LoadedModel:
            self.loads += 1
            return loaded("Win64", version, seed=1)

    reg = Fresh()
    shared = SharedModelSource(reg)
    a = shared.load(NAME, "1")
    assert shared.load(NAME, "1") is a and reg.loads == 1
    del a
    import gc

    gc.collect()
    shared.load(NAME, "1")
    assert reg.loads == 2  # nobody held it, so it was loaded again


def test_router_needs_every_ring() -> None:
    reg = Registry()
    m = ModelManager(reg, NAMES, "production")
    with pytest.raises(ValueError):
        RingRouter({"ring0": m}, RINGS)
    with pytest.raises(ValueError):
        ModelManager(reg, NAMES, ())


# ------------------------------------------------------------------ serving: kill switch

KEY = "test-admin-key"


def _api(
    reg: Registry, key: str | None = KEY
) -> tuple[TestClient, RingRouter, InMemorySink, Store]:
    store = Store()
    router = _router(reg)
    router.refresh()
    sink = InMemorySink()

    def kill(reason: str) -> dict[str, Any]:
        return emergency_rollback(store, reg, {"Win64": NAME}, CFG, "kill_switch", reason)

    app = create_app(
        router,
        InMemoryOverrideStore(),
        sink,
        MetricsRecorder("api", 100),
        serving_cfg(),
        background=False,
        kill_switch=kill,
        admin_api_key=key,
    )
    return TestClient(app), router, sink, store


def _body(tenant: str) -> dict[str, Any]:
    return {
        "file_id": "f1",
        "tenant_id": tenant,
        "features": [0.1] * DIM,
        "feature_schema_version": SCHEMA["Win64"],
    }


def test_kill_switch_restores_every_ring_in_under_60_seconds() -> None:
    reg = Registry()
    _two_types(reg, "production", "ring0_model", "ring1_model", "ring2_model")
    reg.add(loaded("Win64", "7", seed=3))
    c, router, sink, _ = _api(reg)
    for alias in ("ring0_model", "ring1_model"):
        reg.set_alias(NAME, alias, "7")
    router.refresh()
    assert router.serving_versions()["ring1"]["Win64"] == "7"
    with c:
        r = c.post("/v1/score", json=_body(canary_tenant("ring0", RINGS, "x-")))
        assert r.json()["model_version"] == f"{NAME}/7"
        assert sink.records[-1].ring == "ring0"
        t0 = time.monotonic()
        k = c.post("/admin/rollback", json={"reason": "drill"}, headers={"X-API-Key": KEY})
        elapsed = time.monotonic() - t0
        assert k.status_code == 200, k.text
        assert c.get("/v1/rings").json() == {
            r: {"Win64": "1", "Win32": "1"} for r in ("ring0", "ring1", "ring2")
        }
        r = c.post("/v1/score", json=_body(canary_tenant("ring0", RINGS, "x-")))
        assert r.json()["model_version"] == f"{NAME}/1"
    assert elapsed < 60 and k.json()["seconds"] < 60
    assert k.json()["result"]["Win64"]["moved"] == {"ring0_model": "1", "ring1_model": "1"}


def test_canary_requests_never_enter_the_watched_metrics() -> None:
    reg = Registry()
    _two_types(reg, "production")
    router = _router(reg)
    router.refresh()
    metrics = MetricsRecorder("api", 100)
    app = create_app(
        router,
        InMemoryOverrideStore(),
        InMemorySink(),
        metrics,
        serving_cfg(),
        background=False,
        canary_prefix=CFG.canaries.tenant_prefix,
    )
    with TestClient(app) as c:
        assert c.post("/v1/score", json=_body("tenant-0001")).status_code == 200
        canary = canary_tenant("ring0", RINGS, CFG.canaries.tenant_prefix)
        assert c.post("/v1/score", json=_body(canary)).status_code == 200
        bad = {**_body(canary), "features": [0.1]}
        assert c.post("/v1/score", json=bad).status_code == 422
    label = {"model_version": f"{NAME}/1"}
    assert metrics.counter("requests", {**label, "status": "200"}) == 1
    assert metrics.counter("canary_requests", {**label, "status": "200"}) == 1
    assert metrics.counter("canary_requests", {**label, "status": "422"}) == 1
    series = {s.metric: s.count for s in metrics.snapshot() if "latency" in s.metric}
    assert series == {"latency_ms": 1, "canary_latency_ms": 1}


def test_kill_switch_auth() -> None:
    reg = Registry()
    _two_types(reg, "production")
    c, *_ = _api(reg)
    with c:
        assert c.post("/admin/rollback", json={"reason": "x"}).status_code == 401
        bad = c.post("/admin/rollback", json={"reason": "x"}, headers={"X-API-Key": "nope"})
        assert bad.status_code == 401
        assert (
            c.post("/admin/rollback", json={"reason": ""}, headers={"X-API-Key": KEY}).status_code
            == 422
        )
    off, *_ = _api(reg, key=None)
    with off:
        r = off.post("/admin/rollback", json={"reason": "x"}, headers={"X-API-Key": KEY})
        assert r.status_code == 503


def test_kill_and_time_polls_until_rings_serve_production() -> None:
    clock = iter(np.arange(0.0, 100.0, 0.5))
    states = iter(
        [
            {"ring0": {"Win64": "7"}, "ring1": {"Win64": "1"}, "ring2": {"Win64": "1"}},
            {r: {"Win64": "1"} for r in ("ring0", "ring1", "ring2")},
        ]
    )
    res = kill_and_time(
        lambda: {"ok": True},
        lambda: next(states),
        {"Win64": "1"},
        timeout=60,
        poll=0.5,
        clock=lambda: float(next(clock)),
        sleep=lambda _: None,
    )
    assert res["restored"] and res["seconds"] == 1.0
    stuck = kill_and_time(
        lambda: {},
        lambda: {"ring0": {"Win64": "7"}},
        {"Win64": "1"},
        timeout=2,
        poll=0.5,
        clock=lambda: float(next(clock)),
        sleep=lambda _: None,
    )
    assert not stuck["restored"]


# ------------------------------------------------------------------ demo models (real MLflow)


def test_demo_versions_copy_production_and_change_only_the_threshold(tmp_path: Path) -> None:
    from mlflow import MlflowClient

    from sentinel.models.registry import register_derived_version
    from sentinel.serving.models import MlflowModelSource
    from tests.serving_support import register_sentinel_model

    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    v1 = register_sentinel_model(uri, tmp_path, "Win64", 0.9, seed=1, alias="production")
    bad = register_derived_version(
        uri, NAME, v1, tmp_path / "bad", tmp_path / "artifacts", {"demo": "true"}, threshold_raw=0.5
    )
    good = register_derived_version(
        uri, NAME, v1, tmp_path / "good", tmp_path / "artifacts", {"demo": "true"}
    )
    src = MlflowModelSource(uri, {NAME: "Win64"}, predict_threads=1)
    m1, mb, mg = (src.load(NAME, v) for v in (v1, bad, good))
    assert (m1.threshold, mb.threshold, mg.threshold) == (0.9, 0.5, 0.9)
    x = np.random.default_rng(0).normal(size=(20, DIM)).astype(np.float32)
    assert np.array_equal(m1.score(x), mb.score(x)) and np.array_equal(m1.score(x), mg.score(x))
    tags = MlflowClient(tracking_uri=uri).get_model_version(NAME, bad).tags
    assert tags["demo"] == "true" and tags["source_version"] == v1
    assert src.resolve(NAME, "production") == v1  # production untouched, no alias on demos
    with pytest.raises(LookupError):
        src.resolve(NAME, "ring0_model")
