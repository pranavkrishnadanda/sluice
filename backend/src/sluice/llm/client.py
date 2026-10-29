"""Typed LLM calls through the LiteLLM proxy, with one schema-repair retry and per-call accounting.

`LLM.run` builds a Pydantic AI `Agent` on an OpenAI-compatible model whose name is the *tier*
(`"small"` / `"strong"`); LiteLLM maps the tier to a model group with retries, fallbacks, rate
limits and a Redis response cache (`backend/litellm/config.yaml`). The client itself never retries
transport errors (that is LiteLLM's job): it retries only an output that fails schema validation,
once, with the validation error fed back to the model.

Structured output (`OutputMode`, default ``"native"``): the output schema is sent as an OpenAI
``response_format`` of type ``json_schema`` (Pydantic AI `NativeOutput`), not as a required output
tool. Groq documents json_schema structured outputs for ``openai/gpt-oss-120b`` and ``-20b``
(https://console.groq.com/docs/structured-outputs, read 2026-10-29), LiteLLM 1.106.5 passes the
``response_format`` through for both (``litellm.supports_response_schema`` is true, so its Groq
tool-call workaround stays off) and maps it to Gemini's ``responseJsonSchema`` for the fallback. In
tool mode gpt-oss sometimes wrote reasoning prose instead of the tool call (first live eval: 3 of 37
calls). ``strict`` is sent as false (Groq best-effort mode), because strict mode rejects schemas
with optional fields or defaults. When the provider still reports an unparseable generation (HTTP
400 with ``output_parse_failed`` / ``json_validate_failed`` / ``tool_use_failed``), the call is
retried once with a "respond with only the JSON object" nudge, and a second such failure is an
`LLMOutputError`.

Failures map onto two exceptions the pipeline handles by falling back to rules-only output:
`LLMOutputError` (still invalid after the repair) and `LLMUnavailable` (the proxy answered with an
error, or could not be reached). Their messages are built here from the status code and error class
only, never from provider text, and chaining is suppressed, so the master key or a provider key
echoed in an error body cannot leak through them.

Cost: LiteLLM's `x-litellm-response-cost` header when the proxy sent one, otherwise the dated list
price table in `sluice.llm.prices`. A cache hit is recognised by a non-empty `x-litellm-cache-key`
header (LiteLLM 1.106.5 sets it only on cached responses) and costs 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final, Literal, Self

import httpx2
from opentelemetry.trace import Span, SpanKind
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, NativeOutput, PromptedOutput, capture_run_messages
from pydantic_ai.exceptions import (
    AgentRunError,
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.output import OutputSpec
from pydantic_ai.providers.litellm import LiteLLMProvider
from pydantic_ai.settings import ModelSettings

from sluice import tracing
from sluice.llm import prices

Tier = Literal["small", "strong"]

OutputMode = Literal["native", "prompted", "tool"]
"""How the output schema reaches the model: a json_schema ``response_format`` (``native``), a
schema in the prompt (``prompted``), or Pydantic AI's required output tool (``tool``)."""

DEFAULT_OUTPUT_MODE: Final[OutputMode] = "native"

REQUEST_TIMEOUT_S: Final = 120.0
"""Client-side cap on one proxy request (LiteLLM's own retries and fallbacks included).

Nested between the proxy's timeout-only worst case and the per-file wall cap, so a hung
deployment still reaches its last fallback and one hung call cannot by itself exceed a file's
wall time. From ``backend/litellm/config.yaml`` (per-attempt timeout 30 s on every deployment; a
timeout is not retried, it goes to the next group of the chain), the path where every attempt
*times out* is:

* strong: gpt-oss-120b 30 + Gemini 30 + qwen 30 = 90 s
* small:  gpt-oss-20b 30 + qwen 30 = 60 s

so 90 s < 120 s (this) < 180 s (``caps.MAX_WALL_S_PER_FILE``). The headroom covers proxy
overhead and *fast-failing* 429/5xx retries. ``tests/llm/test_litellm_config.py`` asserts the
nesting from the config itself.

This does not bound *slow* errors: LiteLLM retries a 429/500/503 twice on the same group, so a
group can take up to 3 attempts x 30 s (`SLOW_ERROR_WORST_CASE_S`). Such a call hits this
timeout first and surfaces as `LLMUnavailable`: the file degrades to rules-only output for that
step, never to wrong output. The numbers are left as they are, since a client timeout above the
slow-error bound would exceed the per-file wall cap."""

