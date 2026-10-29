"""The contract error type and body: ``{"detail": str, "code"?: str}`` (``docs/api/contract.md``).

Kept apart from :mod:`sluice.api.app` so route modules can raise :class:`ApiError` without
importing the app factory (which imports them).
"""

from fastapi.responses import JSONResponse

from sluice.api.schemas import ErrorBody


class ApiError(Exception):
    """An expected failure a route reports to the client as ``{detail, code}``.

    Args:
        status: HTTP status, 400-599.
        detail: User-facing message. Never put secrets, paths or exception text here.
        code: Optional machine-readable code (``too_large``, ``not_ready``, ...).
    """

    def __init__(self, status: int, detail: str, code: str | None = None) -> None:
        if not 400 <= status <= 599:
            raise ValueError(f"ApiError status must be 4xx or 5xx, got {status}")
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code


def error_response(
    status: int, detail: str, code: str | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    """The contract error body; ``code`` is omitted when ``None``."""
    body = ErrorBody(detail=detail, code=code).model_dump(exclude_none=True)
    return JSONResponse(body, status_code=status, headers=headers)
