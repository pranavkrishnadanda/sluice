"""LLM facade: typed outputs, one schema-repair retry, provider failures, cost and cache accounting.

No network: Pydantic AI `FunctionModel` for model behaviour, and a respx router (bridged onto the
`httpx2` client the OpenAI SDK uses) standing in for the LiteLLM proxy.
"""

import asyncio
import json
import traceback
from typing import Any

import httpx
import httpx2
import pytest
import respx
from pydantic import BaseModel, Field
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from sluice.llm import prices
from sluice.llm.client import LLM, CallRecord, LLMOutputError, LLMUnavailable

BASE_URL = "http://litellm.test"
API_KEY = "sk-sluice-test-master-key-0123456789"


class Answer(BaseModel):
    column: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


OK = {"column": "a", "confidence": 0.1}

# ---------------------------------------------------------------- helpers


class RespxBridge(httpx2.AsyncBaseTransport):
    """Send `httpx2` requests (OpenAI SDK 3) through a respx router, which speaks legacy httpx."""

    def __init__(self, router: respx.Router) -> None:
        self.router = router

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        legacy = httpx.Request(
            request.method,
            str(request.url),
            headers=list(request.headers.multi_items()),
            content=await request.aread(),
        )
        response = await self.router.async_handler(legacy)
        return httpx2.Response(
            response.status_code,
            headers=list(response.headers.multi_items()),
            content=response.content,
        )


def proxy_llm(router: respx.Router, **kwargs: Any) -> LLM:
    client = httpx2.AsyncClient(transport=RespxBridge(router))
    return LLM(BASE_URL, API_KEY, http_client=client, **kwargs)


def completion(arguments: dict[str, Any], *, prompt_tokens: int, completion_tokens: int) -> dict:
    """An OpenAI chat completion whose content is the JSON object (native structured output)."""
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1_761_609_600,
        "model": "strong",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(arguments)},
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def tool_completion(
    arguments: dict[str, Any], *, prompt_tokens: int, completion_tokens: int
) -> dict:
    """An OpenAI chat completion whose only content is a call to Pydantic AI's output tool."""
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1_761_609_600,
        "model": "strong",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "final_result",
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def scripted(*outputs: dict[str, Any], in_tok: int = 50, out_tok: int = 10) -> FunctionModel:
    """A FunctionModel that answers each request with the next output-tool call in `outputs`."""
    queue = list(outputs)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        payload = json.dumps(queue.pop(0))
        part = (
            ToolCallPart(info.output_tools[0].name, payload)
            if info.output_tools
            else TextPart(payload)
        )
        return ModelResponse(
            parts=[part], usage=RequestUsage(input_tokens=in_tok, output_tokens=out_tok)
        )

    return FunctionModel(respond, model_name="scripted")


def assert_no_key_reachable(err: BaseException) -> None:
    """Neither the message nor anything chained to it (cause, context, traceback) holds the key."""
    assert err.__cause__ is None
    assert err.__context__ is None
    assert API_KEY not in str(err)
    assert API_KEY not in repr(err)
    assert API_KEY not in "".join(traceback.format_exception(err))


def run(coro):  # noqa: ANN001, ANN201 - tiny sync shim, keeps tests free of an async plugin
    return asyncio.run(coro)


# ---------------------------------------------------------------- FunctionModel: typed output


def test_run_returns_typed_output_and_record() -> None:
    model = scripted({"column": "o_totalprice", "confidence": 0.9})
    llm = LLM(BASE_URL, API_KEY, model_override=model)

    out, record = run(llm.run("mapper", "strong", "You map columns.", "amt?", Answer))

    assert out == Answer(column="o_totalprice", confidence=0.9)
    assert isinstance(record, CallRecord)
    assert (record.role, record.tier, record.cached) == ("mapper", "strong", False)
    assert (record.input_tokens, record.output_tokens) == (50, 10)
    assert record.latency_ms >= 0
    assert llm.records == [record]


def test_run_unknown_model_is_priced_at_tier_primary_list_price() -> None:
    llm = LLM(BASE_URL, API_KEY, model_override=scripted({"column": "x", "confidence": 1.0}))

    _, record = run(llm.run("mapper", "strong", "s", "p", Answer))

    expected = prices.cost_usd(prices.TIER_PRIMARY["strong"], 50, 10)
    assert expected > 0
    assert record.cost_usd == pytest.approx(expected)


def test_run_repairs_invalid_output_once() -> None:
    bad, good = {"column": "", "confidence": 2.0}, {"column": "o_orderdate", "confidence": 0.5}
    model = scripted(bad, good)
    llm = LLM(BASE_URL, API_KEY, model_override=model)

    out, record = run(llm.run("mapper", "small", "s", "p", Answer))

    assert out.column == "o_orderdate"
    assert (record.input_tokens, record.output_tokens) == (100, 20)  # both attempts are billed


