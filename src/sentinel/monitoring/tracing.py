"""Stage 8 tracing: OpenTelemetry SDK, OTLP/HTTP export to Jaeger.

One trace per stream event: the live consumer's `consume` span -> the API's `score` span
(context carried in the W3C `traceparent` header) -> `log` (lineage hand-off). The API
stores the trace id with the prediction, so every lineage row links to its trace.

Without `setup_tracing` (unit tests, tracing switched off) the global OpenTelemetry provider
is a no-op: spans are not recorded and `trace_id_of` returns None. Export is batched in a
background thread with a bounded queue; a full queue drops spans and never blocks a request.
"""

from __future__ import annotations

from collections.abc import Mapping

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import Span

from sentinel.config import TracingConfig
from sentinel.logging import get_logger

log = get_logger(__name__)


def setup_tracing(service_name: str, cfg: TracingConfig, endpoint: str | None = None) -> bool:
    """Install the global tracer provider. Returns False (tracing off) when no endpoint."""
    url = endpoint if endpoint is not None else cfg.endpoint
    if not url:
        log.info("tracing_disabled", service=service_name)
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}),
        sampler=ParentBased(TraceIdRatioBased(cfg.sample_ratio)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=url),
            max_queue_size=cfg.export_queue_size,
            max_export_batch_size=min(cfg.export_batch_size, cfg.export_queue_size),
        )
    )
    trace.set_tracer_provider(provider)
    log.info("tracing_enabled", service=service_name, endpoint=url)
    return True


def trace_id_of(span: Span) -> str | None:
    """32-hex W3C trace id of a recorded span; None for a no-op / unsampled span."""
    ctx = span.get_span_context()
    if not ctx.is_valid or not span.is_recording():
        return None
    return trace.format_trace_id(ctx.trace_id)


def context_from_headers(headers: Mapping[str, str]) -> Context:
    return extract(dict(headers))


def headers_for_current_span() -> dict[str, str]:
    """`traceparent` (and `tracestate`) for an outgoing request; {} without a recorded span."""
    carrier: dict[str, str] = {}
    inject(carrier)
    return carrier
