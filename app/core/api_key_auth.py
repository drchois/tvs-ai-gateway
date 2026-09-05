from __future__ import annotations

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from app.core.config import get_settings

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    scheme_name="ApiKeyAuth",
    description="API key for protected endpoints. Use the FASTAPI_API_KEY environment value.",
)


def configured_api_keys(values: list[str]) -> list[str]:
    """Return usable credentials, excluding blank and unresolved env placeholders."""
    return [
        value.strip()
        for value in values
        if value and value.strip() and not (value.strip().startswith("${") and value.strip().endswith("}"))
    ]


def require_api_key_for_docs(
    request: Request,
    api_key: str | None = Security(api_key_header),
) -> str:
    settings = get_settings()
    security = settings.security

    if not security.enabled:
        return ""

    allowed_keys = configured_api_keys(security.api_keys)
    if not allowed_keys:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Security enabled but no API keys configured",
        )

    normalized_key = (api_key or "").strip()
    if not normalized_key and request is not None:
        normalized_key = str(request.query_params.get("api_key") or "").strip()
    if not normalized_key and request is not None:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            normalized_key = auth_header[7:].strip()

    if normalized_key.lower().startswith("bearer "):
        normalized_key = normalized_key[7:].strip()

    if not normalized_key or normalized_key not in allowed_keys:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )

    return normalized_key