def test_run_invalid_output_twice_raises_output_error_with_record() -> None:
    bad = {"column": "", "confidence": 2.0}
    llm = LLM(BASE_URL, API_KEY, model_override=scripted(bad, bad, bad))

    with pytest.raises(LLMOutputError) as caught:
        run(llm.run("planner", "strong", "s", "p", Answer))

    assert caught.value.record is not None
    assert caught.value.record.output_tokens == 20  # first try + exactly one repair
    assert llm.records == [caught.value.record]


# ---------------------------------------------------------------- prices


def test_cost_from_table_is_exact() -> None:
    in_price, out_price = prices.PRICES["groq/openai/gpt-oss-120b"]

    cost = prices.cost_usd("groq/openai/gpt-oss-120b", 1_000_000, 500_000)

    assert cost == pytest.approx(in_price + out_price / 2)
    assert (in_price, out_price) == (0.15, 0.60)


def test_cost_table_is_dated_and_prices_free_tier_at_list() -> None:
    assert prices.PRICE_TABLE_DATE == "2026-10-28"
    assert prices.PRICES["gemini/gemini-3.8-flash"] == (0.75, 3.75)
    assert prices.cost_usd("openai/Qwen3.5-4B-MLX-4bit", 10_000, 10_000) == 0.0


def test_cost_unknown_model_raises() -> None:
    with pytest.raises(KeyError):
        prices.cost_usd("nope/not-a-model", 1, 1)


# ---------------------------------------------------------------- respx: the proxy boundary


def test_proxy_call_uses_tier_as_model_and_master_key_as_bearer() -> None:
    router = respx.Router(assert_all_called=False)
    route = router.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=completion(OK, prompt_tokens=1, completion_tokens=1),
        )
    )

    run(proxy_llm(router).run("mapper", "small", "s", "p", Answer))

    sent = route.calls.last.request
    assert json.loads(sent.content)["model"] == "small"
    assert sent.headers["authorization"] == f"Bearer {API_KEY}"


def test_proxy_cost_header_and_deployment_name_are_used() -> None:
    router = respx.Router(assert_all_called=False)
    router.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=completion(OK, prompt_tokens=1000, completion_tokens=100),
            headers={
                "x-litellm-response-cost": "0.000123",
                "x-litellm-model-name": "gemini/gemini-3.8-flash",
            },
        )
    )

    _, record = run(proxy_llm(router).run("verifier", "strong", "s", "p", Answer))

    assert record.model == "gemini/gemini-3.8-flash"
    assert record.cost_usd == pytest.approx(0.000123)
    assert (record.input_tokens, record.output_tokens, record.cached) == (1000, 100, False)


def test_proxy_without_cost_header_falls_back_to_price_table() -> None:
    router = respx.Router(assert_all_called=False)
    router.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=completion(OK, prompt_tokens=2000, completion_tokens=300),
            headers={"x-litellm-model-name": "groq/openai/gpt-oss-20b"},
        )
    )

    _, record = run(proxy_llm(router).run("worker", "small", "s", "p", Answer))

    assert record.cost_usd == pytest.approx(prices.cost_usd("groq/openai/gpt-oss-20b", 2000, 300))


def test_proxy_cache_hit_is_cached_and_free() -> None:
    router = respx.Router(assert_all_called=False)
    router.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json=completion(OK, prompt_tokens=800, completion_tokens=40),
            headers={
                "x-litellm-cache-key": "9f2c0d5e",
                "x-litellm-response-cost": "0.0",
                "x-litellm-model-name": "groq/openai/gpt-oss-120b",
            },
        )
    )

    _, record = run(proxy_llm(router).run("planner", "strong", "s", "p", Answer))

    assert record.cached is True
    assert record.cost_usd == 0.0


def test_proxy_503_on_all_routes_raises_unavailable() -> None:
    router = respx.Router(assert_all_called=False)
    route = router.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(
            503, json={"error": {"message": "No deployments available for selected model"}}
        )
    )
    llm = proxy_llm(router)

    with pytest.raises(LLMUnavailable) as caught:
        run(llm.run("planner", "strong", "s", "p", Answer))

    assert caught.value.status_code == 503
    assert route.call_count == 1  # LiteLLM owns retries/fallbacks; the client does not retry
    assert llm.records == []


def test_proxy_connection_error_raises_unavailable() -> None:
    router = respx.Router(assert_all_called=False)
    router.post(f"{BASE_URL}/v1/chat/completions").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(LLMUnavailable):
        run(proxy_llm(router).run("planner", "strong", "s", "p", Answer))


