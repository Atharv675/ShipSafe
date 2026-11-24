"""Small real LightGBM models and registries for serving tests (no dataset needed)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import lightgbm as lgb
import numpy as np
import numpy.typing as npt

from sentinel.config import LineageConfig, OverridesConfig, ServingConfig
from sentinel.models.calibration import IsotonicCalibrator
from sentinel.serving.models import LoadedModel

DIM = 6
FEATURES = tuple(f"f_{i:04d}" for i in range(DIM))
SCHEMA = {"Win64": "schema-win64", "Win32": "schema-win32"}
NAMES = {"Win64": "sentinel-win64", "Win32": "sentinel-win32"}


def booster(seed: int, trees: int = 15) -> lgb.Booster:
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(400, DIM)).astype(np.float32)
    y = (x[:, 0] + 0.3 * rng.normal(size=400) > 0).astype(int)
    params = {
        "objective": "binary",
        "num_leaves": 7,
        "min_data_in_leaf": 5,
        "seed": seed,
        "deterministic": True,
        "num_threads": 1,
        "verbose": -1,
    }
    return lgb.train(params, lgb.Dataset(x, label=y), num_boost_round=trees)


def loaded(
    file_type: str,
    version: str,
    threshold: float = 0.5,
    seed: int = 0,
    schema: str | None = None,
    model: object | None = None,
) -> LoadedModel:
    return LoadedModel(
        name=NAMES[file_type],
        version=version,
        file_type=file_type,
        feature_schema_version=schema or SCHEMA[file_type],
        features=FEATURES,
        threshold=threshold,
        booster=model if model is not None else booster(seed),  # type: ignore[arg-type]
    )


class FakeSource:
    """In-memory registry: alias -> version, (name, version) -> LoadedModel."""

    def __init__(self, load_delay: float = 0.0) -> None:
        self.alias: dict[str, str] = {}
        self.models: dict[tuple[str, str], LoadedModel] = {}
        self.fail: set[tuple[str, str]] = set()
        self.loads = 0
        self.load_delay = load_delay
        self._lock = threading.Lock()

    def add(self, m: LoadedModel, set_alias: bool = True) -> None:
        self.models[(m.name, m.version)] = m
        if set_alias:
            self.alias[m.name] = m.version

    def resolve(self, name: str, alias: str) -> str:
        return self.alias[name]

    def load(self, name: str, version: str) -> LoadedModel:
        if (name, version) in self.fail:
            raise RuntimeError(f"cannot load {name}/{version}")
        time.sleep(self.load_delay)
        with self._lock:
            self.loads += 1
        return self.models[(name, version)]


def serving_cfg(**over: object) -> ServingConfig:
    base: dict[str, object] = {
        "service_name": "api",
        "file_types": ["Win64", "Win32"],
        "model_alias": "production",
        "poll_seconds": 30.0,
        "predict_threads": 1,
        "workers": 1,
        "max_id_length": 64,
        "latency_samples": 1000,
        "lineage": LineageConfig(batch_size=50, flush_seconds=0.05, queue_max=1000, max_retries=1),
        "overrides": OverridesConfig(refresh_seconds=5.0),
    }
    base.update(over)
    return ServingConfig.model_validate(base)


def register_sentinel_model(
    uri: str, work: Path, file_type: str, threshold: float, seed: int, alias: str | None
) -> str:
    """Log + register a real SentinelModel pyfunc (as Stage 2 does) and return its version."""
    import mlflow
    import mlflow.pyfunc
    from mlflow import MlflowClient

    from sentinel.models.registry import SentinelModel

    d = work / f"{file_type}-{seed}"
    d.mkdir(parents=True, exist_ok=True)
    b = booster(seed)
    b.save_model(str(d / "lightgbm.txt"))
    rng = np.random.default_rng(seed)
    raw: npt.NDArray[np.float64] = rng.uniform(size=200)
    IsotonicCalibrator.fit(raw, (raw > 0.5).astype(int)).save(d / "cal.npz")
    meta = {
        "file_type": file_type,
        "features": list(FEATURES),
        "feature_schema_version": SCHEMA[file_type],
        "threshold_raw": threshold,
        "threshold_probability": 0.5,
        "target_fpr": 0.001,
        "data_fingerprint": "test",
    }
    (d / "model_meta.json").write_text(json.dumps(meta), encoding="utf-8")
    mlflow.set_tracking_uri(uri)
    exp = mlflow.get_experiment_by_name("serving-tests")
    exp_id = (
        exp.experiment_id
        if exp
        else mlflow.create_experiment(
            "serving-tests", artifact_location=(work / "artifacts").as_uri()
        )
    )
    with mlflow.start_run(experiment_id=exp_id):
        info = mlflow.pyfunc.log_model(
            name="model",
            python_model=SentinelModel(),
            artifacts={
                "lightgbm": str(d / "lightgbm.txt"),
                "calibration": str(d / "cal.npz"),
                "meta": str(d / "model_meta.json"),
            },
            registered_model_name=NAMES[file_type],
            pip_requirements=["lightgbm"],
        )
    version = str(info.registered_model_version)
    if alias:
        MlflowClient(tracking_uri=uri).set_registered_model_alias(NAMES[file_type], alias, version)
    return version
