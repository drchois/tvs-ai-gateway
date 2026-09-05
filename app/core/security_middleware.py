from __future__ import annotations

from fastapi import HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import get_settings
from app.core.api_key_auth import configured_api_keys


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        settings = get_settings()
        security = settings.security

        # CORS preflight는 인증 없이 통과시켜야 브라우저에서 실제 요청이 가능합니다.
        if request.method == "OPTIONS":
            return await call_next(request)

        if not security.enabled:
            return await call_next(request)

        path = request.url.path

        if any(path.startswith(exempt) for exempt in security.exempt_paths):
            return await call_next(request)

        if not any(path.startswith(prefix) for prefix in security.protected_prefixes):
            return await call_next(request)

        allowed_keys = configured_api_keys(security.api_keys)
        if not allowed_keys:
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": "Security enabled but no API keys configured"},
            )

        auth_header = request.headers.get("Authorization", "")
        custom_header = request.headers.get(security.header_name, "")
        query_token = str(request.query_params.get("api_key") or "").strip()

        bearer_token = ""
        if auth_header.lower().startswith("bearer "):
            bearer_token = auth_header[7:].strip()

        custom_token = custom_header.strip()
        # Swagger API key 입력란에 실수로 "Bearer <key>"를 넣어도 허용합니다.
        if custom_token.lower().startswith("bearer "):
            custom_token = custom_token[7:].strip()

        provided_tokens = [token for token in (bearer_token, custom_token, query_token) if token]

        if not provided_tokens or not any(token in allowed_keys for token in provided_tokens):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid or missing API key"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        return await call_next(request)
