"""Dated list-price table for the models behind the LiteLLM groups, and cost from token counts.

Keys are LiteLLM deployment strings exactly as in `backend/litellm/config.yaml` (the proxy reports
the one that served a call in `x-litellm-model-name`). Values are USD per 1M (input, output)
tokens at the provider's *list* price. Free-tier calls are priced at list too (spec §7.4), so cost
curves reflect real-world cost; actual spend on the free tiers is $0 and is reported separately.

Sources (read 2026-10-28):
- Groq: https://console.groq.com/docs/models ("Price per 1M tokens" column).
- Google AI Studio: https://ai.google.dev/gemini-api/docs/pricing (Standard, paid tier, text),
  page "Last updated 2026-10-27 UTC". Gemini 3.8 Flash list price rises to $1.50 / $7.50 on
  2027-01-01: re-date this table then.
- Local vLLM worker: self-hosted, no list price, so 0.
"""

from typing import Final

PRICE_TABLE_DATE: Final = "2026-10-28"

PRICES: Final[dict[str, tuple[float, float]]] = {
    # strong primary (Groq, production model)
    "groq/openai/gpt-oss-120b": (0.15, 0.60),
    # strong fallback (Google AI Studio, stable Flash)
    "gemini/gemini-3.8-flash": (0.75, 3.75),
    # small primary (local vLLM via vllm-metal)
    "openai/Qwen3.5-4B-MLX-4bit": (0.0, 0.0),
    # small fallback (Groq, production model)
    "groq/openai/gpt-oss-20b": (0.075, 0.30),
}

TIER_PRIMARY: Final[dict[str, str]] = {
    "strong": "groq/openai/gpt-oss-120b",
    "small": "openai/Qwen3.5-4B-MLX-4bit",
}
"""Deployment a tier is priced as when the serving deployment is unknown (no proxy header)."""

_PER_TOKEN: Final = 1 / 1_000_000


def cost_usd(model: str, in_tok: int, out_tok: int) -> float:
    """USD for `in_tok` input and `out_tok` output tokens of `model`. Unknown model: `KeyError`."""
    in_price, out_price = PRICES[model]
    return (in_tok * in_price + out_tok * out_price) * _PER_TOKEN