def test_api_key_never_appears_in_errors_or_records() -> None:
    router = respx.Router(assert_all_called=False)
    router.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(
            401,
            json={"error": {"message": f"Authentication Error, invalid key {API_KEY}"}},
        )
    )
    llm = proxy_llm(router)

    with pytest.raises(LLMUnavailable) as caught:
        run(llm.run("planner", "strong", "s", "p", Answer))

    err = caught.value
    assert err.status_code == 401
    assert_no_key_reachable(err)
    assert API_KEY not in repr(llm)

    ok = LLM(BASE_URL, API_KEY, model_override=scripted(OK))
    _, record = run(ok.run("mapper", "strong", "s", "p", Answer))
    assert API_KEY not in record.model_dump_json()


def test_connection_error_keeps_no_exception_chain() -> None:
    router = respx.Router(assert_all_called=False)
    router.post(f"{BASE_URL}/v1/chat/completions").mock(
        side_effect=httpx.ConnectError(f"refused, auth was {API_KEY}")
    )

    with pytest.raises(LLMUnavailable) as caught:
        run(proxy_llm(router).run("planner", "strong", "s", "p", Answer))

    assert_no_key_reachable(caught.value)


def test_output_error_keeps_no_exception_chain() -> None:
    # The invalid output echoes the key; pydantic's validation error would carry it as input.
    bad = {"column": API_KEY, "confidence": 2.0}
    llm = LLM(BASE_URL, API_KEY, model_override=scripted(bad, bad, bad))

    with pytest.raises(LLMOutputError) as caught:
        run(llm.run("planner", "strong", "s", "p", Answer))

    assert_no_key_reachable(caught.value)


def test_concurrent_calls_attribute_headers_to_their_own_record() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        cost = "0.5" if "first" in json.dumps(body["messages"]) else "0.25"
        return httpx.Response(
            200,
            json=completion(OK, prompt_tokens=1, completion_tokens=1),
            headers={"x-litellm-response-cost": cost},
        )

    router = respx.Router(assert_all_called=False)
    router.post(f"{BASE_URL}/v1/chat/completions").mock(side_effect=respond)
    llm = proxy_llm(router)

    async def both() -> list[tuple[Answer, CallRecord]]:
        return await asyncio.gather(
            llm.run("worker", "small", "s", "first", Answer),
            llm.run("worker", "small", "s", "second", Answer),
        )

    (_, first), (_, second) = run(both())

    assert (first.cost_usd, second.cost_usd) == (0.5, 0.25)


# ---------------------------------------------------------------- final review I-3: cache bypass


def _sent_body(cache: bool | None) -> dict[str, Any]:
    router = respx.Router(assert_all_called=False)
    route = router.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=completion(OK, prompt_tokens=1, completion_tokens=1))
    )
    client = httpx2.AsyncClient(transport=RespxBridge(router))
    kwargs = {} if cache is None else {"cache": cache}
    run(
        LLM(BASE_URL, API_KEY, http_client=client, **kwargs).run(
            "worker", "small", "s", "p", Answer
        )
    )
    return json.loads(route.calls.last.request.content)


def test_proxy_request_with_cache_disabled_carries_litellm_no_cache() -> None:
    assert _sent_body(cache=False)["cache"] == {"no-cache": True}


def test_proxy_request_uses_the_cache_by_default() -> None:
    assert "cache" not in _sent_body(cache=None)
    assert "cache" not in _sent_body(cache=True)


def test_request_timeout_is_below_the_per_file_wall_cap() -> None:
    """Final review minor #10: one hung call cannot by itself double a file's wall time."""
    from sluice.agent.caps import MAX_WALL_S_PER_FILE
    from sluice.llm.client import REQUEST_TIMEOUT_S

    assert REQUEST_TIMEOUT_S < MAX_WALL_S_PER_FILE
    llm = LLM(BASE_URL, API_KEY)
    assert llm._http.timeout.read == REQUEST_TIMEOUT_S
    run(llm.aclose())


# ---------------------------------------------------------------- structured-output mode (gpt-oss)

PARSE_FAILED = {
    # LiteLLM 1.106.5 relaying Groq's 400 when gpt-oss wrote prose instead of the structured output
    "error": {
        "message": (
            'litellm.BadRequestError: GroqException - {"error":{"message":"Parsing failed. The '
            'model generated output that could not be parsed.","type":'
            '"invalid_request_error","code":"output_parse_failed",'
            f'"failed_generation":"We need to write a transform function {API_KEY}"}}}}'
        ),
        "type": None,
        "param": None,
        "code": "400",
    }
}


def _parse_failed() -> httpx.Response:
    return httpx.Response(400, json=PARSE_FAILED)


