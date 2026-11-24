"""FastAPI scoring service.

    POST /v1/score   {file_id, tenant_id, features[], feature_schema_version}
                  -> {score, verdict, model_version, threshold, override_applied, request_id}

- The model is chosen by `feature_schema_version` (each file type's model has its own);
  a version no loaded model serves -> HTTP 409, never a silent score.
- `features` must be in the model's column order (the schema version pins that order).
- score = the model's raw score; verdict = "malicious" if score > threshold, then tenant
  overrides win.
- Stage 6: the tenant's ring (stable hash of tenant_id) picks the ring's model alias; the
  ring is stored with the lineage row. POST /admin/rollback (header X-API-Key) is the kill
  switch: every ring back to `production` at once.
Run: uvicorn --factory sentinel.serving.app:build_app
"""

from __future__ import annotations

import hmac
import os
import threading
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import numpy as np
import numpy.typing as npt
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, StatusCode, Tracer
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from sentinel.config import RingsConfig, ServingConfig
from sentinel.logging import get_logger
from sentinel.monitoring.metrics import MetricsRecorder, OpsMetricsWriter
from sentinel.monitoring.tracing import context_from_headers, trace_id_of
from sentinel.serving.lineage import PredictionRecord, PredictionSink
from sentinel.serving.models import LoadedModel, ModelManager
from sentinel.serving.overrides import OverrideStore, apply_override
from sentinel.serving.rings import RingRouter

KillSwitch = Callable[[str], dict[str, Any]]  # reason -> per-file-type result
WORKER_HEADER = "X-Sentinel-Worker"  # which API worker process answered (Stage 8)

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
    event_id: str | None = Field(default=None, min_length=1)  # stream events: idempotency key


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)


# Used when tests pass a bare ModelManager: every ring is served by it (Stage 3 behaviour).
_DEFAULT_RINGS = RingsConfig(ring0_percent=5, ring1_percent=25)


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


def _reference_score(
    router: RingRouter,
    model: LoadedModel,
    schema: str,
    x: npt.NDArray[np.float32],
    request_id: str,
) -> tuple[str | None, float | None, str | None]:
    """Stage 7 paired scoring: production's score for the same file when this ring serves
    another model. Never fails the request: a failed reference score is logged and skipped
    (the rollout sees it as missing coverage, not as a disagreement)."""
    ref = router.reference_for(model, schema)
    if ref is None:
        return None, None, None
    try:
        s = float(ref.score(x.reshape(1, -1))[0])
    except Exception as exc:
        log.error(
            "reference_scoring_failed",
            request_id=request_id,
            model_version=ref.label,
            error=type(exc).__name__,
        )
        return None, None, None
    return ref.label, s, MALICIOUS if s > ref.threshold else BENIGN