SLOW_ERROR_WORST_CASE_S: Final[dict[str, float]] = {"strong": 270.0, "small": 180.0}
"""Proxy time if every attempt of every group in the chain fails slowly with a retried error:
attempts per group (1 + 2 retries) x groups x 30 s. strong = 3 x 3 x 30, small = 3 x 2 x 30.
Above `REQUEST_TIMEOUT_S` on purpose (see there); the config test recomputes it from the YAML."""

CONNECT_TIMEOUT_S: Final = 5.0

OUTPUT_REPAIR_RETRIES: Final = 1
"""Re-asks after an output fails schema validation, before `LLMOutputError`."""

GENERATION_RETRIES: Final = 1
"""Re-asks after the provider itself rejects the generation as unparseable (see the module doc)."""

GENERATION_ERROR_CODES: Final = ("output_parse_failed", "json_validate_failed", "tool_use_failed")
"""Groq error codes for a generation that did not parse / match the schema. Groq sends them as
``error.code``; LiteLLM relays them inside ``error.message`` with ``error.code`` set to the HTTP
status (``"400"``), so the message is searched only when the code is absent or numeric."""

RATE_LIMIT_RETRIES: Final = 3
"""Re-sends after a rate limit (HTTP 429, or the proxy's all-in-cooldown error with Retry-After)."""

RATE_LIMIT_BACKOFF_S: Final = 2.0
"""First wait without a usable Retry-After; doubles per retry, capped at `RATE_LIMIT_MAX_BACKOFF_S`.
Also the minimum wait, so a Retry-After of 0 or in the past does not hot-loop the proxy."""

RATE_LIMIT_MAX_BACKOFF_S: Final = 30.0
"""Cap of the computed back-off (the hosted deployments' cooldown is 30 s). A Retry-After the
provider sends is honoured as is, bounded only by the wall budget (see `LLM.wall_budget`)."""

DEFAULT_WALL_BUDGET_S: Final = 120.0
"""Deadline for rate-limit back-off per call when no `LLM.wall_budget` is active. The pipeline sets
the per-file budget (``caps.MAX_WALL_S_PER_FILE``) around each file, so this applies to ad-hoc use.
Like the per-file budget it bounds only the waiting, not the requests themselves."""

PROXY_COOLDOWN_MARKER: Final = "No deployments available"
"""LiteLLM's router error when every deployment of the route is cooling down or over its limits.
The proxy answers it with a ``retry-after`` header (``_apply_router_cooldown_retry_after`` in
1.106.5 ``proxy/common_request_processing.py``); only then is it paced like a 429."""

JSON_ONLY_NUDGE: Final = (
    "\n\nRespond with only the JSON object that matches the required schema: no prose, no "
    "reasoning, no markdown."
)

COST_HEADER: Final = "x-litellm-response-cost"
CACHE_KEY_HEADER: Final = "x-litellm-cache-key"
DEPLOYMENT_HEADER: Final = "x-litellm-model-name"

_MODEL_SETTINGS: Final = ModelSettings(temperature=0.0, max_retries=0)
"""Deterministic sampling (stable cache keys) and no SDK-level retries (LiteLLM retries)."""

NO_CACHE_BODY: Final = {"cache": {"no-cache": True}}
"""LiteLLM's per-request cache control: do not answer from the response cache (the fresh answer
is still stored). Checked in LiteLLM 1.106.5 ``caching/caching_handler.py``: a cache read is
skipped when the request's ``cache`` dict has ``"no-cache": True``; the proxy forwards top-level
body keys to the router, so the client sends it through ``extra_body``."""

