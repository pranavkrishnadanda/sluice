from pathlib import Path

import pytest
from pydantic import ValidationError

from sluice.settings import Settings, get_settings

# Dummy, non-secret values that are easy to spot if they leak into repr/str.
SECRETS = {
    "GROQ_API_KEY": "groq-dummy-7f3a",
    "GEMINI_API_KEY": "gemini-dummy-9c1b",
    "LITELLM_MASTER_KEY": "sk-litellm-dummy-42aa",
    "POSTGRES_PASSWORD": "pg-dummy-pass-5e6d",
    "RUSTFS_ACCESS_KEY": "rustfs-access-dummy-11",
    "RUSTFS_SECRET_KEY": "rustfs-secret-dummy-22",
}
ENV_NAMES = [*SECRETS, "LITELLM_URL", "REDIS_URL", "S3_ENDPOINT", "WAREHOUSE_BUCKET"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from the developer's shell environment."""
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()


def _write_env(path: Path, values: dict[str, str]) -> Path:
    env = path / ".env"
    env.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return env


def test_settings_masks_secrets(tmp_path: Path) -> None:
    s = Settings(_env_file=_write_env(tmp_path, SECRETS))
    rendered = repr(s) + str(s)
    for value in SECRETS.values():
        assert value not in rendered
    assert "postgres_dsn" not in rendered


def test_settings_reads_env_file(tmp_path: Path) -> None:
    s = Settings(_env_file=_write_env(tmp_path, SECRETS))
    assert s.groq_api_key is not None
    assert s.groq_api_key.get_secret_value() == SECRETS["GROQ_API_KEY"]
    assert s.litellm_master_key.get_secret_value() == SECRETS["LITELLM_MASTER_KEY"]
    assert s.rustfs_access_key.get_secret_value() == SECRETS["RUSTFS_ACCESS_KEY"]
    assert s.rustfs_secret_key.get_secret_value() == SECRETS["RUSTFS_SECRET_KEY"]
    assert ":55432/" in s.postgres_dsn
    assert (
        s.postgres_dsn
        == f"postgresql://sluice:{SECRETS['POSTGRES_PASSWORD']}@localhost:55432/sluice"
    )
    assert s.litellm_url == "http://localhost:54000"
    assert s.redis_url == "redis://localhost:56379/0"
    assert s.s3_endpoint == "http://localhost:59000"
    assert s.warehouse_bucket == "sluice-warehouse"


def test_settings_missing_llm_keys_ok(tmp_path: Path) -> None:
    required = {k: v for k, v in SECRETS.items() if k not in {"GROQ_API_KEY", "GEMINI_API_KEY"}}
    s = Settings(_env_file=_write_env(tmp_path, required))
    assert s.groq_api_key is None
    assert s.gemini_api_key is None


def test_settings_dsn_url_encodes_password(tmp_path: Path) -> None:
    values = {**SECRETS, "POSTGRES_PASSWORD": "p@ss/w:rd"}
    s = Settings(_env_file=_write_env(tmp_path, values))
    assert s.postgres_dsn == "postgresql://sluice:p%40ss%2Fw%3Ard@localhost:55432/sluice"


def test_settings_env_vars_override_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REDIS_URL", "redis://example:6379/1")
    s = Settings(_env_file=_write_env(tmp_path, SECRETS))
    assert s.redis_url == "redis://example:6379/1"


def test_get_settings_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in SECRETS.items():
        monkeypatch.setenv(k, v)
    assert get_settings() is get_settings()


REQUIRED_SECRETS = [
    "LITELLM_MASTER_KEY",
    "POSTGRES_PASSWORD",
    "RUSTFS_ACCESS_KEY",
    "RUSTFS_SECRET_KEY",
]


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
@pytest.mark.parametrize("name", REQUIRED_SECRETS)
def test_settings_rejects_blank_required_secret(tmp_path: Path, name: str, blank: str) -> None:
    env = _write_env(tmp_path, {**SECRETS, name: blank})
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=env)
    message = str(exc.value)
    assert name in message
    assert "input_value" not in message
    for value in SECRETS.values():
        assert value not in message


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
def test_settings_blank_optional_llm_keys_are_none(tmp_path: Path, blank: str) -> None:
    env = _write_env(tmp_path, {**SECRETS, "GROQ_API_KEY": blank, "GEMINI_API_KEY": blank})
    s = Settings(_env_file=env)
    assert s.groq_api_key is None
    assert s.gemini_api_key is None
