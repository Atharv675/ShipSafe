"""Models served: loaded ONLY via the registry alias, hot-reloaded, with instant rollback.

Design (see docs/DECISIONS.md, Stage 3):
- One slot per file type, holding `current` and `previous` LoadedModel.
- Requests read one immutable dict {feature_schema_version: LoadedModel}. A reload builds a
  NEW dict and rebinds the attribute (atomic in Python), so an in-flight request keeps the
  model it started with and no request ever sees a half-loaded model.
- Loading happens before the swap, outside the lock; a failed load or failed warm-up leaves
  the current model serving.
- If the alias moves back to the version kept as `previous`, the swap is instant (no load).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Protocol

import numpy as np
import numpy.typing as npt

from sentinel.logging import get_logger
from sentinel.models.calibration import IsotonicCalibrator
from sentinel.monitoring.metrics import MetricsRecorder

log = get_logger(__name__)


class Scorer(Protocol):
    def predict(self, data: npt.NDArray[np.float32], num_threads: int = ...) -> Any: ...


@dataclass(frozen=True, eq=False)
class LoadedModel:
    name: str
    version: str
    file_type: str
    feature_schema_version: str
    features: tuple[str, ...]
    threshold: float
    booster: Scorer
    calibrator: IsotonicCalibrator | None = None
    predict_threads: int = 1

    @property
    def label(self) -> str:
        return f"{self.name}/{self.version}"

    @property
    def n_features(self) -> int:
        return len(self.features)

    def score(self, x: npt.NDArray[np.float32]) -> npt.NDArray[np.float64]:
        if x.ndim != 2 or x.shape[1] != self.n_features:
            raise ValueError(f"expected shape (n, {self.n_features}), got {x.shape}")
        return np.asarray(self.booster.predict(x, num_threads=self.predict_threads), np.float64)

    def warm_up(self) -> None:
        """Score one all-zero row: proves the model is usable before it takes traffic."""
        out = self.score(np.zeros((1, self.n_features), dtype=np.float32))
        if out.shape != (1,) or not np.isfinite(out).all():
            raise ValueError(f"{self.label}: warm-up produced {out!r}")

    def describe(self) -> dict[str, Any]:
        return {
            "model": self.name,
            "version": self.version,
            "file_type": self.file_type,
            "feature_schema_version": self.feature_schema_version,
            "n_features": self.n_features,
            "threshold": self.threshold,
        }


class ModelSource(Protocol):
    def resolve(self, name: str, alias: str) -> str:
        """Version the alias points to now."""

    def load(self, name: str, version: str) -> LoadedModel:
        """Load that exact version."""


class MlflowModelSource:
    """Reads the MLflow model registry (sentinel.models.registry.SentinelModel pyfunc)."""

    def __init__(self, tracking_uri: str, file_types: Mapping[str, str], predict_threads: int):
        import mlflow
        from mlflow import MlflowClient

        mlflow.set_tracking_uri(tracking_uri)
        self._client = MlflowClient(tracking_uri=tracking_uri, registry_uri=tracking_uri)
        self._file_types = dict(file_types)  # model name -> file type
        self._threads = predict_threads

    def resolve(self, name: str, alias: str) -> str:
        return str(self._client.get_model_version_by_alias(name, alias).version)

    def load(self, name: str, version: str) -> LoadedModel:
        """Copy the version's artifacts to private temp space, then load from there.

        Loading straight from a `models:/` URI makes MLflow write metadata next to the
        artifacts; serving mounts the registry's file store read-only, so we never do that.
        Everything is read into memory, so the temp copy is removed afterwards."""
        import tempfile

        import mlflow.artifacts
        import mlflow.pyfunc

        with tempfile.TemporaryDirectory(prefix="sentinel-model-") as tmp:
            local = mlflow.artifacts.download_artifacts(
                artifact_uri=f"models:/{name}/{version}", dst_path=tmp
            )
            py = mlflow.pyfunc.load_model(local).unwrap_python_model()
        meta: dict[str, Any] = py.meta
        if meta.get("file_type") != self._file_types[name]:
            raise ValueError(f"{name}/{version} is for {meta.get('file_type')!r}")
        return LoadedModel(
            name=name,
            version=version,
            file_type=str(meta["file_type"]),
            feature_schema_version=str(meta["feature_schema_version"]),
            features=tuple(meta["features"]),
            threshold=float(meta["threshold_raw"]),
            booster=py.booster,
            calibrator=py.calibrator,
            predict_threads=self._threads,
        )


@dataclass
class _Slot:
    file_type: str
    name: str
    current: LoadedModel | None = None
    previous: LoadedModel | None = None
    last_error: str | None = None
    reloads: int = field(default=0)


class ModelManager:
    """Keeps the alias' model per file type in memory; `refresh()` is the hot-reload step."""

    def __init__(
        self,
        source: ModelSource,
        models: Mapping[str, str],
        alias: str,
        metrics: MetricsRecorder | None = None,
    ) -> None:
        """`models` maps file type -> registered model name."""
        self._source = source
        self._alias = alias
        self._metrics = metrics
        self._slots = {ft: _Slot(ft, name) for ft, name in models.items()}
        self._write = threading.Lock()  # serialises swaps; readers never lock
        self._refreshing = threading.Lock()
        self._by_schema: Mapping[str, LoadedModel] = MappingProxyType({})

    # ------------------------------------------------------------------ request path
    def get(self, feature_schema_version: str) -> LoadedModel | None:
        return self._by_schema.get(feature_schema_version)

    @property
    def ready(self) -> bool:
        return all(s.current is not None for s in self._slots.values())

    def expected_schemas(self) -> dict[str, str]:
        return {m.feature_schema_version: m.file_type for m in self._by_schema.values()}

    # ------------------------------------------------------------------ writers
    def _publish(self) -> None:
        by_schema: dict[str, LoadedModel] = {}
        for s in self._slots.values():
            if s.current is None:
                continue
            other = by_schema.get(s.current.feature_schema_version)
            if other is not None:  # pragma: no cover - guarded in _swap_in
                raise ValueError("two file types share a feature_schema_version")
            by_schema[s.current.feature_schema_version] = s.current
        self._by_schema = MappingProxyType(by_schema)  # atomic rebind

    def _swap_in(self, slot: _Slot, model: LoadedModel, how: str) -> None:
        for other in self._slots.values():
            if (
                other is not slot
                and other.current is not None
                and other.current.feature_schema_version == model.feature_schema_version
            ):
                raise ValueError(
                    f"{model.label} has the same feature_schema_version as {other.current.label}"
                )
        old = slot.current
        slot.previous, slot.current = old, model
        slot.reloads += 1
        slot.last_error = None
        self._publish()
        log.info(
            "model_swapped",
            file_type=slot.file_type,
            how=how,
            to_version=model.version,
            from_version=old.version if old else None,
        )
        if self._metrics:
            self._metrics.inc("model_reloads", {"model_version": model.label, "how": how})

    def refresh(self) -> dict[str, str]:
        """Poll the alias for every file type; load and swap where it moved.

        Returns {file_type: outcome}. Never raises: a failure keeps the current model.
        """
        outcomes: dict[str, str] = {}
        with self._refreshing:  # one refresh at a time; slow loads do NOT hold self._write
            for ft, slot in self._slots.items():
                try:
                    version = self._source.resolve(slot.name, self._alias)
                    if slot.current is not None and slot.current.version == version:
                        outcomes[ft] = "unchanged"
                        continue
                    if slot.previous is not None and slot.previous.version == version:
                        with self._write:
                            self._swap_in(slot, slot.previous, "previous_in_memory")
                        outcomes[ft] = f"swapped_to_previous:{version}"
                        continue
                    model = self._source.load(slot.name, version)
                    if model.file_type != ft:
                        raise ValueError(f"{model.label} is a {model.file_type} model")
                    model.warm_up()
                    with self._write:
                        self._swap_in(slot, model, "loaded")
                    outcomes[ft] = f"loaded:{version}"
                except Exception as exc:  # keep serving the current model
                    slot.last_error = f"{type(exc).__name__}: {exc}"
                    outcomes[ft] = "error"
                    log.error("model_reload_failed", file_type=ft, error=slot.last_error)
                    if self._metrics:
                        self._metrics.inc("model_reload_failures", {"file_type": ft})
        return outcomes

    def rollback(self, file_type: str) -> LoadedModel:
        """Instantly serve the previous model again (Stage 6 wires the admin endpoint).

        Note: the next poll re-applies whatever the alias says, so a durable rollback also
        moves the alias (Stage 6 does both)."""
        with self._write:
            slot = self._slots[file_type]
            if slot.previous is None:
                raise LookupError(f"no previous {file_type} model in memory")
            self._swap_in(slot, slot.previous, "rollback")
            assert slot.current is not None
            return slot.current

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "file_type": s.file_type,
                "model": s.name,
                "alias": self._alias,
                "current": s.current.describe() if s.current else None,
                "previous_version": s.previous.version if s.previous else None,
                "reloads": s.reloads,
                "last_error": s.last_error,
            }
            for s in self._slots.values()
        ]
