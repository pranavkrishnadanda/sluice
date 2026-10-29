"""``sluice.api.errors``: the contract error type lives outside ``app`` (no import cycle)."""

import ast
from pathlib import Path

import sluice.api as api_pkg
from sluice.api import app, errors


def test_app_reexports_errors() -> None:
    assert app.ApiError is errors.ApiError
    assert app.error_response is errors.error_response


def test_error_response_omits_missing_code() -> None:
    assert errors.error_response(404, "Not found.").body == b'{"detail":"Not found."}'


def test_route_modules_do_not_import_app() -> None:
    root = Path(api_pkg.__file__).parent
    for module in sorted(root.glob("routes_*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        assert "sluice.api.app" not in imported, module.name
