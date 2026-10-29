"""API server knobs, read from the environment. No secrets live here (see ``sluice.settings``).

Every knob is ``SLUICE_API_<NAME>`` except the CORS allow-list, which is ``SLUICE_UI_ORIGINS``
(comma-separated). The env file is not read: the stack secrets stay in ``sluice.settings``.
"""

from typing import Annotated, Final

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_UI_ORIGINS: Final = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3100",
    "http://127.0.0.1:3100",
)
"""``next dev`` (3000) and the UI preview (3100), by host name and by loopback address."""

MAX_UPLOAD_BYTES: Final = 100 * 1024 * 1024
"""Upload limit from the contract: 100 MB (``413`` above it)."""


class ApiSettings(BaseSettings):
    """Process-wide API configuration. Frozen; build a new instance to change it in tests."""

    model_config = SettingsConfigDict(
        env_prefix="SLUICE_API_",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    ui_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_UI_ORIGINS),
        validation_alias=AliasChoices("SLUICE_UI_ORIGINS", "ui_origins"),
        description="Exact browser origins allowed by CORS (scheme://host[:port], no path).",
    )
    max_upload_bytes: int = Field(MAX_UPLOAD_BYTES, gt=0)
    concurrency: int = Field(1, ge=1, le=4, description="Runs executed at once (Groq limits).")
    keepalive_s: float = Field(15.0, gt=0, description="SSE keep-alive comment interval.")
    landing_bucket: str = Field("sluice-landing", min_length=3, max_length=63)

    @field_validator("ui_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """``"a, b,,"`` -> ``["a", "b"]``; lists pass through."""
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("ui_origins")
    @classmethod
    def _exact_origins(cls, origins: list[str]) -> list[str]:
        """Reject ``*`` (any site could read responses) and origins that can never match."""
        for origin in origins:
            scheme, sep, rest = origin.partition("://")
            if origin == "*" or not sep or scheme not in ("http", "https") or not rest:
                raise ValueError("SLUICE_UI_ORIGINS entries must be http(s)://host[:port]")
            if "/" in rest:
                raise ValueError("SLUICE_UI_ORIGINS entries must not have a path or trailing /")
        return origins
