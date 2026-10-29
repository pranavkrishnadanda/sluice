"""backend/litellm/config.yaml: routes, rate limits, cooldown policies and timeout nesting.

Owner decisions of 2026-10-29: no local vLLM (its deployment stays as a commented block), the
Gemini key's prepaid credit is depleted (HTTP 402), so a free Groq qwen model is the last resort.

No router-wide `allowed_fails` / `cooldown_time` override: LiteLLM's defaults apply to anything
not covered below. Hosted deployments carry tolerant per-deployment policies so a short 429 burst
does not eject them.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from sluice.llm import prices

CONFIG = Path(__file__).resolve().parents[2] / "litellm" / "config.yaml"

HOSTED_POLICY = {
    "RateLimitErrorAllowedFails": 3,
    "InternalServerErrorAllowedFails": 2,
    "ServiceUnavailableErrorAllowedFails": 2,
    "TimeoutErrorAllowedFails": 2,
}
GROUPS = ("strong", "strong-fallback", "small", "small-fallback")
DEPLOYMENTS = {
    "strong": "groq/openai/gpt-oss-120b",
    "strong-fallback": "gemini/gemini-3.8-flash",
    "small": "groq/openai/gpt-oss-20b",
    "small-fallback": "groq/qwen/qwen3.8-27b",
}
GROQ_GROUPS = ("strong", "small", "small-fallback")
CHAINS = {
    "strong": ["strong-fallback", "small-fallback"],
    "small": ["small-fallback"],
}


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _deployment(config: dict[str, Any], group: str) -> dict[str, Any]:
    (entry,) = [m for m in config["model_list"] if m["model_name"] == group]
    return entry


def _fallbacks(config: dict[str, Any]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    for entry in config["router_settings"]["fallbacks"]:
        merged.update(entry)
    return merged


# --- routes ---------------------------------------------------------------------------------


def test_each_group_has_one_deployment_on_the_chosen_model(config: dict[str, Any]) -> None:
    served = {m["model_name"]: m["litellm_params"]["model"] for m in config["model_list"]}

    assert served == DEPLOYMENTS


def test_fallback_chains_end_on_free_groq_qwen(config: dict[str, Any]) -> None:
    assert _fallbacks(config) == CHAINS


def test_local_vllm_deployment_is_kept_only_as_a_commented_block(config: dict[str, Any]) -> None:
    text = CONFIG.read_text(encoding="utf-8")
    commented = [line for line in text.splitlines() if line.lstrip().startswith("#")]
    models = [m["litellm_params"]["model"] for m in config["model_list"]]

    assert all("Qwen3.5-4B-MLX-4bit" not in model for model in models)
    assert any("model: openai/Qwen3.5-4B-MLX-4bit" in line for line in commented)
    assert any("host.docker.internal:58000" in line for line in commented)


def test_every_deployment_has_a_list_price(config: dict[str, Any]) -> None:
    for entry in config["model_list"]:
        assert entry["litellm_params"]["model"] in prices.PRICES


def test_tier_primary_prices_match_the_primary_deployments() -> None:
    primaries = {tier: DEPLOYMENTS[tier] for tier in ("strong", "small")}
    assert primaries == prices.TIER_PRIMARY


# --- rate limits ----------------------------------------------------------------------------


@pytest.mark.parametrize("group", GROQ_GROUPS)
def test_groq_deployments_carry_the_free_plan_minute_limits(
    config: dict[str, Any], group: str
) -> None:
    params = _deployment(config, group)["litellm_params"]

    assert (params["rpm"], params["tpm"]) == (30, 8000)


def test_gemini_carries_the_owner_console_limits(config: dict[str, Any]) -> None:
    params = _deployment(config, "strong-fallback")["litellm_params"]

    assert (params["rpm"], params["tpm"]) == (1000, 2_000_000)


# --- cooldown and retry policies ------------------------------------------------------------


def test_no_router_wide_cooldown_overrides(config: dict[str, Any]) -> None:
    router = config["router_settings"]

    assert "allowed_fails" not in router
    assert "allowed_fails_policy" not in router
    assert "cooldown_time" not in router


@pytest.mark.parametrize("group", GROQ_GROUPS)
def test_groq_deployments_have_tolerant_policies(config: dict[str, Any], group: str) -> None:
    info = _deployment(config, group)["model_info"]

    assert info["allowed_fails_policy"] == HOSTED_POLICY
    assert info["cooldown_time"] == 30


def test_gemini_cools_down_on_its_first_payment_required(config: dict[str, Any]) -> None:
    # LiteLLM 1.106.5 maps Gemini's 402 to PaymentRequiredError, a BadRequestError: not retried
    # (BadRequestErrorRetries 0), the router moves on to the next fallback, and with 0 allowed
    # fails the deployment is skipped for its cooldown instead of being asked again each call.
    info = _deployment(config, "strong-fallback")["model_info"]

    assert info["allowed_fails_policy"] == {**HOSTED_POLICY, "BadRequestErrorAllowedFails": 0}
    assert info["cooldown_time"] == 30


def test_a_bad_request_including_402_is_never_retried_in_place(config: dict[str, Any]) -> None:
    router = config["router_settings"]

    assert router["retry_policy"]["BadRequestErrorRetries"] == 0
    assert "model_group_retry_policy" not in router


def test_policies_live_in_model_info_not_litellm_params(config: dict[str, Any]) -> None:
    # litellm_params is copied into the provider request; LiteLLM reads the policy from model_info.
    for entry in config["model_list"]:
        assert "allowed_fails_policy" not in entry["litellm_params"]
        assert "cooldown_time" not in entry["litellm_params"]


# --- timeout nesting: a hung primary must reach its last fallback inside the client timeout ---

TIERS = ("small", "strong")


def _timeout(config: dict[str, Any], group: str) -> float:
    """The per-call timeout LiteLLM applies to ``group`` (deployment param, else router)."""
    params = _deployment(config, group)["litellm_params"]
    return float(params.get("timeout", config["router_settings"]["timeout"]))


def _timeout_retries(config: dict[str, Any], group: str) -> int:
    router = config["router_settings"]
    per_group = router.get("model_group_retry_policy", {}).get(group, {})
    return int(per_group.get("TimeoutErrorRetries", router["retry_policy"]["TimeoutErrorRetries"]))


def worst_case_proxy_s(config: dict[str, Any], tier: str) -> float:
    """Timeout-only worst case: every attempt of each group in the chain hangs until it times out.

    A timeout is not retried in place, so this is one attempt per group. It does not cover slow
    retried errors (429/500/503 that each take the full per-attempt timeout); see
    `slow_error_worst_case_s`.
    """
    return sum(
        _timeout(config, group) * (1 + _timeout_retries(config, group))
        for group in (tier, *_fallbacks(config)[tier])
    )


def _retried_error_retries(config: dict[str, Any]) -> int:
    policy = config["router_settings"]["retry_policy"]
    return max(
        int(policy[key])
        for key in (
            "RateLimitErrorRetries",
            "InternalServerErrorRetries",
            "ServiceUnavailableErrorRetries",
        )
    )


def slow_error_worst_case_s(config: dict[str, Any], tier: str) -> float:
    """Realistic bound with retried errors: every attempt fails slowly, at the full timeout.

    Each group in the chain gets 1 + its 429/5xx retries attempts, each up to its timeout.
    """
    attempts = 1 + _retried_error_retries(config)
    return sum(_timeout(config, group) * attempts for group in (tier, *_fallbacks(config)[tier]))


@pytest.mark.parametrize("tier", TIERS)
def test_slow_error_bound_matches_the_documented_constant(
    config: dict[str, Any], tier: str
) -> None:
    from sluice.llm.client import SLOW_ERROR_WORST_CASE_S

    assert slow_error_worst_case_s(config, tier) == SLOW_ERROR_WORST_CASE_S[tier]


@pytest.mark.parametrize("tier", TIERS)
def test_slow_errors_can_hit_the_client_timeout_and_degrade(
    config: dict[str, Any], tier: str
) -> None:
    # Documented, not a bug: such a call ends at REQUEST_TIMEOUT_S as LLMUnavailable and the file
    # falls back to rules-only output for that step. Never wrong output.
    from sluice.llm.client import REQUEST_TIMEOUT_S

    assert slow_error_worst_case_s(config, tier) >= REQUEST_TIMEOUT_S


STRONG_GROUPS = ("strong", "strong-fallback")
"""The strong chain's own deployments: large files need more than 30 s (round 2, item 7)."""
SMALL_GROUPS = ("small", "small-fallback")


