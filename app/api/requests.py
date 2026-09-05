from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Security
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from app.api.assistant import _normalize_sales_result
from app.core.api_key_auth import require_api_key_for_docs
from app.core.request_context import RequestContext, required_request_context
from app.services.minio_storage_service import (
    MinioStorageService,
    ObjectStorageError,
    find_minio_download,
    get_minio_storage_service,
)
from app.services.request_projection_service import RequestProjectionService, get_request_projection_service
from app.services.sales_agent_service import SalesAgentClient, SalesAgentError, get_sales_agent_client


router = APIRouter(tags=["requests"], dependencies=[Security(require_api_key_for_docs)])
LOG = logging.getLogger(__name__)


@router.get("/api/requests")
def list_requests(
    status: str | None = None,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    keyword: str | None = None,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    principal: RequestContext = Depends(required_request_context),
    projection: RequestProjectionService = Depends(get_request_projection_service),
):
    return projection.list_owned(
        tenant_id=principal.tenant_id,
        user_id=principal.user_id,
        roles=principal.roles,
        status=status.upper() if status else None,
        date_from=date_from,
        date_to=date_to,
        keyword=keyword,
        page=page,
        size=size,
    )


@router.get("/api/requests/{request_id}")
async def request_detail(
    request_id: str,
    http_request: Request,
    principal: RequestContext = Depends(required_request_context),
    projection: RequestProjectionService = Depends(get_request_projection_service),
    sales_agent_client: SalesAgentClient = Depends(get_sales_agent_client),
):
    detail = projection.get_owned(request_id, principal.tenant_id, principal.user_id, principal.roles)
    if detail is None:
        raise HTTPException(status_code=404, detail={"code": "REQUEST_NOT_FOUND"})
    if detail["status"] not in {"COMPLETED", "FAILED", "BLOCKED"} and detail.get("agentRequestId"):
        trace_id = str(getattr(http_request.state, "request_id", "") or uuid4().hex)
        try:
            raw = await sales_agent_client.get_request(agent_request_id=detail["agentRequestId"], request_id=trace_id)
            normalized = _normalize_sales_result(raw, tenant_id=principal.tenant_id)
            projection.project(
                request_id=request_id,
                agent_request_id=detail["agentRequestId"],
                tenant_id=principal.tenant_id,
                user_id=detail.get("userId") or principal.user_id,
                request_text=detail["requestText"],
                response=normalized,
            )
            detail = projection.get_owned(request_id, principal.tenant_id, principal.user_id, principal.roles)
        except SalesAgentError as exc:
            raise HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": str(exc)}) from exc
    return detail


def _chunks(response: object):
    yield from response.stream(amt=64 * 1024)


def _close(response: object) -> None:
    response.close()
    response.release_conn()


@router.get("/api/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: str,
    http_request: Request,
    principal: RequestContext = Depends(required_request_context),
    projection: RequestProjectionService = Depends(get_request_projection_service),
    minio_service: MinioStorageService = Depends(get_minio_storage_service),
    sales_agent_client: SalesAgentClient = Depends(get_sales_agent_client),
):
    LOG.info(
        "download.started",
        extra={"event": "download.started", "artifact_id": artifact_id},
    )
    owner = projection.artifact_owner(artifact_id)
    if owner is None or owner[1] != principal.tenant_id:
        raise HTTPException(status_code=404, detail={"code": "ARTIFACT_NOT_FOUND"})
    if owner[2] != principal.user_id and not set(principal.roles).intersection(projection.config.admin_roles):
        raise HTTPException(status_code=403, detail={"code": "ARTIFACT_FORBIDDEN"})
    metadata = find_minio_download(download_id=artifact_id)
    if metadata is None:
        try:
            stream = await sales_agent_client.download_artifact(
                artifact_id=artifact_id,
                request_id=str(getattr(http_request.state, "request_id", "") or uuid4().hex),
            )
        except SalesAgentError as agent_exc:
            if agent_exc.code != "ARTIFACT_NOT_FOUND":
                projection.mark_artifact_failed(artifact_id)
                raise HTTPException(
                    status_code=agent_exc.http_status,
                    detail={"code": agent_exc.code, "message": str(agent_exc)},
                ) from agent_exc
            try:
                object_key = await asyncio.to_thread(
                    minio_service.find_artifact_object_key,
                    artifact_id,
                )
                LOG.info(
                    "download.storage_fallback",
                    extra={"event": "download.storage_fallback", "artifact_id": artifact_id},
                )
                response = await asyncio.to_thread(
                    minio_service.get_object,
                    object_key=object_key,
                )
            except ObjectStorageError as storage_exc:
                projection.mark_artifact_failed(artifact_id)
                status_code = 404 if storage_exc.code == "ARTIFACT_NOT_FOUND" else 502
                raise HTTPException(
                    status_code=status_code,
                    detail={"code": storage_exc.code, "message": str(storage_exc)},
                ) from storage_exc
            detail = projection.get_owned(
                owner[0],
                principal.tenant_id,
                principal.user_id,
                principal.roles,
            )
            artifact = next(
                (
                    item
                    for item in (detail or {}).get("artifacts", [])
                    if item.get("artifactId") == artifact_id
                ),
                {},
            )
            file_name = str(artifact.get("fileName") or "sales-result.xlsx")
            content_type = str(
                artifact.get("contentType")
                or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            LOG.info(
                "download.completed",
                extra={"event": "download.completed", "artifact_id": artifact_id},
            )
            return StreamingResponse(
                _chunks(response),
                media_type=content_type,
                headers={
                    "Content-Disposition": (
                        f"attachment; filename*=UTF-8''{quote(file_name, safe='')}"
                    )
                },
                background=BackgroundTask(_close, response),
            )
        LOG.info(
            "download.completed",
            extra={"event": "download.completed", "artifact_id": artifact_id},
        )
        return StreamingResponse(
            stream.chunks(),
            media_type=stream.response.headers.get("content-type", "application/octet-stream"),
            headers={key: stream.response.headers[key] for key in ("content-length", "content-disposition") if key in stream.response.headers},
            background=BackgroundTask(stream.close),
        )
    if metadata.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail={"code": "ARTIFACT_FORBIDDEN"})
    try:
        response = await asyncio.to_thread(
            minio_service.get_object,
            object_key=metadata.object_key,
            bucket_name=metadata.bucket_name,
        )
    except ObjectStorageError as exc:
        projection.mark_artifact_failed(artifact_id)
        raise HTTPException(status_code=502, detail={"code": exc.code, "message": str(exc)}) from exc
    LOG.info(
        "download.completed",
        extra={"event": "download.completed", "artifact_id": artifact_id},
    )
    return StreamingResponse(
        _chunks(response),
        media_type=metadata.content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(metadata.file_name, safe='')}"},
        background=BackgroundTask(_close, response),
    )
