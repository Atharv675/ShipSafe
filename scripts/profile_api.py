"""Stage 8: where does a /v1/score request spend its time? (real models, in-process)

    python scripts/profile_api.py [--n 1000]        (make profile-api; compose network)

Loads the production models from the MLflow server exactly like the API does, then times,
per request: JSON decode, request validation, feature array, model score, and the whole
handler through FastAPI's TestClient (no network). Prints a cProfile top list as well.
Uses the load-test bodies (make load-bodies). Writes nothing to the database.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import time
from pathlib import Path
from typing import Any

import numpy as np

from sentinel.config import (
    Settings,
    load_monitoring_config,
    load_replay_config,
    load_rollout_config,
    load_serving_config,
    load_train_config,
)
from sentinel.monitoring.metrics import MetricsRecorder
from sentinel.serving.app import ScoreRequest, create_app
from sentinel.serving.lineage import PredictionRecord
from sentinel.serving.models import MlflowModelSource
from sentinel.serving.overrides import InMemoryOverrideStore
from sentinel.serving.rings import RingRouter


class NullSink:
    def submit(self, record: PredictionRecord) -> None:
        return None

    def close(self) -> None:
        return None


def _ms(samples: list[float]) -> dict[str, float]:
    a = np.asarray(samples) * 1000
    return {
        "mean": round(float(a.mean()), 3),
        "p50": round(float(np.percentile(a, 50)), 3),
        "p99": round(float(np.percentile(a, 99)), 3),
    }


def main() -> None:
    from fastapi.testclient import TestClient

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n", type=int, default=1000)
    p.add_argument("--bodies", type=Path, default=Path("/data/loadtest/bodies_Win64.jsonl"))
    args = p.parse_args()
    settings = Settings()
    cfg = load_serving_config()
    names = {ft: load_train_config().mlflow.model_name(ft) for ft in cfg.file_types}
    metrics = MetricsRecorder(
        cfg.service_name, cfg.latency_samples, load_monitoring_config().metrics.window_max_samples
    )
    source = MlflowModelSource(
        settings.tracking_uri, {n: ft for ft, n in names.items()}, cfg.predict_threads
    )
    router = RingRouter.build(
        source, names, load_rollout_config(), load_replay_config().rings, metrics
    )
    t0 = time.perf_counter()
    router.refresh()
    load_s = time.perf_counter() - t0
    overrides = InMemoryOverrideStore()
    app = create_app(router, overrides, NullSink(), metrics, cfg, background=False)
    bodies = [b for b in args.bodies.read_bytes().splitlines() if b.strip()][: args.n]
    parsed = [json.loads(b) for b in bodies]
    _, model = router.get(parsed[0]["tenant_id"], parsed[0]["feature_schema_version"])
    assert model is not None, "production model not loaded"

    parts: dict[str, list[float]] = {
        "json_decode": [],
        "validate": [],
        "to_array": [],
        "model_score": [],
    }
    for raw in bodies:
        a = time.perf_counter()
        json.loads(raw)
        b = time.perf_counter()
        req = ScoreRequest.model_validate_json(raw)
        c = time.perf_counter()
        x = np.asarray(req.features, dtype=np.float32)
        d = time.perf_counter()
        model.score(x.reshape(1, -1))
        e = time.perf_counter()
        parts["json_decode"].append(b - a)
        parts["validate"].append(c - b)
        parts["to_array"].append(d - c)
        parts["model_score"].append(e - d)

    handler: list[float] = []
    headers = {"content-type": "application/json"}
    with TestClient(app) as client:
        for raw in bodies[:50]:  # warm-up
            client.post("/v1/score", content=raw, headers=headers)
        prof = cProfile.Profile()
        prof.enable()
        for raw in bodies:
            a = time.perf_counter()
            r = client.post("/v1/score", content=raw, headers=headers)
            handler.append(time.perf_counter() - a)
            assert r.status_code == 200, r.text
        prof.disable()
    server = [s for s in metrics.snapshot() if s.metric == "latency_ms"]
    buf = io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(18)
    out: dict[str, Any] = {
        "requests": len(bodies),
        "model": model.label,
        "model_trees": int(model.booster.num_trees()),
        "features": model.n_features,
        "models_load_seconds": round(load_s, 1),
        "per_request_ms": {k: _ms(v) for k, v in parts.items()},
        "testclient_round_trip_ms": _ms(handler),
        "handler_ms_from_metrics": {
            s.labels["model_version"]: {"p50": s.p50, "p99": s.p99} for s in server
        },
    }
    print(json.dumps(out, indent=1))
    print(buf.getvalue())


if __name__ == "__main__":
    main()