_response_headers: ContextVar[list[httpx2.Headers] | None] = ContextVar(
    "sluice_llm_response_headers", default=None
)


class CallRecord(BaseModel):
    """Accounting for one `LLM.run` call: every attempt it made.

    Tokens and cost sum the responses the provider returned (schema-repair retries included).
    An attempt the provider rejected as unparseable (`GENERATION_ERROR_CODES`) returns no usage,
    so it adds nothing to tokens or cost and is counted in `rejected_attempts` instead. Rate-limited
    sends are not billed and are not counted. `latency_ms` spans the whole call, back-off included.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    tier: Tier
    model: str = Field(description="Deployment that served the call, else the reported model name.")
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    latency_ms: int = Field(ge=0)
    cached: bool
    rejected_attempts: int = Field(
        default=0,
        ge=0,
        description="Attempts the provider rejected as unparseable; their tokens are unknown.",
    )


class LLMError(Exception):
    """Base class for model/provider failures the pipeline turns into a rules-only fallback."""


class LLMOutputError(LLMError):
    """The model's output still failed schema validation after the repair retry."""

    def __init__(self, message: str, *, record: CallRecord) -> None:
        super().__init__(message)
        self.record = record


class LLMUnavailable(LLMError):
    """Every route failed: the proxy returned an error or could not be reached."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


_deadline: ContextVar[float | None] = ContextVar("sluice_llm_deadline", default=None)
"""Absolute time (the client's clock) by which rate-limit back-off must end; see `wall_budget`.
In-flight requests are not cut off at it (each is bounded by `REQUEST_TIMEOUT_S` instead)."""


async def _capture_headers(response: httpx2.Response) -> None:
    """Keep the headers of successful responses only: cost, cache and deployment describe those."""
    sink = _response_headers.get()
    if sink is not None and response.status_code < 400:
        sink.append(response.headers)


type _Kind = Literal["ok", "invalid", "rejected", "throttled", "unavailable", "failed"]


@dataclass(frozen=True, slots=True)
class _Attempt:
    """What one `agent.run` came to, as plain data: no exception object survives the handler."""

    kind: _Kind
    output: object = None
    status: int | None = None
    error_name: str = ""
    code: str | None = None
    retry_after: float | None = None


class LLM:
    """Facade over Pydantic AI and the LiteLLM proxy. Appends a `CallRecord` per billed call.

    Args:
        base_url: LiteLLM proxy root, e.g. `http://localhost:54000` (`/v1` is appended).
        api_key: LiteLLM master key. Kept only inside the HTTP client; never in `repr`.
        model_override: A model used for every tier instead of the proxy (`FunctionModel` /
            `TestModel` in tests).
        http_client: The `httpx2` client to send proxy requests with (tests inject a transport).
            A client passed in stays the caller's to close; one created here is closed by `aclose`.
        cache: Whether the proxy may answer from its response cache. Evals pass ``False`` so
            every trial measures the model, not the cache (Ruling F3); ``sluice run`` keeps it.
        output_mode: How the output schema is sent (`OutputMode`); ``native`` by default.
        clock: Monotonic seconds for the rate-limit budget (tests inject a fake).
        sleep: The rate-limit back-off wait (tests inject a fake).

    Event-loop lifetime: the HTTP client pools connections bound to the event loop that first
    uses them. Create, use and close (`aclose` / `async with`) an instance inside ONE event loop,
    e.g. inside the coroutine passed to `asyncio.run`. Never share an instance across
    `asyncio.run` calls.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        model_override: Model | None = None,
        http_client: httpx2.AsyncClient | None = None,
        cache: bool = True,
        output_mode: OutputMode = DEFAULT_OUTPUT_MODE,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.records: list[CallRecord] = []
        self.cache = cache
        self.output_mode: OutputMode = output_mode
        self._clock = clock
        self._sleep = sleep
        self._override = model_override
        self._owns_client = http_client is None
        self._http = http_client or httpx2.AsyncClient(
            timeout=httpx2.Timeout(REQUEST_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
        )
        hooks = self._http.event_hooks
        hooks.setdefault("response", []).append(_capture_headers)
        self._http.event_hooks = hooks
        provider = LiteLLMProvider(
            api_base=f"{base_url.rstrip('/')}/v1", api_key=api_key, http_client=self._http
        )
        settings = (
            _MODEL_SETTINGS if cache else ModelSettings(**_MODEL_SETTINGS, extra_body=NO_CACHE_BODY)
        )
        self._models: dict[Tier, Model] = {
            tier: OpenAIChatModel(tier, provider=provider, settings=settings)
            for tier in ("small", "strong")
        }

    def __repr__(self) -> str:
        return (
            f"LLM(records={len(self.records)}, override={self._override is not None}, "
            f"cache={self.cache}, output_mode={self.output_mode})"
        )

    async def aclose(self) -> None:
        """Close the HTTP client if this instance created it."""
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    @contextlib.contextmanager
    def wall_budget(self, seconds: float) -> Iterator[None]:
        """Bound rate-limit back-off of every `run` inside this block (and tasks it starts).

        A back-off wait that would end more than `seconds` after entering the block is not
        started; the call fails as `LLMUnavailable` instead. So back-off alone never runs past the
        budget. The budget does not cover time spent in requests: a request started just before
        the deadline still runs to completion (up to `REQUEST_TIMEOUT_S`). With the pipeline's
        per-file budget, which checks its wall cap only between attempts, a file can therefore
        overrun ``MAX_WALL_S_PER_FILE`` by up to one request.
        """
        token = _deadline.set(self._clock() + seconds)
        try:
            yield
        finally:
            _deadline.reset(token)

    async def run[T](
        self, role: str, tier: Tier, system: str, prompt: str, output_type: type[T]
    ) -> tuple[T, CallRecord]:
        """Ask the `tier` model group for an `output_type`.

        Retries, each bounded: one schema-repair retry (validation error fed back); one re-ask
        with `JSON_ONLY_NUDGE` after the provider rejects the generation as unparseable; up to
        `RATE_LIMIT_RETRIES` re-sends after a rate limit, waiting Retry-After (else exponential
        back-off) while the wait ends inside the active `wall_budget`.

        Returns:
            The output and a `CallRecord` covering every attempt (see `CallRecord`).

        Raises:
            LLMOutputError: the output was invalid after the repair retry, or the provider rejected
                the generation twice. Its `record` carries the spent tokens and rejected attempts.
            LLMUnavailable: the proxy failed, was unreachable, or stayed rate-limited (no record).
        """
        # One `chat <tier>` span per call (GenAI semantic conventions); never prompt text.
        attributes = {
            "gen_ai.operation.name": "chat",
            "gen_ai.system": "litellm",
            "gen_ai.request.model": tier,
            "sluice.role": role,
        }
        with tracing.span(f"chat {tier}", attributes, kind=SpanKind.CLIENT) as span:
            try:
                output, record = await self._run(role, tier, system, prompt, output_type)
            except LLMOutputError as exc:
                _annotate(span, exc.record)
                raise
            _annotate(span, record)
            return output, record

    async def _run[T](
        self, role: str, tier: Tier, system: str, prompt: str, output_type: type[T]
    ) -> tuple[T, CallRecord]:
        """`run` without its span."""
        agent = Agent(
            self._override or self._models[tier],
            output_type=_wrap_output(output_type, self.output_mode),
            system_prompt=system,
            retries={"output": OUTPUT_REPAIR_RETRIES},
        )
        headers: list[httpx2.Headers] = []
        messages: list[ModelMessage] = []
        token = _response_headers.set(headers)
        start = time.perf_counter()
        deadline = _deadline.get()
        if deadline is None:
            deadline = self._clock() + DEFAULT_WALL_BUDGET_S
        rejected = throttled = 0
        try:
            while True:
                nudge = JSON_ONLY_NUDGE if rejected else ""
                attempt = await self._attempt(agent, prompt + nudge, messages)
                if attempt.kind == "ok":
                    record = self._record(role, tier, messages, headers, start, rejected)
                    return attempt.output, record  # type: ignore[return-value]  # agent output
                if attempt.kind == "rejected":
                    rejected += 1
                    if rejected <= GENERATION_RETRIES:
                        continue
                    raise LLMOutputError(
                        f"{role}/{tier}: provider rejected the generation {rejected} times "
                        f"(HTTP {attempt.status}, {attempt.code})",
                        record=self._record(role, tier, messages, headers, start, rejected),
                    )
                if attempt.kind == "throttled" and throttled < RATE_LIMIT_RETRIES:
                    wait = _backoff_s(throttled, attempt.retry_after)
                    if self._clock() + wait <= deadline:
                        throttled += 1
                        await self._sleep(wait)
                        continue
                if attempt.kind == "invalid":
                    raise LLMOutputError(
                        f"{role}/{tier}: output failed validation after "
                        f"{OUTPUT_REPAIR_RETRIES} repair retry ({attempt.error_name})",
                        record=self._record(role, tier, messages, headers, start, rejected),
                    )
                if attempt.kind == "failed":
                    raise LLMUnavailable(f"{role}/{tier}: LLM call failed ({attempt.error_name})")
                status = attempt.status
                raise LLMUnavailable(
                    f"{role}/{tier}: LLM proxy unavailable ({attempt.error_name}"
                    + (f", HTTP {status})" if status is not None else ")")
                    + (f" after {throttled} rate-limit retries" if throttled else ""),
                    status_code=status,
                )
        finally:
            _response_headers.reset(token)

    async def _attempt(
        self, agent: Agent[None, object], prompt: str, messages: list[ModelMessage]
    ) -> _Attempt:
        """One `agent.run`, its messages appended to `messages`, its outcome as plain data.

        The caller raises from the returned data, outside any ``except`` block: an exception raised
        in a handler keeps the original as ``__context__`` (even with ``from None``), and a provider
        error body may echo a key.
        """
        with capture_run_messages() as attempt_messages:
            try:
                result = await agent.run(prompt)
            except UnexpectedModelBehavior as exc:
                outcome = _Attempt("invalid", error_name=type(exc).__name__)
            except ModelAPIError as exc:
                outcome = _classify(exc)
            except (AgentRunError, TimeoutError, httpx2.HTTPError) as exc:
                outcome = _Attempt("failed", error_name=type(exc).__name__)
            else:
                outcome = _Attempt("ok", output=result.output)
        messages.extend(attempt_messages)
        return outcome

    def _record(
        self,
        role: str,
        tier: Tier,
        messages: list[ModelMessage],
        headers: list[httpx2.Headers],
        start: float,
        rejected: int = 0,
    ) -> CallRecord:
        responses = [m for m in messages if isinstance(m, ModelResponse)]
        in_tok = sum(r.usage.input_tokens for r in responses)
        out_tok = sum(r.usage.output_tokens for r in responses)
        deployment = next((h[DEPLOYMENT_HEADER] for h in headers if h.get(DEPLOYMENT_HEADER)), None)
        reported = next((r.model_name for r in responses if r.model_name), None)
        model = deployment or reported or tier
        cached = bool(headers) and all(h.get(CACHE_KEY_HEADER) for h in headers)
        record = CallRecord(
            role=role,
            tier=tier,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=0.0 if cached else _cost(model, tier, in_tok, out_tok, headers),
            latency_ms=round((time.perf_counter() - start) * 1000),
            cached=cached,
            rejected_attempts=rejected,
        )
        self.records.append(record)
        return record


def _annotate(span: Span, record: CallRecord) -> None:
    """Put a call's accounting on its span: model, token counts, cost (no text)."""
    span.set_attributes(
        {
            "gen_ai.response.model": record.model,
            "gen_ai.usage.input_tokens": record.input_tokens,
            "gen_ai.usage.output_tokens": record.output_tokens,
            "sluice.cost_usd": record.cost_usd,
            "sluice.cached": record.cached,
        }
    )


def _wrap_output[T](output_type: type[T], mode: OutputMode) -> OutputSpec[T]:
    """The Pydantic AI output spec for `output_type` in `mode`."""
    if mode == "native":
        return NativeOutput(output_type, strict=False)
    if mode == "prompted":
        return PromptedOutput(output_type)
    return output_type


def _classify(exc: ModelAPIError) -> _Attempt:
    """Map a provider/proxy error onto an attempt outcome (status, code and wait only)."""
    name = type(exc).__name__
    if not isinstance(exc, ModelHTTPError):
        return _Attempt("unavailable", error_name=name)
    status = exc.status_code
    if status == 400 and (code := _generation_error_code(exc)) is not None:
        return _Attempt("rejected", status=status, error_name=name, code=code)
    retry_after = _retry_after_s(exc.headers)
    cooldown = retry_after is not None and PROXY_COOLDOWN_MARKER in _error_message(exc.body)
    if status == 429 or cooldown:
        return _Attempt("throttled", status=status, error_name=name, retry_after=retry_after)
    return _Attempt("unavailable", status=status, error_name=name)


def _generation_error_code(exc: ModelHTTPError) -> str | None:
    """The `GENERATION_ERROR_CODES` entry a 400 carries, else None (see that constant).

    The structured ``error.code`` decides when it is a provider code; the message is searched
    only when the code is absent or just the HTTP status (LiteLLM's relay). Only the code is
    returned, never body text, so a key echoed in the body cannot escape.
    """
    code = exc.provider_error_code
    if code in GENERATION_ERROR_CODES:
        return code
    if code is not None and not code.isdecimal():
        return None
    message = _error_message(exc.body)
    return next((c for c in GENERATION_ERROR_CODES if c in message), None)


def _error_message(body: object) -> str:
    """``error.message`` of an OpenAI-style error body (the SDK passes the inner ``error``)."""
    if isinstance(body, dict):
        message = body.get("message")
        return message if isinstance(message, str) else ""
    return body if isinstance(body, str) else ""


def _retry_after_s(headers: dict[str, str] | None) -> float | None:
    """Seconds from a ``retry-after`` header (delta-seconds or HTTP-date), else None."""
    raw = (headers or {}).get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except TypeError, ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    return seconds if math.isfinite(seconds) else None


def _backoff_s(retry: int, retry_after: float | None) -> float:
    """Wait before rate-limit retry number `retry` (0-based): Retry-After, else exponential."""
    if retry_after is not None:
        return max(retry_after, RATE_LIMIT_BACKOFF_S)
    return min(RATE_LIMIT_BACKOFF_S * 2**retry, RATE_LIMIT_MAX_BACKOFF_S)


def _header_cost(headers: list[httpx2.Headers]) -> float | None:
    """Sum of the proxy's per-response cost headers, or None unless every response priced itself.

    LiteLLM reports 0 for a deployment missing from its own price map, so a 0 on a non-cached
    response is treated as "unknown" rather than "free".
    """
    if not headers:
        return None
    total = 0.0
    for h in headers:
        try:
            value = float(h.get(COST_HEADER, ""))
        except ValueError:
            return None
        if not math.isfinite(value) or value < 0 or (value == 0 and not h.get(CACHE_KEY_HEADER)):
            return None
        total += value
    return total


def _cost(
    model: str, tier: Tier, in_tok: int, out_tok: int, headers: list[httpx2.Headers]
) -> float:
    from_header = _header_cost(headers)
    if from_header is not None:
        return from_header
    priced = model if model in prices.PRICES else prices.TIER_PRIMARY[tier]
    return prices.cost_usd(priced, in_tok, out_tok)
