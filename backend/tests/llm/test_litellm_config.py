"""backend/litellm/config.yaml: cooldown thresholds are scoped per deployment (Ruling M2).

No router-wide `allowed_fails` / `cooldown_time` override: LiteLLM's defaults apply to anything
not covered below. The local worker cools down on its first failure; hosted deployments carry
deliberate, more tolerant per-deployment policies so a short 429 burst does not eject them.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

CONFIG = Path(__file__).resolve().parents[2] / "litellm" / "config.yaml"

HOSTED_POLICY = {
    "RateLimitErrorAllowedFails": 3,
    "InternalServerErrorAllowedFails": 2,
    "ServiceUnavailableErrorAllowedFails": 2,
    "TimeoutErrorAllowedFails": 2,
}
LOCAL_POLICY = {
    "InternalServerErrorAllowedFails": 0,
    "ServiceUnavailableErrorAllowedFails": 0,
    "TimeoutErrorAllowedFails": 0,
}


@pytest.fixture(scope="module")
def config() -> dict[str, Any]:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _deployment(config: dict[str, Any], group: str) -> dict[str, Any]:
    (entry,) = [m for m in config["model_list"] if m["model_name"] == group]
    return entry


def test_no_router_wide_cooldown_overrides(config: dict[str, Any]) -> None:
    router = config["router_settings"]

    assert "allowed_fails" not in router
    assert "allowed_fails_policy" not in router
    assert "cooldown_time" not in router


def test_local_worker_cools_down_on_first_failure(config: dict[str, Any]) -> None:
    info = _deployment(config, "small")["model_info"]

    assert info["allowed_fails_policy"] == LOCAL_POLICY
    assert info["cooldown_time"] == 60
    assert config["router_settings"]["model_group_retry_policy"]["small"]["DefaultRetries"] == 0


@pytest.mark.parametrize("group", ["strong", "strong-fallback", "small-fallback"])
def test_hosted_deployments_have_tolerant_policies(config: dict[str, Any], group: str) -> None:
    info = _deployment(config, group)["model_info"]

    assert info["allowed_fails_policy"] == HOSTED_POLICY
    assert info["cooldown_time"] == 30


def test_policies_live_in_model_info_not_litellm_params(config: dict[str, Any]) -> None:
    # litellm_params is copied into the provider request; LiteLLM reads the policy from model_info.
    for entry in config["model_list"]:
        assert "allowed_fails_policy" not in entry["litellm_params"]
        assert "cooldown_time" not in entry["litellm_params"]


# --- timeout nesting: a hung primary must reach its fallback inside the client timeout -------

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
    """Every attempt of the primary and of its fallback group hangs until its timeout."""
    return sum(
        _timeout(config, group) * (1 + _timeout_retries(config, group))
        for group in (tier, f"{tier}-fallback")
    )


def test_every_deployment_has_its_own_short_timeout(config: dict[str, Any]) -> None:
    assert _deployment(config, "small")["litellm_params"]["timeout"] == 20
    for group in ("strong", "strong-fallback", "small-fallback"):
        assert _deployment(config, group)["litellm_params"]["timeout"] == 30


@pytest.mark.parametrize("group", ["small", "small-fallback", "strong", "strong-fallback"])
def test_a_timeout_is_retried_at_most_once(config: dict[str, Any], group: str) -> None:
    assert _timeout_retries(config, group) <= 1


@pytest.mark.parametrize("tier", TIERS)
def test_client_timeout_covers_the_worst_case_proxy_path(config: dict[str, Any], tier: str) -> None:
    from sluice.llm.client import REQUEST_TIMEOUT_S

    assert worst_case_proxy_s(config, tier) < REQUEST_TIMEOUT_S


def test_wall_cap_exceeds_the_client_timeout() -> None:
    from sluice.agent.caps import MAX_WALL_S_PER_FILE
    from sluice.llm.client import REQUEST_TIMEOUT_S

    assert MAX_WALL_S_PER_FILE > REQUEST_TIMEOUT_S
