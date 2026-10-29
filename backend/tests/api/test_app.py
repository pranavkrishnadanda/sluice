"""App skeleton: health, contract error bodies on every 4xx/5xx, and CORS on every response.

Tests use ``TestClient`` (in-process ASGI); nothing binds a port.
"""

import logging
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sluice.api.app import ApiError, create_app, redacted_traceback
from sluice.api.settings import ApiSettings

UI_ORIGIN = "http://localhost:3100"
EVIL_ORIGIN = "http://evil.test"
SECRET = "sk-live-do-not-leak-0123456789"
CAUSE_SECRET = "cause-secret-9f8e7d"
CONTEXT_SECRET = "context-secret-1a2b3c"


def _app() -> FastAPI:
    app = create_app(settings=ApiSettings(), worker=False)

    @app.get("/_test/items/{item_id}")
    def _item(item_id: int, limit: int = 10) -> dict[str, int]:
        return {"item": item_id, "limit": limit}

    @app.get("/_test/boom")
    def _boom() -> None:
        raise RuntimeError(f"database password is {SECRET}")

    @app.get("/_test/chained")
    def _chained() -> None:
        try:
            try:
                raise KeyError(f"context {CONTEXT_SECRET}")
            except KeyError as context:
                raise ValueError(f"cause {CAUSE_SECRET}") from context
        except ValueError as cause:
            raise LookupError(f"outer {SECRET}") from cause

    @app.get("/_test/api-error")
    def _api_error() -> None:
        raise ApiError(409, "The preview is not ready yet.", code="not_ready")

    @app.get("/_test/api-error-no-code")
    def _api_error_no_code() -> None:
        raise ApiError(400, "Bad thing.")

    return app


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(_app(), raise_server_exceptions=False) as test_client:
        yield test_client


def test_healthz_returns_ok(client: TestClient) -> None:
    response = client.get("/healthz")
    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_unknown_route_returns_contract_404(client: TestClient) -> None:
    response = client.get("/no/such/route")
    body = response.json()
    assert response.status_code == 404
    assert set(body) == {"detail", "code"}
    assert isinstance(body["detail"], str) and body["code"] == "not_found"


def test_wrong_method_returns_contract_405_with_allow_header(client: TestClient) -> None:
    response = client.post("/healthz")
    assert response.status_code == 405
    assert response.json()["code"] == "method_not_allowed"
    assert "GET" in response.headers["allow"]


def test_validation_error_returns_string_detail_not_list(client: TestClient) -> None:
    response = client.get("/_test/items/abc")
    body = response.json()
    assert response.status_code == 422
    assert set(body) == {"detail", "code"}
    assert isinstance(body["detail"], str) and body["code"] == "invalid_request"


def test_validation_error_detail_names_the_field(client: TestClient) -> None:
    response = client.get("/_test/items/1", params={"limit": "many"})
    assert "limit" in response.json()["detail"]


def test_validation_error_detail_does_not_echo_input(client: TestClient) -> None:
    response = client.get("/_test/items/1", params={"limit": SECRET})
    assert SECRET not in response.text


def test_unhandled_exception_returns_generic_500(client: TestClient) -> None:
    response = client.get("/_test/boom")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal error.", "code": "internal"}


def test_unhandled_exception_leaks_no_secret_or_traceback(client: TestClient) -> None:
    response = client.get("/_test/boom")
    assert SECRET not in response.text
    assert "Traceback" not in response.text and "RuntimeError" not in response.text


def _logged(caplog: pytest.LogCaptureFixture) -> str:
    """Everything a handler could write: formatted text plus each record's message and exc_info."""
    parts = [caplog.text]
    for record in caplog.records:
        parts.append(record.getMessage())
        parts.append(repr(record.exc_info))
        parts.append(str(record.exc_text))
    return "\n".join(parts)


def test_unhandled_exception_log_names_type_method_and_path(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="sluice.api.app"):
        client.get("/_test/boom")
    logged = _logged(caplog)
    assert "RuntimeError" in logged and "GET" in logged and "/_test/boom" in logged


