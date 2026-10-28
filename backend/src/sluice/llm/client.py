"""Typed LLM calls through the LiteLLM proxy, with one schema-repair retry and per-call accounting.

`LLM.run` builds a Pydantic AI `Agent` on an OpenAI-compatible model whose name is the *tier*
(`"small"` / `"strong"`); LiteLLM maps the tier to a model group with retries, fallbacks, rate
limits and a Redis response cache (`backend/litellm/config.yaml`). The client itself never retries
transport errors (that is LiteLLM's job): it retries only an output that fails schema validation,
once, with the validation error fed back to the model.

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

import math
import time
from contextvars import ContextVar
from typing import Final, Literal, Self

import httpx2
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, capture_run_messages
from pydantic_ai.exceptions import (
    AgentRunError,
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
)
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.litellm import LiteLLMProvider
from pydantic_ai.settings import ModelSettings

from sluice.llm import prices

Tier = Literal["small", "strong"]

REQUEST_TIMEOUT_S: Final = 180.0
"""Client-side cap on one proxy request, covering LiteLLM's own retries and fallbacks."""

CONNECT_TIMEOUT_S: Final = 5.0

OUTPUT_REPAIR_RETRIES: Final = 1
"""Re-asks after an output fails schema validation, before `LLMOutputError`."""

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
    """Accounting for one `LLM.run` call (all HTTP requests it made, repair retry included)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    tier: Tier
    model: str = Field(description="Deployment that served the call, else the reported model name.")
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    latency_ms: int = Field(ge=0)
    cached: bool


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


async def _capture_headers(response: httpx2.Response) -> None:
    sink = _response_headers.get()
    if sink is not None:
        sink.append(response.headers)


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
    ) -> None:
        self.records: list[CallRecord] = []
        self.cache = cache
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
            f"cache={self.cache})"
        )

    async def aclose(self) -> None:
        """Close the HTTP client if this instance created it."""
        if self._owns_client:
            await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def run[T](
        self, role: str, tier: Tier, system: str, prompt: str, output_type: type[T]
    ) -> tuple[T, CallRecord]:
        """Ask the `tier` model group for an `output_type`; one repair retry on invalid output.

        Raises:
            LLMOutputError: the output was invalid twice (its `record` carries the spent tokens).
            LLMUnavailable: the proxy failed or was unreachable (nothing billed, no record).
        """
        agent = Agent(
            self._override or self._models[tier],
            output_type=output_type,
            system_prompt=system,
            retries={"output": OUTPUT_REPAIR_RETRIES},
        )
        headers: list[httpx2.Headers] = []
        token = _response_headers.set(headers)
        start = time.perf_counter()
        failure: LLMError | None = None
        try:
            with capture_run_messages() as messages:
                # Failures are built inside the handlers but raised after them: an exception
                # raised in an `except` block keeps the original as `__context__` (even with
                # `from None`), and a provider error body may echo a key.
                try:
                    result = await agent.run(prompt)
                except UnexpectedModelBehavior as exc:
                    failure = LLMOutputError(
                        f"{role}/{tier}: output failed validation after "
                        f"{OUTPUT_REPAIR_RETRIES} repair retry ({type(exc).__name__})",
                        record=self._record(role, tier, messages, headers, start),
                    )
                except ModelAPIError as exc:
                    status = exc.status_code if isinstance(exc, ModelHTTPError) else None
                    failure = LLMUnavailable(
                        f"{role}/{tier}: LLM proxy unavailable ({type(exc).__name__}"
                        + (f", HTTP {status})" if status is not None else ")"),
                        status_code=status,
                    )
                except (AgentRunError, TimeoutError, httpx2.HTTPError) as exc:
                    failure = LLMUnavailable(
                        f"{role}/{tier}: LLM call failed ({type(exc).__name__})"
                    )
            if failure is not None:
                raise failure
            record = self._record(role, tier, messages, headers, start)
            return result.output, record
        finally:
            _response_headers.reset(token)

    def _record(
        self,
        role: str,
        tier: Tier,
        messages: list[ModelMessage],
        headers: list[httpx2.Headers],
        start: float,
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
        )
        self.records.append(record)
        return record


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
