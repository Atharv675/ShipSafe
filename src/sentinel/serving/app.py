"""FastAPI scoring service.

    POST /v1/score   {file_id, tenant_id, features[], feature_schema_version}
                  -> {score, verdict, model_version, threshold, override_applied, request_id}

- The model is chosen by `feature_schema_version` (each file type's model has its own);
  a version no loaded model serves -> HTTP 409, never a silent score.
- `features` must be in the model's column order (the schema version pins that order).
- score = the model's raw score; verdict = "malicious" if score > threshold, then tenant
  overrides win.
Run: uvicorn --factory sentinel.serving.app:build_app
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from sentinel.config import ServingConfig
from sentinel.logging import get_logger
from sentinel.monitoring.metrics import MetricsRecorder
from sentinel.serving.lineage import PredictionRecord, PredictionSink
from sentinel.serving.models import ModelManager
from sentinel.serving.overrides import OverrideStore, apply_override

log = get_logger(__name__)

Verdict = Literal["malicious", "benign"]
MALICIOUS: Verdict = "malicious"
BENIGN: Verdict = "benign"


class ScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    features: list[float] = Field(min_length=1)
    feature_schema_version: str = Field(min_length=1)


class ScoreResponse(BaseModel):
    score: float
    verdict: Verdict
    model_version: str
    threshold: float
    override_applied: bool
    request_id: str


def _every(seconds: float, fn: Callable[[], object], stop: threading.Event, name: str) -> None:
    def loop() -> None:
        while not stop.is_set():
            try:
                fn()
            except Exception as exc:  # the job logs its own details; keep the loop alive
                log.error("background_job_failed", job=name, error=f"{type(exc).__name__}")
            stop.wait(seconds)

    threading.Thread(target=loop, name=name, daemon=True).start()


def create_app(
    models: ModelManager,
    overrides: OverrideStore,
    sink: PredictionSink,
    metrics: MetricsRecorder,
    cfg: ServingConfig,
    *,
    background: bool = True,
) -> FastAPI:
    """`background=False` (tests) means no poll threads: call models.refresh() yourself."""
    stop = threading.Event()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        if background:
            _every(cfg.poll_seconds, models.refresh, stop, "model-poller")
            _every(cfg.overrides.refresh_seconds, overrides.refresh, stop, "overrides-refresh")
        try:
            yield
        finally:
            stop.set()
            sink.close()

    app = FastAPI(title="Sentinel Gate scoring", version="1", lifespan=lifespan)

    def _fail(status: int, label: str, detail: dict[str, Any]) -> HTTPException:
        metrics.inc("requests", {"model_version": label, "status": str(status)})
        if status >= 500:
            metrics.inc("errors", {"model_version": label})
        return HTTPException(status_code=status, detail=detail)

    @app.post("/v1/score", response_model=ScoreResponse)
    def score(req: ScoreRequest) -> ScoreResponse:  # sync: runs in the threadpool
        t0 = time.perf_counter()
        request_id = uuid.uuid4().hex
        for field_name in ("file_id", "tenant_id"):
            if len(getattr(req, field_name)) > cfg.max_id_length:
                raise _fail(422, "none", {"error": f"{field_name} longer than limit"})
        model = models.get(req.feature_schema_version)
        if model is None:
            if not models.ready:
                raise _fail(503, "none", {"error": "models not loaded yet"})
            raise _fail(
                409,
                "none",
                {
                    "error": "feature_schema_mismatch",
                    "message": "no served model uses this feature_schema_version; "
                    "re-extract features with the schema of the model you want",
                    "got": req.feature_schema_version,
                    "served": models.expected_schemas(),
                },
            )
        label = model.label
        if not overrides.loaded:
            raise _fail(503, label, {"error": "tenant overrides not loaded yet"})
        with np.errstate(over="ignore"):  # too-large values become inf, rejected below
            x = np.asarray(req.features, dtype=np.float32)
        if x.shape[0] != model.n_features:
            raise _fail(
                422,
                label,
                {
                    "error": "wrong_feature_count",
                    "expected": model.n_features,
                    "got": int(x.shape[0]),
                },
            )
        if not np.isfinite(x).all():
            raise _fail(422, label, {"error": "non_finite_features"})
        try:
            raw = float(model.score(x.reshape(1, -1))[0])
        except Exception as exc:
            log.error("scoring_failed", model_version=label, error=f"{type(exc).__name__}: {exc}")
            raise _fail(500, label, {"error": "scoring_failed", "request_id": request_id}) from exc
        model_malicious = raw > model.threshold
        final, applied = apply_override(
            model_malicious, overrides.lookup(req.tenant_id, req.file_id)
        )
        if applied and final != model_malicious:
            log.info(
                "override_changed_verdict",
                request_id=request_id,
                tenant_id=req.tenant_id,
                file_id=req.file_id,
                model_version=label,
                model_verdict=MALICIOUS if model_malicious else BENIGN,
                final_verdict=MALICIOUS if final else BENIGN,
            )
            metrics.inc("override_changed_verdict", {"tenant_id": req.tenant_id})
        verdict: Verdict = MALICIOUS if final else BENIGN
        sink.submit(
            PredictionRecord(
                request_id=request_id,
                file_id=req.file_id,
                tenant_id=req.tenant_id,
                model_version=label,
                feature_schema_version=req.feature_schema_version,
                threshold=model.threshold,
                score=raw,
                verdict=verdict,
                override_applied=applied,
                ts=datetime.now(UTC),
            )
        )
        metrics.inc("requests", {"model_version": label, "status": "200"})
        metrics.observe("latency_ms", (time.perf_counter() - t0) * 1000, {"model_version": label})
        return ScoreResponse(
            score=raw,
            verdict=verdict,
            model_version=label,
            threshold=model.threshold,
            override_applied=applied,
            request_id=request_id,
        )

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        ok = models.ready and overrides.loaded
        body = {"ready": ok, "models": models.ready, "overrides": overrides.loaded}
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/v1/models")
    def list_models() -> list[dict[str, Any]]:
        return models.status()

    @app.get("/v1/ops/stats")
    def stats() -> list[dict[str, Any]]:
        return [s.as_dict() for s in metrics.snapshot()]

    return app


def build_app() -> FastAPI:  # pragma: no cover - wiring, exercised by `make up` + integration
    """Production wiring from .env + configs/*.yaml (used by docker compose)."""
    from sentinel.config import Settings, load_serving_config, load_train_config
    from sentinel.logging import configure_logging
    from sentinel.serving.lineage import BatchedLineageWriter, postgres_batch_writer
    from sentinel.serving.models import MlflowModelSource
    from sentinel.serving.overrides import PostgresOverrideStore

    settings = Settings()
    configure_logging(settings.log_level)
    cfg = load_serving_config()
    names = {ft: load_train_config().mlflow.model_name(ft) for ft in cfg.file_types}
    if not settings.pg_dsn:
        raise SystemExit("SENTINEL_PG_DSN is not set")
    metrics = MetricsRecorder(cfg.service_name, cfg.latency_samples)
    source = MlflowModelSource(
        settings.tracking_uri, {n: ft for ft, n in names.items()}, cfg.predict_threads
    )
    manager = ModelManager(source, names, cfg.model_alias, metrics)
    store = PostgresOverrideStore(settings.pg_dsn)
    sink = BatchedLineageWriter(postgres_batch_writer(settings.pg_dsn), cfg.lineage, metrics)
    return create_app(manager, store, sink, metrics, cfg)
