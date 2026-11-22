"""Stage 3 serving: scoring, 409 on schema mismatch, overrides, hot reload, rollback."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from sentinel.config import load_serving_config
from sentinel.monitoring.metrics import MetricsRecorder
from sentinel.serving.app import create_app
from sentinel.serving.lineage import InMemorySink
from sentinel.serving.models import ModelManager
from sentinel.serving.overrides import (
    Action,
    InMemoryOverrideStore,
    Override,
    apply_override,
)
from tests.serving_support import (
    DIM,
    NAMES,
    SCHEMA,
    FakeSource,
    booster,
    loaded,
    register_sentinel_model,
    serving_cfg,
)

ROW = [0.5, -0.2, 0.1, 0.0, 1.0, -1.0]


def _body(ft: str = "Win64", **over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "file_id": "file-1",
        "tenant_id": "tenant-a",
        "features": ROW,
        "feature_schema_version": SCHEMA[ft],
    }
    body.update(over)
    return body


class Stack:
    def __init__(
        self,
        source: FakeSource | None = None,
        overrides: InMemoryOverrideStore | None = None,
        refresh: bool = True,
        **cfg_over: Any,
    ) -> None:
        self.source = source or FakeSource()
        if source is None:
            self.source.add(loaded("Win64", "1", seed=1))
            self.source.add(loaded("Win32", "1", seed=2))
        self.metrics = MetricsRecorder("api", 1000)
        self.cfg = serving_cfg(**cfg_over)
        self.manager = ModelManager(self.source, NAMES, "production", self.metrics)
        self.overrides = overrides or InMemoryOverrideStore()
        self.sink = InMemorySink()
        self.app = create_app(
            self.manager, self.overrides, self.sink, self.metrics, self.cfg, background=False
        )
        if refresh:
            assert self.manager.refresh() == {"Win64": "loaded:1", "Win32": "loaded:1"}

    def client(self) -> TestClient:
        return TestClient(self.app)


# ------------------------------------------------------------------ scoring contract


def test_score_returns_contract_and_writes_lineage() -> None:
    s = Stack()
    with s.client() as c:
        r = c.post("/v1/score", json=_body())
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == {
        "score",
        "verdict",
        "model_version",
        "threshold",
        "override_applied",
        "request_id",
    }
    assert out["model_version"] == "sentinel-win64/1"
    assert out["threshold"] == 0.5
    assert 0.0 < out["score"] < 1.0
    assert out["verdict"] == ("malicious" if out["score"] > 0.5 else "benign")
    assert out["override_applied"] is False
    expected = float(booster(1).predict(np.asarray([ROW], np.float32))[0])
    assert out["score"] == pytest.approx(expected, rel=1e-6)
    [rec] = s.sink.records
    assert rec.request_id == out["request_id"]
    assert (rec.file_id, rec.tenant_id, rec.model_version) == (
        "file-1",
        "tenant-a",
        "sentinel-win64/1",
    )
    assert rec.feature_schema_version == SCHEMA["Win64"]
    assert (rec.score, rec.verdict, rec.threshold) == (out["score"], out["verdict"], 0.5)
    assert rec.ts.tzinfo is not None
    assert s.metrics.counter("requests", {"model_version": "sentinel-win64/1", "status": "200"})


def test_requests_are_routed_by_feature_schema_version() -> None:
    s = Stack()
    with s.client() as c:
        r64 = c.post("/v1/score", json=_body("Win64")).json()
        r32 = c.post("/v1/score", json=_body("Win32")).json()
    assert r64["model_version"] == "sentinel-win64/1"
    assert r32["model_version"] == "sentinel-win32/1"
    assert r64["score"] != r32["score"]


def test_schema_mismatch_is_409_and_never_scored() -> None:
    s = Stack()
    with s.client() as c:
        r = c.post("/v1/score", json=_body(feature_schema_version="old-schema"))
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["error"] == "feature_schema_mismatch"
    assert d["got"] == "old-schema"
    assert d["served"] == {SCHEMA["Win64"]: "Win64", SCHEMA["Win32"]: "Win32"}
    assert s.sink.records == []
    assert s.metrics.counter("requests", {"model_version": "none", "status": "409"}) == 1


@pytest.mark.parametrize(
    ("features", "error"),
    [
        (ROW[:-1], "wrong_feature_count"),
        ([*ROW, 0.0], "wrong_feature_count"),
        ([*ROW[:-1], float("nan")], "non_finite_features"),
        ([*ROW[:-1], 1e300], "non_finite_features"),  # overflows float32
    ],
)
def test_bad_feature_vectors_are_422(features: list[float], error: str) -> None:
    s = Stack()
    with s.client() as c:
        r = c.post(
            "/v1/score",
            content=json.dumps(_body(features=features)),
            headers={"content-type": "application/json"},
        )
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == error
    assert s.sink.records == []


def test_request_validation() -> None:
    s = Stack(max_id_length=8)
    with s.client() as c:
        assert c.post("/v1/score", json=_body(file_id="x" * 9)).status_code == 422
        assert c.post("/v1/score", json=_body(file_id="")).status_code == 422
        assert c.post("/v1/score", json=_body(extra=1)).status_code == 422
        missing = _body()
        del missing["tenant_id"]
        assert c.post("/v1/score", json=missing).status_code == 422


def test_not_ready_until_models_and_overrides_load() -> None:
    s = Stack(refresh=False, overrides=InMemoryOverrideStore(loaded=False))
    with s.client() as c:
        assert c.get("/healthz").json() == {"status": "ok"}
        assert c.get("/readyz").status_code == 503
        assert c.post("/v1/score", json=_body()).status_code == 503
        s.manager.refresh()
        r = c.post("/v1/score", json=_body())
        assert r.status_code == 503
        assert "overrides" in r.json()["detail"]["error"]
        s.overrides.refresh()
        assert c.get("/readyz").json() == {"ready": True, "models": True, "overrides": True}
        assert c.post("/v1/score", json=_body()).status_code == 200


def test_scoring_error_is_500_and_counted() -> None:
    class Broken:
        calls = 0

        def predict(self, data: Any, num_threads: int = 1) -> Any:
            Broken.calls += 1
            if Broken.calls > 1:  # the warm-up passes, real requests fail
                raise RuntimeError("boom")
            return np.zeros(len(data))

    src = FakeSource()
    src.add(loaded("Win64", "1", model=Broken()))
    src.add(loaded("Win32", "1", seed=2))
    s = Stack(source=src)
    with s.client() as c:
        r = c.post("/v1/score", json=_body())
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "scoring_failed"
    assert s.metrics.counter("errors", {"model_version": "sentinel-win64/1"}) == 1


def test_models_and_stats_endpoints() -> None:
    s = Stack()
    with s.client() as c:
        c.post("/v1/score", json=_body())
        models = c.get("/v1/models").json()
        stats = c.get("/v1/ops/stats").json()
    # one entry per (ring, file type); a bare ModelManager serves every ring (Stage 3 mode)
    assert [(m["ring"], m["file_type"]) for m in models] == [
        (r, ft) for r in ("ring0", "ring1", "ring2") for ft in ("Win64", "Win32")
    ]
    assert [m["current"]["version"] for m in models] == ["1"] * 6
    assert models[0]["current"]["n_features"] == DIM
    lat = [x for x in stats if x["metric"] == "latency_ms"]
    assert lat
    assert lat[0]["labels"] == {"model_version": "sentinel-win64/1"}
    assert lat[0]["count"] == 1


# ------------------------------------------------------------------ overrides


def test_apply_override_always_wins() -> None:
    allow = Override("t", "f", Action.ALLOW, "vendor tool")
    block = Override("t", "f", Action.BLOCK, "known bad")
    assert apply_override(True, None) == (True, False)
    assert apply_override(False, None) == (False, False)
    assert apply_override(True, allow) == (False, True)
    assert apply_override(False, allow) == (False, True)
    assert apply_override(True, block) == (True, True)
    assert apply_override(False, block) == (True, True)


def test_allow_listed_file_is_never_blocked() -> None:
    # threshold 0.0: the model flags EVERY file; the allow-list must still win.
    src = FakeSource()
    src.add(loaded("Win64", "1", threshold=0.0, seed=1))
    src.add(loaded("Win32", "1", seed=2))
    store = InMemoryOverrideStore([Override("tenant-a", "good.exe", Action.ALLOW, "signed tool")])
    s = Stack(source=src, overrides=store)
    rng = np.random.default_rng(7)
    with s.client() as c:
        for _ in range(50):
            feats = (rng.normal(size=DIM) * 5).tolist()
            r = c.post("/v1/score", json=_body(file_id="good.exe", features=feats)).json()
            assert (r["verdict"], r["override_applied"]) == ("benign", True)
        other_tenant = c.post(
            "/v1/score", json=_body(file_id="good.exe", tenant_id="tenant-b")
        ).json()
        other_file = c.post("/v1/score", json=_body(file_id="other.exe")).json()
    assert (other_tenant["verdict"], other_tenant["override_applied"]) == ("malicious", False)
    assert (other_file["verdict"], other_file["override_applied"]) == ("malicious", False)
    assert all(
        r.verdict == "benign"
        for r in s.sink.records
        if r.file_id == "good.exe" and r.tenant_id == "tenant-a"
    )
    assert s.metrics.counter("override_changed_verdict", {"tenant_id": "tenant-a"}) == 50


def test_block_list_flags_file_the_model_calls_benign() -> None:
    src = FakeSource()
    src.add(loaded("Win64", "1", threshold=1.0, seed=1))  # flags nothing
    src.add(loaded("Win32", "1", seed=2))
    store = InMemoryOverrideStore()
    s = Stack(source=src, overrides=store)
    with s.client() as c:
        assert c.post("/v1/score", json=_body(file_id="bad.exe")).json()["verdict"] == "benign"
        store.put(Override("tenant-a", "bad.exe", Action.BLOCK, "incident 42"))
        r = c.post("/v1/score", json=_body(file_id="bad.exe")).json()
    assert (r["verdict"], r["override_applied"]) == ("malicious", True)
    assert s.sink.records[-1].override_applied is True


# ------------------------------------------------------------------ hot reload / rollback


def test_refresh_reloads_only_when_alias_moves_and_keeps_previous() -> None:
    s = Stack()
    assert s.manager.refresh() == {"Win64": "unchanged", "Win32": "unchanged"}
    assert s.source.loads == 2
    s.source.add(loaded("Win64", "2", seed=3))
    assert s.manager.refresh()["Win64"] == "loaded:2"
    assert s.source.loads == 3
    st = {m["file_type"]: m for m in s.manager.status()}
    assert (st["Win64"]["current"]["version"], st["Win64"]["previous_version"]) == ("2", "1")
    s.source.alias[NAMES["Win64"]] = "1"  # alias moved back: instant, no load
    assert s.manager.refresh()["Win64"] == "swapped_to_previous:1"
    assert s.source.loads == 3
    assert s.manager.get(SCHEMA["Win64"]).version == "1"  # type: ignore[union-attr]


def test_failed_load_keeps_serving_current_model() -> None:
    s = Stack()
    s.source.add(loaded("Win64", "2", seed=3))
    s.source.fail.add((NAMES["Win64"], "2"))
    assert s.manager.refresh()["Win64"] == "error"
    assert s.manager.get(SCHEMA["Win64"]).version == "1"  # type: ignore[union-attr]
    assert "cannot load" in s.manager.status()[0]["last_error"]
    assert s.metrics.counter("model_reload_failures", {"file_type": "Win64"}) == 1
    with s.client() as c:
        assert c.post("/v1/score", json=_body()).json()["model_version"] == "sentinel-win64/1"


def test_bad_candidates_are_refused() -> None:
    class NaNModel:
        def predict(self, data: Any, num_threads: int = 1) -> Any:
            return np.full(len(data), np.nan)

    s = Stack()
    s.source.add(loaded("Win64", "2", model=NaNModel()))  # warm-up fails
    assert s.manager.refresh()["Win64"] == "error"
    s.source.add(loaded("Win64", "3", schema=SCHEMA["Win32"], seed=4))  # schema clash
    assert s.manager.refresh()["Win64"] == "error"
    wrong = loaded("Win32", "4", seed=5)
    s.source.models[(NAMES["Win64"], "4")] = wrong  # a Win32 model under the Win64 name
    s.source.alias[NAMES["Win64"]] = "4"
    assert s.manager.refresh()["Win64"] == "error"
    assert s.manager.get(SCHEMA["Win64"]).version == "1"  # type: ignore[union-attr]


def test_rollback_is_instant_and_needs_a_previous_model() -> None:
    s = Stack()
    with pytest.raises(LookupError):
        s.manager.rollback("Win64")
    s.source.add(loaded("Win64", "2", seed=3))
    s.manager.refresh()
    loads = s.source.loads
    t0 = time.perf_counter()
    m = s.manager.rollback("Win64")
    assert time.perf_counter() - t0 < 0.05
    assert m.version == "1"
    assert s.source.loads == loads
    assert s.manager.get(SCHEMA["Win64"]).version == "1"  # type: ignore[union-attr]


def test_background_poller_picks_up_alias_change() -> None:
    src = FakeSource()
    src.add(loaded("Win64", "1", seed=1))
    src.add(loaded("Win32", "1", seed=2))
    metrics = MetricsRecorder("api", 100)
    manager = ModelManager(src, NAMES, "production", metrics)
    cfg = serving_cfg(poll_seconds=0.05)
    app = create_app(manager, InMemoryOverrideStore(), InMemorySink(), metrics, cfg)
    with TestClient(app) as c:
        deadline = time.monotonic() + 5
        while c.get("/readyz").status_code != 200 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert c.post("/v1/score", json=_body()).json()["model_version"] == "sentinel-win64/1"
        src.add(loaded("Win64", "2", seed=3))
        while manager.get(SCHEMA["Win64"]).version != "2" and time.monotonic() < deadline:  # type: ignore[union-attr]
            time.sleep(0.02)
        assert c.post("/v1/score", json=_body()).json()["model_version"] == "sentinel-win64/2"


async def _burst(app: Any, n: int, reload: Any) -> list[httpx.Response]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
        rng = np.random.default_rng(n)
        reqs = [
            client.post(
                "/v1/score",
                json=_body(file_id=f"f{i}", features=rng.normal(size=DIM).tolist()),
            )
            for i in range(n)
        ]
        results = await asyncio.gather(*reqs, asyncio.to_thread(reload))
    return list(results[:-1])  # type: ignore[arg-type]


def test_hot_reload_under_200_concurrent_requests_has_zero_errors(tmp_path: Path) -> None:
    """Real MLflow registry: move the alias v1 -> v2 -> v1 while 200 requests are in flight."""
    from sentinel.serving.models import MlflowModelSource

    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    v1 = register_sentinel_model(uri, tmp_path, "Win64", 0.5, seed=1, alias="production")
    v2 = register_sentinel_model(uri, tmp_path, "Win64", 0.4, seed=9, alias=None)
    register_sentinel_model(uri, tmp_path, "Win32", 0.5, seed=2, alias="production")
    from mlflow import MlflowClient

    client = MlflowClient(tracking_uri=uri)
    source = MlflowModelSource(uri, {v: k for k, v in NAMES.items()}, predict_threads=1)
    metrics = MetricsRecorder("api", 5000)
    manager = ModelManager(source, NAMES, "production", metrics)
    assert manager.refresh() == {"Win64": f"loaded:{v1}", "Win32": "loaded:1"}
    app = create_app(
        manager, InMemoryOverrideStore(), InMemorySink(), metrics, serving_cfg(), background=False
    )

    seen: set[str] = set()
    for target in (v2, v1, v2):
        client.set_registered_model_alias(NAMES["Win64"], "production", target)
        responses = asyncio.run(_burst(app, 200, manager.refresh))
        codes = [r.status_code for r in responses]
        assert codes.count(200) == 200, sorted(set(codes))
        seen |= {r.json()["model_version"] for r in responses}
        assert manager.get(SCHEMA["Win64"]).version == target  # type: ignore[union-attr]
    assert {f"sentinel-win64/{v1}", f"sentinel-win64/{v2}"} <= seen
    assert metrics.counter("errors", {"model_version": f"sentinel-win64/{v1}"}) == 0
    assert metrics.counter("errors", {"model_version": f"sentinel-win64/{v2}"}) == 0
    st = {m["file_type"]: m for m in manager.status()}
    assert st["Win64"]["current"]["threshold"] == 0.4


def test_serving_config_file_is_valid() -> None:
    cfg = load_serving_config()
    assert cfg.poll_seconds == 30
    assert cfg.model_alias == "production"
    assert set(cfg.file_types) == {"Win64", "Win32"}


def test_mlflow_source_loads_from_a_read_only_artifact_store(tmp_path: Path) -> None:
    """Compose mounts the registry's file store read-only into the api (regression)."""
    import os
    import stat

    from sentinel.serving.models import MlflowModelSource

    uri = f"sqlite:///{(tmp_path / 'mlflow.db').as_posix()}"
    v = register_sentinel_model(uri, tmp_path, "Win64", 0.5, seed=1, alias="production")
    store = tmp_path / "artifacts"
    for root, dirs, files in os.walk(store):
        for n in [*dirs, *files]:
            p = Path(root) / n
            p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    store.chmod(0o555)
    try:
        source = MlflowModelSource(uri, {NAMES["Win64"]: "Win64"}, predict_threads=1)
        m = source.load(NAMES["Win64"], source.resolve(NAMES["Win64"], "production"))
    finally:
        for root, dirs, _ in os.walk(store):
            for n in dirs:
                (Path(root) / n).chmod(0o755)
        store.chmod(0o755)
    assert (m.version, m.file_type, m.threshold) == (v, "Win64", 0.5)
    assert m.feature_schema_version == SCHEMA["Win64"]
    m.warm_up()
    assert m.calibrator is not None