def create_app(
    models: ModelManager | RingRouter,
    overrides: OverrideStore,
    sink: PredictionSink,
    metrics: MetricsRecorder,
    cfg: ServingConfig,
    *,
    background: bool = True,
    kill_switch: KillSwitch | None = None,
    admin_api_key: str | None = None,
    canary_prefix: str | None = None,
    ops_writer: OpsMetricsWriter | None = None,
    tracer: Tracer | None = None,
) -> FastAPI:
    """`background=False` (tests) means no poll threads: call models.refresh() yourself.
    Stage 8: `ops_writer` (started with the background jobs) flushes the metrics to
    ops_metrics every 10 s; `tracer` defaults to the global (no-op unless set up).
    The kill switch is enabled only when both `kill_switch` and `admin_api_key` are given.
    Requests from tenants starting with `canary_prefix` (the rollout's synthetic never-break
    canaries) are counted under `canary_*` metrics, so they never distort the real-traffic
    request, error and latency figures the rollout watches."""
    stop = threading.Event()
    router = models if isinstance(models, RingRouter) else RingRouter.single(models, _DEFAULT_RINGS)
    tracer = tracer or trace.get_tracer("sentinel.serving")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
        if background:
            _every(cfg.poll_seconds, router.refresh, stop, "model-poller")
            _every(cfg.overrides.refresh_seconds, overrides.refresh, stop, "overrides-refresh")
            if ops_writer is not None:
                ops_writer.start()
        try:
            yield
        finally:
            stop.set()
            sink.close()
            if ops_writer is not None:
                ops_writer.close()

    app = FastAPI(title="Sentinel Gate scoring", version="1", lifespan=lifespan)

    def _fail(status: int, label: str, detail: dict[str, Any], kind: str = "") -> HTTPException:
        metrics.inc(f"{kind}requests", {"model_version": label, "status": str(status)})
        if status >= 500:
            metrics.inc(f"{kind}errors", {"model_version": label})
        return HTTPException(status_code=status, detail=detail)

    @app.post(
        "/v1/score",
        response_model=ScoreResponse,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": ScoreRequest.model_json_schema()}},
            }
        },
    )
    async def score(request: Request) -> ScoreResponse:
        # Stage 8 (load test): the body (~2,500 numbers) is decoded and validated in one pass
        # by pydantic's native JSON validator. FastAPI's default (stdlib json.loads, then
        # validation of the Python objects) cost ~4x the CPU. Same 422 on a bad body.
        body = await request.body()
        try:
            req = ScoreRequest.model_validate_json(body)
        except ValidationError as exc:
            raise HTTPException(
                status_code=422, detail=exc.errors(include_url=False, include_input=False)
            ) from exc
        return await run_in_threadpool(_traced_score, req, dict(request.headers))

    def _traced_score(req: ScoreRequest, headers: dict[str, str]) -> ScoreResponse:
        # Stage 8: continue the caller's trace (live consumer `consume` span) if it sent one
        with tracer.start_as_current_span(
            "score",
            context=context_from_headers(headers),
            kind=SpanKind.SERVER,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                return _score(req, span)
            except HTTPException as exc:
                span.set_attribute("http.status_code", exc.status_code)
                if exc.status_code >= 500:
                    span.set_status(StatusCode.ERROR)
                raise

    def _score(req: ScoreRequest, span: Span) -> ScoreResponse:
        t0 = time.perf_counter()
        request_id = uuid.uuid4().hex
        span.set_attribute("sentinel.request_id", request_id)
        kind = "canary_" if canary_prefix and req.tenant_id.startswith(canary_prefix) else ""
        for field_name in ("file_id", "tenant_id", "event_id"):
            if len(getattr(req, field_name) or "") > cfg.max_id_length:
                raise _fail(422, "none", {"error": f"{field_name} longer than limit"}, kind)
        ring, model = router.get(req.tenant_id, req.feature_schema_version)
        if model is None:
            if not router.ready:
                raise _fail(503, "none", {"error": "models not loaded yet"}, kind)
            raise _fail(
                409,
                "none",
                {
                    "error": "feature_schema_mismatch",
                    "message": "no served model uses this feature_schema_version; "
                    "re-extract features with the schema of the model you want",
                    "got": req.feature_schema_version,
                    "served": router.expected_schemas(),
                },
                kind,
            )
        label = model.label
        if not overrides.loaded:
            raise _fail(503, label, {"error": "tenant overrides not loaded yet"}, kind)
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
                kind,
            )
        if not np.isfinite(x).all():
            raise _fail(422, label, {"error": "non_finite_features"}, kind)
        try:
            raw = float(model.score(x.reshape(1, -1))[0])
        except Exception as exc:
            log.error("scoring_failed", model_version=label, error=f"{type(exc).__name__}: {exc}")
            raise _fail(
                500, label, {"error": "scoring_failed", "request_id": request_id}, kind
            ) from exc
        model_malicious = raw > model.threshold
        if kind and model_malicious:  # a never-break canary flagged by the model itself
            metrics.inc("never_break_hits", {"model_version": label, "ring": ring})
        ref_version, ref_score, ref_verdict = _reference_score(
            router, model, req.feature_schema_version, x, request_id
        )
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
        span.set_attributes(
            {"sentinel.model_version": label, "sentinel.ring": ring, "sentinel.verdict": verdict}
        )
        with tracer.start_as_current_span("log"):  # lineage hand-off (batched insert later)
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
                    event_id=req.event_id,
                    ring=ring,
                    reference_version=ref_version,
                    reference_score=ref_score,
                    reference_verdict=ref_verdict,
                    trace_id=trace_id_of(span),
                )
            )
        metrics.inc(f"{kind}requests", {"model_version": label, "status": "200"})
        metrics.observe(
            f"{kind}latency_ms", (time.perf_counter() - t0) * 1000, {"model_version": label}
        )
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
        ok = router.ready and overrides.loaded
        body = {"ready": ok, "models": router.ready, "overrides": overrides.loaded}
        return JSONResponse(body, status_code=200 if ok else 503)

    @app.get("/v1/models")
    def list_models() -> list[dict[str, Any]]:
        return router.status()

    @app.get("/v1/rings")
    def rings(response: Response) -> dict[str, dict[str, str | None]]:
        """{ring: {file_type: version served now}} as seen by the worker process that
        answers (Stage 8: several workers; header X-Sentinel-Worker = its process id)."""
        response.headers[WORKER_HEADER] = str(os.getpid())
        return router.serving_versions()

    @app.post("/admin/rollback")
    def admin_rollback(
        req: RollbackRequest, x_api_key: Annotated[str | None, Header()] = None
    ) -> dict[str, Any]:
        """Kill switch: every ring alias back to production, recorded, reloaded at once."""
        if kill_switch is None or not admin_api_key:
            raise HTTPException(503, {"error": "kill switch disabled (no admin API key set)"})
        if x_api_key is None or not hmac.compare_digest(
            x_api_key.encode("utf-8"), admin_api_key.encode("utf-8")
        ):
            metrics.inc("admin_auth_failures")
            raise HTTPException(401, {"error": "missing or wrong X-API-Key"})
        t0 = time.perf_counter()
        result = kill_switch(req.reason)
        refreshed = router.refresh()  # do not wait for the next poll
        seconds = time.perf_counter() - t0
        metrics.inc("kill_switch")
        log.warning("kill_switch_done", reason=req.reason, seconds=round(seconds, 3))
        return {
            "result": result,
            "refresh": refreshed,
            "serving": router.serving_versions(),
            "seconds": seconds,
        }

    @app.get("/v1/ops/stats")
    def stats() -> list[dict[str, Any]]:
        return [s.as_dict() for s in metrics.snapshot()]

    return app