def test_unhandled_exception_log_has_frame_locations(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="sluice.api.app"):
        client.get("/_test/boom")
    assert "test_app.py:" in _logged(caplog) and "_boom" in _logged(caplog)


def test_unhandled_exception_log_omits_exception_message(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        client.get("/_test/boom")
    assert SECRET not in _logged(caplog)


def test_chained_exception_log_names_types_without_messages(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = client.get("/_test/chained")
    logged = _logged(caplog)
    assert response.status_code == 500
    assert all(name in logged for name in ("LookupError", "ValueError", "KeyError"))
    assert not any(secret in logged for secret in (SECRET, CAUSE_SECRET, CONTEXT_SECRET))


def test_api_error_returns_its_status_detail_and_code(client: TestClient) -> None:
    response = client.get("/_test/api-error")
    assert response.status_code == 409
    assert response.json() == {"detail": "The preview is not ready yet.", "code": "not_ready"}


def test_api_error_without_code_omits_code(client: TestClient) -> None:
    response = client.get("/_test/api-error-no-code")
    assert (response.status_code, response.json()) == (400, {"detail": "Bad thing."})


@pytest.mark.parametrize("status", [200, 399, 600])
def test_api_error_rejects_non_error_status(status: int) -> None:
    with pytest.raises(ValueError):
        ApiError(status, "nope")


def test_preflight_from_ui_origin_allows_last_event_id(client: TestClient) -> None:
    response = client.options(
        "/runs",
        headers={
            "Origin": UI_ORIGIN,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "last-event-id",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == UI_ORIGIN
    assert "last-event-id" in response.headers["access-control-allow-headers"].lower()


def test_preflight_sends_no_credentials_header(client: TestClient) -> None:
    response = client.options(
        "/runs",
        headers={"Origin": UI_ORIGIN, "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-credentials" not in response.headers


def test_preflight_from_unknown_origin_is_not_allowed(client: TestClient) -> None:
    response = client.options(
        "/runs",
        headers={"Origin": EVIL_ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("path", ["/healthz", "/no/such/route", "/_test/items/abc", "/_test/boom"])
def test_ui_origin_gets_cors_header_on_every_response(client: TestClient, path: str) -> None:
    response = client.get(path, headers={"Origin": UI_ORIGIN})
    assert response.headers.get("access-control-allow-origin") == UI_ORIGIN


@pytest.mark.parametrize("path", ["/healthz", "/no/such/route", "/_test/boom"])
def test_unknown_origin_gets_no_cors_header(client: TestClient, path: str) -> None:
    response = client.get(path, headers={"Origin": EVIL_ORIGIN})
    assert "access-control-allow-origin" not in response.headers


def test_create_app_without_settings_reads_api_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLUICE_UI_ORIGINS", "http://example.test")
    with TestClient(create_app(worker=False)) as test_client:
        response = test_client.get("/healthz", headers={"Origin": "http://example.test"})
    assert response.headers["access-control-allow-origin"] == "http://example.test"


def test_create_app_keeps_dependencies_on_state() -> None:
    store, blobs, settings = object(), object(), ApiSettings()
    app = create_app(settings=settings, store=store, blobs=blobs, worker=False)
    assert (app.state.settings, app.state.store, app.state.blobs) == (settings, store, blobs)
    assert app.state.worker_enabled is False


def test_redacted_traceback_follows_implicit_context_without_messages() -> None:
    try:
        try:
            raise KeyError(CONTEXT_SECRET)
        except KeyError:
            raise LookupError(SECRET)  # noqa: B904 - the implicit __context__ is the point
    except LookupError as exc:
        text = redacted_traceback(exc)
    assert "LookupError" in text and "during handling of:" in text and "KeyError" in text
    assert SECRET not in text and CONTEXT_SECRET not in text


def test_redacted_traceback_stops_on_a_cycle() -> None:
    first, second = ValueError(SECRET), KeyError(CAUSE_SECRET)
    first.__cause__, second.__cause__ = second, first
    assert redacted_traceback(first).count("ValueError") == 1
