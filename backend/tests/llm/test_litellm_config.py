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
