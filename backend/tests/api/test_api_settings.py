"""``ApiSettings``: API knobs from ``SLUICE_API_*`` (and ``SLUICE_UI_ORIGINS``), no secrets."""

import pytest
from pydantic import ValidationError

from sluice.api.settings import DEFAULT_UI_ORIGINS, ApiSettings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "SLUICE_UI_ORIGINS",
        "SLUICE_API_UI_ORIGINS",
        "UI_ORIGINS",
        "SLUICE_API_MAX_UPLOAD_BYTES",
        "SLUICE_API_CONCURRENCY",
        "SLUICE_API_KEEPALIVE_S",
        "SLUICE_API_LANDING_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)


def test_api_settings_defaults() -> None:
    settings = ApiSettings()
    assert settings.ui_origins == list(DEFAULT_UI_ORIGINS)
    assert settings.max_upload_bytes == 100 * 1024 * 1024
    assert settings.concurrency == 1
    assert settings.keepalive_s == 15.0
    assert settings.landing_bucket == "sluice-landing"


def test_default_ui_origins_cover_dev_and_preview_ports() -> None:
    assert DEFAULT_UI_ORIGINS == (
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:3100",
        "http://127.0.0.1:3100",
    )


def test_ui_origins_reads_comma_separated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLUICE_UI_ORIGINS", " http://a.test, http://b.test ,,")
    assert ApiSettings().ui_origins == ["http://a.test", "http://b.test"]


def test_ui_origins_ignores_prefixed_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLUICE_API_UI_ORIGINS", "http://a.test")
    assert ApiSettings().ui_origins == list(DEFAULT_UI_ORIGINS)


def test_ui_origins_ignores_unprefixed_field_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UI_ORIGINS", "http://a.test")
    assert ApiSettings().ui_origins == list(DEFAULT_UI_ORIGINS)


def test_ui_origins_ignores_lowercase_field_name_kwarg() -> None:
    # Only SLUICE_UI_ORIGINS sets the allow-list; `ui_origins=` is not an accepted input.
    assert ApiSettings(ui_origins=["http://a.test"]).ui_origins == list(DEFAULT_UI_ORIGINS)


def test_ui_origins_rejects_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLUICE_UI_ORIGINS", "*")
    with pytest.raises(ValidationError):
        ApiSettings()


def test_ui_origins_rejects_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    # Browsers send Origin without a path; "http://a.test/" would silently never match.
    monkeypatch.setenv("SLUICE_UI_ORIGINS", "http://a.test/")
    with pytest.raises(ValidationError):
        ApiSettings()


def test_prefixed_env_overrides_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLUICE_API_CONCURRENCY", "4")
    monkeypatch.setenv("SLUICE_API_MAX_UPLOAD_BYTES", "1024")
    settings = ApiSettings()
    assert (settings.concurrency, settings.max_upload_bytes) == (4, 1024)


@pytest.mark.parametrize("value", ["0", "5"])
def test_concurrency_is_bounded(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SLUICE_API_CONCURRENCY", value)
    with pytest.raises(ValidationError):
        ApiSettings()


@pytest.mark.parametrize(("field", "value"), [("max_upload_bytes", 0), ("keepalive_s", 0.0)])
def test_positive_knobs_reject_zero(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        ApiSettings(**{field: value})


def test_api_settings_holds_no_secret_fields() -> None:
    names = set(ApiSettings.model_fields)
    assert names == {
        "ui_origins",
        "max_upload_bytes",
        "concurrency",
        "keepalive_s",
        "landing_bucket",
    }
