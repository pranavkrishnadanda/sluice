"""Runtime settings for the services stack, loaded from the environment and `backend/.env`.

Secrets are `SecretStr`, so `repr`/`str` show `'**********'`. `postgres_dsn` is a plain
property (not a field or computed field) so it never appears in `repr`, `str` or
`model_dump()`; it embeds the password and must not be logged.
"""

from functools import cache
from pathlib import Path
from urllib.parse import quote

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/src/sluice/settings.py -> backend/.env, independent of the working directory.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

POSTGRES_USER = "sluice"
POSTGRES_DB = "sluice"
POSTGRES_HOST = "localhost"
POSTGRES_PORT = 55432


class Settings(BaseSettings):
    """Service endpoints and credentials. Environment variables override the env file."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    litellm_master_key: SecretStr
    litellm_url: str = "http://localhost:54000"
    postgres_password: SecretStr
    redis_url: str = "redis://localhost:56379/0"
    s3_endpoint: str = "http://localhost:59000"
    rustfs_access_key: SecretStr
    rustfs_secret_key: SecretStr
    warehouse_bucket: str = "sluice-warehouse"

    @property
    def postgres_dsn(self) -> str:
        """libpq URL for the Compose Postgres. Contains the password: never print or log it."""
        password = quote(self.postgres_password.get_secret_value(), safe="")
        return (
            f"postgresql://{POSTGRES_USER}:{password}@{POSTGRES_HOST}:{POSTGRES_PORT}/{POSTGRES_DB}"
        )


@cache
def get_settings() -> Settings:
    """Process-wide settings, read once. Call `get_settings.cache_clear()` in tests."""
    return Settings()  # pyright: ignore[reportCallIssue]  # required fields come from env
