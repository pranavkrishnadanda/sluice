"""The CLI owns its terminal output, so Pydantic AI's first-run banner is off for it.

The library module (`sluice.llm.client`) must not change that process-wide setting itself.
Each check runs in a fresh interpreter, so no module is reloaded inside the test session.
"""

import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _banner_after_import(module: str) -> str:
    code = f"import pydantic_ai, {module}; print(pydantic_ai.BANNER_ENABLED)"
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    )
    return out.stdout.strip()


def test_cli_entrypoint_disables_pydantic_ai_banner() -> None:
    assert _banner_after_import("sluice.cli") == "False"


def test_llm_client_import_leaves_banner_setting_alone() -> None:
    assert _banner_after_import("sluice.llm.client") == "True"
