from __future__ import annotations

import asyncio

from fastapi import APIRouter

from app.core.config import get_settings
from app.services.sales_agent_service import HttpSalesAgentClient
from app.services.minio_storage_service import MinioStorageService

router = APIRouter(tags=["health"])


@router.get("/api/health/")
def health() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "service": settings.app.name,
        "env": settings.app.env,
    }


@router.get("/health", include_in_schema=False)
def gateway_health() -> dict[str, str]:
    return {"status": "UP", "service": "tvs-ai-gateway"}


@router.get("/health/agent", include_in_schema=False)
async def agent_health() -> dict[str, str]:
    agent = await HttpSalesAgentClient(get_settings().sales_agent).health()
    return {"status": "UP" if agent == "UP" else "DEGRADED", "agent": "UP" if agent == "UP" else "DOWN"}


@router.get("/api/health/ai-gateway", summary="AI Gateway 구성요소 상태")
async def ai_gateway_health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "components": {
            "application": "UP",
            "salesAgent": await HttpSalesAgentClient(settings.sales_agent).health(),
            "minio": await asyncio.to_thread(MinioStorageService(settings.minio).health),
        },
    }
