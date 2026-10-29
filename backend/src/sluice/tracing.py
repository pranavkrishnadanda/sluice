"""OpenTelemetry tracing for runs, pipeline steps, node attempts, LLM calls and sandbox runs.

Export is opt-in: `setup_tracing` installs an SDK `TracerProvider` with a batching OTLP/HTTP
exporter only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (e.g. ``http://127.0.0.1:56006`` for
the local Phoenix of ``make up-obs``; the exporter appends ``/v1/traces``). Otherwise the API's
no-op provider stays in place and every span below costs a few attribute writes and nothing else.

Span tree of one run (names fixed; docs/api and Plan 4 Task 7)::

    sluice.run                      sluice.run_id, sluice.table, sluice.status
      sluice.step                   sluice.step = mapping | profile | rules_pass_1 | plan | ...
        chat <tier>                 GenAI attributes (mapping and plan calls)
      sluice.node.attempt           sluice.node_id, sluice.attempt, sluice.tier, sluice.outcome
        chat <tier>                 worker / verifier calls
        sluice.sandbox              sluice.exit_code, sluice.timed_out, sluice.duration_ms

Attributes carry identifiers, counts and outcomes only: never prompt or completion text,
generated code, cell values or secrets. A span that ends with an exception gets status ERROR
described by the exception *type* name; the exception message is not recorded (a provider error
can echo a key), so no exception event is added either.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from typing import Final, Literal

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode
from opentelemetry.util.types import AttributeValue

__all__ = ["Step", "setup_tracing", "span", "step_span"]

ENDPOINT_ENV: Final = "OTEL_EXPORTER_OTLP_ENDPOINT"
SERVICE_NAME_ENV: Final = "OTEL_SERVICE_NAME"
TRACER_NAME: Final = "sluice"

Step = Literal["profile", "rules_pass_1", "mapping", "plan", "rules_pass_2", "load"]

_tracer = trace.get_tracer(TRACER_NAME)
"""A proxy tracer: it follows whatever global provider is installed later."""

_installed = False


def setup_tracing(service_name: str = "sluice") -> None:
    """Install the OTLP/HTTP exporting provider once, when an endpoint is configured.

    Idempotent: later calls do nothing (the API accepts one global provider per process). With
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` unset or blank nothing is installed and spans stay no-ops.
    ``OTEL_SERVICE_NAME``, when set, overrides ``service_name``. Spans are exported from a
    background thread in batches; the SDK flushes them at interpreter exit.
    """
    global _installed
    if _installed or not os.environ.get(ENDPOINT_ENV, "").strip():
        return
    # Imported here: a process without an endpoint never loads the SDK or the exporter.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    name = os.environ.get(SERVICE_NAME_ENV, "").strip() or service_name
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _installed = True


@contextmanager
def span(
    name: str,
    attributes: Mapping[str, AttributeValue] | None = None,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
) -> Iterator[Span]:
    """A current span; an exception marks it ERROR with the exception type name only."""
    with _tracer.start_as_current_span(
        name,
        kind=kind,
        attributes=attributes,
        record_exception=False,
        set_status_on_exception=False,
    ) as current:
        try:
            yield current
        except BaseException as exc:
            current.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise


def step_span(step: Step) -> AbstractContextManager[Span]:
    """The ``sluice.step`` span of one pipeline step."""
    return span("sluice.step", {"sluice.step": step})
