from __future__ import annotations

import logging
import os
import time
from uuid import uuid4

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request

from app.api.assistant import router as assistant_router
from app.api.health import router as health_router
from app.api.requests import router as requests_router
from app.core.config import get_settings
from app.core.env_loader import load_environment
from app.core.logging import reset_request_id, set_request_id, setup_logging
from app.core.security_middleware import ApiKeyMiddleware

LOG = logging.getLogger(__name__)

load_environment()
settings = get_settings()
os.environ["LOG_LEVEL"] = settings.logging.level
os.environ["LOG_JSON"] = "true" if settings.logging.json_enabled else "false"
os.environ["LOG_PII_MASK_KEYS"] = ",".join(settings.logging.pii_mask_keys)
os.environ["LOG_APP_ENV"] = settings.app.env
setup_logging()

app = FastAPI(
    title=settings.app.name,
    version="1.0.0",
    docs_url=settings.app.docs_url,
    openapi_url=settings.app.openapi_url,
)


@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or uuid4().hex
    request.state.request_id = request_id
    token = set_request_id(request_id)
    started_at = time.perf_counter()
    LOG.info(
        "http.request.started",
        extra={"event": "http.request.started", "method": request.method, "path": request.url.path},
    )
    try:
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        LOG.info(
            "http.request.completed",
            extra={
                "event": "http.request.completed",
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 2),
            },
        )
        return response
    except Exception:
        LOG.exception(
            "http.request.failed",
            extra={
                "event": "http.request.failed",
                "method": request.method,
                "path": request.url.path,
                "elapsed_ms": round((time.perf_counter() - started_at) * 1000, 2),
            },
        )
        raise
    finally:
        reset_request_id(token)


app.add_middleware(ApiKeyMiddleware)
if settings.cors.enabled:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.allow_origins,
        allow_origin_regex=settings.cors.allow_origin_regex,
        allow_credentials=settings.cors.allow_credentials,
        allow_methods=settings.cors.allow_methods,
        allow_headers=settings.cors.allow_headers,
        expose_headers=settings.cors.expose_headers,
        max_age=settings.cors.max_age,
    )

app.include_router(health_router)
app.include_router(assistant_router)
app.include_router(requests_router)
