"""The LiteLLM proxy from compose.yaml, end to end.

- `integration` (needs `make up`): the proxy boots with backend/litellm/config.yaml and serves both
  tiers. No provider keys are needed for this.
- `live` (needs `make up` plus real `GROQ_API_KEY`; `GEMINI_API_KEY` for the strong fallback):
  one typed call per tier. Run with `cd backend && uv run python -m pytest -m live`. Skipped
  without keys.
"""

import asyncio

import httpx
import pytest
from pydantic import BaseModel, Field, ValidationError

from sluice.llm.client import LLM, Tier
from sluice.settings import Settings, get_settings


class Capital(BaseModel):
    country: str = Field(min_length=1)
    capital: str = Field(min_length=1)


def _settings() -> Settings:
    try:
        return get_settings()
    except ValidationError:
        pytest.skip("backend/.env (or the environment) lacks the service settings")


@pytest.mark.integration
def test_proxy_liveliness_and_tier_groups() -> None:
    s = _settings()
    headers = {"Authorization": f"Bearer {s.litellm_master_key.get_secret_value()}"}

    live = httpx.get(f"{s.litellm_url}/health/liveliness", timeout=5)
    models = httpx.get(f"{s.litellm_url}/v1/models", headers=headers, timeout=5)

    assert live.status_code == 200
    ids = {m["id"] for m in models.json()["data"]}
    assert {"strong", "small", "strong-fallback", "small-fallback"} <= ids


@pytest.mark.live
@pytest.mark.parametrize("tier", ["strong", "small"])
def test_live_tier_returns_valid_object(tier: Tier) -> None:
    s = _settings()
    if s.groq_api_key is None:
        pytest.skip("GROQ_API_KEY not set")

    async def call() -> Capital:
        async with LLM(s.litellm_url, s.litellm_master_key.get_secret_value()) as llm:
            out, record = await llm.run(
                "live-check",
                tier,
                "Answer with the requested fields only.",
                "What is the capital of France?",
                Capital,
            )
        assert record.tier == tier
        assert record.input_tokens > 0 or record.cached
        return out

    out = asyncio.run(call())

    assert out.capital.strip().lower() == "paris"