def build_app() -> FastAPI:  # pragma: no cover - wiring, exercised by `make up` + integration
    """Production wiring from .env + configs/*.yaml (used by docker compose)."""
    from sentinel.config import (
        Settings,
        load_monitoring_config,
        load_replay_config,
        load_rollout_config,
        load_serving_config,
        load_train_config,
    )
    from sentinel.logging import configure_logging
    from sentinel.monitoring.metrics import postgres_ops_writer
    from sentinel.monitoring.tracing import setup_tracing
    from sentinel.rollout.store import MlflowRegistry, PostgresTransitionStore, emergency_rollback
    from sentinel.serving.lineage import BatchedLineageWriter, postgres_batch_writer
    from sentinel.serving.models import MlflowModelSource
    from sentinel.serving.overrides import PostgresOverrideStore

    settings = Settings()
    configure_logging(settings.log_level)
    cfg = load_serving_config()
    mon = load_monitoring_config()
    setup_tracing(mon.tracing.api_service_name, mon.tracing, settings.otlp_endpoint)
    rollout = load_rollout_config()
    if rollout.fallback_alias != cfg.model_alias:
        raise SystemExit("rollout.yaml fallback_alias must equal serving.yaml model_alias")
    names = {ft: load_train_config().mlflow.model_name(ft) for ft in cfg.file_types}
    if not settings.pg_dsn:
        raise SystemExit("SENTINEL_PG_DSN is not set")
    pg_dsn = settings.pg_dsn
    metrics = MetricsRecorder(cfg.service_name, cfg.latency_samples, mon.metrics.window_max_samples)
    ops_writer = OpsMetricsWriter([metrics], postgres_ops_writer(pg_dsn), mon.metrics.flush_seconds)
    source = MlflowModelSource(
        settings.tracking_uri, {n: ft for ft, n in names.items()}, cfg.predict_threads
    )
    router = RingRouter.build(source, names, rollout, load_replay_config().rings, metrics)
    store = PostgresOverrideStore(pg_dsn)
    sink = BatchedLineageWriter(postgres_batch_writer(pg_dsn), cfg.lineage, metrics)

    def kill(reason: str) -> dict[str, Any]:
        return emergency_rollback(
            PostgresTransitionStore(pg_dsn),
            MlflowRegistry(settings.tracking_uri),
            names,
            rollout,
            actor="kill_switch",
            reason=reason,
        )

    key = settings.admin_api_key.get_secret_value() if settings.admin_api_key else None
    return create_app(
        router,
        store,
        sink,
        metrics,
        cfg,
        kill_switch=kill,
        admin_api_key=key,
        canary_prefix=rollout.canaries.tenant_prefix,
        ops_writer=ops_writer,
    )
