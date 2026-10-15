"""OpenTelemetry setup: metrics exposed for in-cluster Prometheus scraping, traces to an internal collector.

Nothing is exported unless explicitly configured, and the OTLP endpoint must pass the outbound URL guard.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry.metrics import CallbackOptions, Observation

from branchops.config.settings import Settings
from branchops.infrastructure.security.network_guard import assert_internal_url

logger = logging.getLogger(__name__)
_configured = False

MS_BUCKETS = (5, 25, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, 120000, 300000)
SCORE_BUCKETS = (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)


def setup_telemetry(settings: Settings, role: str) -> None:
    """Idempotently configure global meter/tracer providers for this process."""
    global _configured  # noqa: PLW0603 - process-wide OTel provider setup
    if _configured:
        return
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

    resource = Resource.create(
        {"service.name": settings.app.service_name, "service.namespace": "branchops", "branchops.role": role}
    )
    readers: list[Any] = []
    if settings.observability.metrics_enabled:
        from opentelemetry.exporter.prometheus import PrometheusMetricReader

        readers.append(PrometheusMetricReader())
    views = [
        View(instrument_unit="ms", aggregation=ExplicitBucketHistogramAggregation(MS_BUCKETS)),
        View(instrument_name="*_score", aggregation=ExplicitBucketHistogramAggregation(SCORE_BUCKETS)),
    ]
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=readers, views=views))

    tracer_provider = TracerProvider(
        resource=resource, sampler=TraceIdRatioBased(settings.observability.trace_sample_ratio)
    )
    if settings.observability.otlp_endpoint:
        assert_internal_url(settings.observability.otlp_endpoint, settings.network, purpose="otlp_endpoint")
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.observability.otlp_endpoint))
        )
    trace.set_tracer_provider(tracer_provider)
    _configured = True
    get_instruments.cache_clear()


def tracer() -> trace.Tracer:
    return trace.get_tracer("branchops")


@dataclass(frozen=True)
class Instruments:
    webhook_requests: Any
    webhook_latency_ms: Any
    worker_events: Any
    worker_failures: Any
    agent_duration_ms: Any
    model_latency_ms: Any
    model_tokens: Any
    model_errors: Any
    structured_output_failures: Any
    classification_failures: Any
    completeness_score: Any
    quality_score: Any
    mcp_tool_calls: Any
    mcp_tool_latency_ms: Any
    mcp_auth_failures: Any


@lru_cache(maxsize=1)
def get_instruments() -> Instruments:
    m = metrics.get_meter("branchops")
    return Instruments(
        webhook_requests=m.create_counter(
            "branchops_webhook_requests", description="Webhook requests by outcome"
        ),
        webhook_latency_ms=m.create_histogram("branchops_webhook_latency", unit="ms"),
        worker_events=m.create_counter("branchops_worker_events", description="Inbox events processed"),
        worker_failures=m.create_counter(
            "branchops_worker_failures", description="Inbox processing failures"
        ),
        agent_duration_ms=m.create_histogram("branchops_agent_duration", unit="ms"),
        model_latency_ms=m.create_histogram("branchops_model_latency", unit="ms"),
        model_tokens=m.create_counter("branchops_model_tokens", description="Model token usage"),
        model_errors=m.create_counter("branchops_model_errors", description="Model call errors"),
        structured_output_failures=m.create_counter(
            "branchops_structured_output_failures", description="Model outputs failing schema validation"
        ),
        classification_failures=m.create_counter("branchops_classification_failures"),
        completeness_score=m.create_histogram("branchops_completeness_score"),
        quality_score=m.create_histogram("branchops_quality_score"),
        mcp_tool_calls=m.create_counter(
            "mcp_tool_calls", description="MCP tool calls by tool, team and outcome"
        ),
        mcp_tool_latency_ms=m.create_histogram("mcp_tool_latency", unit="ms"),
        mcp_auth_failures=m.create_counter(
            "mcp_auth_failures", description="Rejected MCP requests (401/403/429) by reason"
        ),
    )


def register_gauge(
    name: str, description: str, callback: Callable[[], Iterable[tuple[float, dict[str, str]]]]
) -> None:
    def _observe(_options: CallbackOptions) -> Iterable[Observation]:
        try:
            return [Observation(value, attrs) for value, attrs in callback()]
        except Exception:  # metrics must never crash the process
            logger.warning("gauge callback failed", extra={"gauge": name}, exc_info=True)
            return []

    metrics.get_meter("branchops").create_observable_gauge(
        name, callbacks=[_observe], description=description
    )