@pytest.mark.parametrize("group", STRONG_GROUPS)
def test_strong_deployments_allow_60_s_per_attempt(config: dict[str, Any], group: str) -> None:
    assert _deployment(config, group)["litellm_params"]["timeout"] == 60


@pytest.mark.parametrize("group", SMALL_GROUPS)
def test_small_deployments_keep_their_short_timeout(config: dict[str, Any], group: str) -> None:
    assert _deployment(config, group)["litellm_params"]["timeout"] == 30


@pytest.mark.parametrize("group", GROUPS)
def test_a_timeout_is_retried_at_most_once(config: dict[str, Any], group: str) -> None:
    assert _timeout_retries(config, group) <= 1


def test_worst_case_paths_are_the_documented_ones(config: dict[str, Any]) -> None:
    assert worst_case_proxy_s(config, "strong") == 150  # 120b 60 + Gemini 60 + qwen 30
    assert worst_case_proxy_s(config, "small") == 60  # 20b, qwen: 30 s each


@pytest.mark.parametrize("tier", TIERS)
def test_client_timeout_covers_the_worst_case_proxy_path(config: dict[str, Any], tier: str) -> None:
    from sluice.llm.client import REQUEST_TIMEOUT_S

    assert worst_case_proxy_s(config, tier) < REQUEST_TIMEOUT_S


def test_client_timeout_is_180_s_and_the_wall_cap_300_s() -> None:
    from sluice.agent.caps import MAX_WALL_S_PER_FILE
    from sluice.llm.client import REQUEST_TIMEOUT_S

    assert (REQUEST_TIMEOUT_S, MAX_WALL_S_PER_FILE) == (180.0, 300)


def test_wall_cap_exceeds_the_client_timeout() -> None:
    from sluice.agent.caps import MAX_WALL_S_PER_FILE
    from sluice.llm.client import REQUEST_TIMEOUT_S

    assert MAX_WALL_S_PER_FILE > REQUEST_TIMEOUT_S