def _ok() -> httpx.Response:
    return httpx.Response(200, json=completion(OK, prompt_tokens=7, completion_tokens=3))


def test_default_output_mode_sends_json_schema_response_format_and_no_tools() -> None:
    router = respx.Router(assert_all_called=False)
    route = router.post(f"{BASE_URL}/v1/chat/completions").mock(return_value=_ok())

    out, _ = run(proxy_llm(router).run("mapper", "strong", "s", "p", Answer))

    body = json.loads(route.calls.last.request.content)
    assert out == Answer(**OK)
    assert body["response_format"]["type"] == "json_schema"
    assert set(body["response_format"]["json_schema"]["schema"]["properties"]) == {
        "column",
        "confidence",
    }
    assert "tools" not in body
    assert "tool_choice" not in body


def test_tool_output_mode_sends_the_output_tool() -> None:
    router = respx.Router(assert_all_called=False)
    route = router.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(
            200, json=tool_completion(OK, prompt_tokens=1, completion_tokens=1)
        )
    )

    out, _ = run(proxy_llm(router, output_mode="tool").run("mapper", "strong", "s", "p", Answer))

    body = json.loads(route.calls.last.request.content)
    assert out == Answer(**OK)
    assert body["tools"][0]["function"]["name"] == "final_result"
    assert "response_format" not in body


def test_output_parse_failed_is_retried_once_with_a_json_only_nudge() -> None:
    router = respx.Router(assert_all_called=False)
    route = router.post(f"{BASE_URL}/v1/chat/completions").mock(
        side_effect=[_parse_failed(), _ok()]
    )
    llm = proxy_llm(router)

    out, record = run(llm.run("worker", "small", "s", "map amt", Answer))

    assert out == Answer(**OK)
    assert route.call_count == 2
    retried = json.dumps(json.loads(route.calls.last.request.content)["messages"])
    assert "only the JSON object" in retried
    assert (record.input_tokens, record.output_tokens) == (7, 3)
    assert llm.records == [record]


def test_output_parse_failed_twice_raises_output_error_not_unavailable() -> None:
    router = respx.Router(assert_all_called=False)
    route = router.post(f"{BASE_URL}/v1/chat/completions").mock(return_value=_parse_failed())
    llm = proxy_llm(router)

    with pytest.raises(LLMOutputError) as caught:
        run(llm.run("worker", "small", "s", "p", Answer))

    assert route.call_count == 2
    assert caught.value.record is not None
    assert llm.records == [caught.value.record]
    assert "output_parse_failed" in str(caught.value)
    assert_no_key_reachable(caught.value)


def test_other_400_is_still_unavailable_and_not_retried() -> None:
    router = respx.Router(assert_all_called=False)
    route = router.post(f"{BASE_URL}/v1/chat/completions").mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad model", "code": "400"}})
    )

    with pytest.raises(LLMUnavailable) as caught:
        run(proxy_llm(router).run("worker", "small", "s", "p", Answer))

    assert caught.value.status_code == 400
    assert route.call_count == 1


def test_native_output_sends_strict_false_for_groq_best_effort_mode() -> None:
    # Groq strict mode rejects schemas with optional fields; best-effort accepts any schema.
    router = respx.Router(assert_all_called=False)
    route = router.post(f"{BASE_URL}/v1/chat/completions").mock(return_value=_ok())

    run(proxy_llm(router).run("mapper", "strong", "s", "p", Answer))

    schema = json.loads(route.calls.last.request.content)["response_format"]["json_schema"]
    assert schema["strict"] is False


def test_json_validate_failed_is_also_a_retryable_generation_error() -> None:
    router = respx.Router(assert_all_called=False)
    route = router.post(f"{BASE_URL}/v1/chat/completions").mock(
        side_effect=[
            httpx.Response(
                400,
                json={"error": {"message": "GroqException - json_validate_failed", "code": "400"}},
            ),
            _ok(),
        ]
    )

    out, _ = run(proxy_llm(router).run("worker", "small", "s", "p", Answer))

    assert out == Answer(**OK)
    assert route.call_count == 2


def test_prompted_output_mode_puts_the_schema_in_the_prompt() -> None:
    router = respx.Router(assert_all_called=False)
    route = router.post(f"{BASE_URL}/v1/chat/completions").mock(return_value=_ok())

    out, _ = run(
        proxy_llm(router, output_mode="prompted").run("mapper", "strong", "s", "p", Answer)
    )

    body = json.loads(route.calls.last.request.content)
    assert out == Answer(**OK)
    assert "confidence" in json.dumps(body["messages"])
    assert "tools" not in body
    assert body.get("response_format", {}).get("type") != "json_schema"
