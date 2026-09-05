from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Security
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from app.core.api_key_auth import require_api_key_for_docs
from app.core.config import get_settings
from app.core.request_context import RequestContext, request_context_headers
from app.schemas.assistant import (
    AssistantAction,
    AssistantMessageRequest,
    AssistantMessageResponse,
    BusinessIntent,
)
from app.services.assistant_intent_router import classify_business_intent
from app.services.agent_response_adapter import (
    UnsupportedAgentSchemaVersion,
    normalize_agent_response,
)
from app.services.minio_storage_service import (
    MinioDownloadMetadata,
    MinioStorageService,
    ObjectStorageError,
    find_minio_download,
    get_minio_storage_service,
    register_minio_download,
)
from app.services.request_projection_service import RequestProjectionService, get_request_projection_service
from app.services.sales_agent_service import (
    SalesAgentClient,
    SalesAgentError,
    get_sales_agent_client,
    register_agent_artifact,
)

LOG = logging.getLogger(__name__)
router = APIRouter(prefix="/api/assistant", tags=["assistant"], dependencies=[Security(require_api_key_for_docs)])
def _normalize_sales_result(result: dict[str, object], *, tenant_id: str) -> dict[str, object]:
    try:
        normalized = normalize_agent_response(result)
    except (UnsupportedAgentSchemaVersion, ValueError) as exc:
        raise SalesAgentError(
            "판매 데이터 처리 서버 응답 계약이 올바르지 않습니다.",
            code="AGENT_CONTRACT_ERROR",
        ) from exc
    result_payload = result.get("result")
    download = result_payload.get("download") if isinstance(result_payload, dict) else None
    if isinstance(download, dict) and download.get("available") is True:
        required = ("downloadId", "bucketName", "objectKey")
        if all(str(download.get(key) or "").strip() for key in required) and normalized.request_id:
            expires_at = None
            if download.get("expiresAt"):
                try:
                    expires_at = datetime.fromisoformat(str(download["expiresAt"]))
                except ValueError:
                    expires_at = None
            try:
                settings = get_settings()
                register_minio_download(
                    MinioDownloadMetadata(
                        request_id=normalized.request_id,
                        tenant_id=tenant_id,
                        download_id=str(download["downloadId"]),
                        bucket_name=str(download["bucketName"]),
                        object_key=str(download["objectKey"]),
                        file_name=str(download.get("fileName") or "sales-result.xlsx"),
                        content_type=str(download.get("contentType") or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                        expires_at=expires_at,
                    ),
                    configured_bucket=settings.minio.bucket_exports,
                )
                LOG.info(
                    "artifact.registered",
                    extra={
                        "event": "artifact.registered",
                        "agent_request_id": normalized.request_id,
                        "artifact_id": str(download["downloadId"]),
                    },
                )
            except ObjectStorageError:
                LOG.warning("assistant.minio_metadata.rejected", extra={"event": "assistant.minio_metadata.rejected", "request_id": normalized.request_id})
    return normalized.model_dump(by_alias=True, exclude_none=True)


def _safe_issues(result: dict[str, object]) -> list[dict[str, object]]:
    safe: list[dict[str, object]] = []
    for item in result.get("issues") or []:
        if not isinstance(item, dict):
            continue
        safe.append({key: item[key] for key in ("code", "message", "severity", "field") if key in item})
    return safe


def _sales_response(
    result: dict[str, object],
) -> tuple[str, str, str | None, dict[str, object], list[dict[str, object]], list[AssistantAction]]:
    status = str(result.get("status") or "ERROR").upper()
    processing_statuses = {
        "RECEIVED", "INTERPRETING", "PLANNING", "EXECUTING",
        "RESULT_PROCESSING", "FILE_GENERATING", "FILE_UPLOADING",
    }
    public_status = "PROCESSING" if status in processing_statuses else status
    message = str(result.get("message") or "판매 데이터 요청 처리 결과입니다.")
    agent_request_id = str(result.get("requestId") or "").strip() or None
    questions = [item for item in (result.get("questions") or []) if isinstance(item, dict)]
    issues = _safe_issues(result)
    issue_codes = {str(item.get("code") or "").upper() for item in issues}
    data: dict[str, object] = {
        "responseSchemaVersion": str(result.get("responseSchemaVersion") or "1.0"),
        "legacy": bool(result.get("legacy", False)),
    }
    for key in ("reasonCode", "interpretation", "rowEstimate", "suggestedRefinements", "refinement"):
        if key in result and result[key] is not None:
            data[key] = result[key]
    agent_result = result.get("result")
    if isinstance(agent_result, dict):
        public_result = dict(agent_result)
        rows = agent_result.get("rows")
        if isinstance(rows, list):
            preview_limit = get_settings().request_projection.preview_row_limit
            preview_rows = rows[:preview_limit]
            public_result["rows"] = preview_rows
            row_count = agent_result.get("rowCount")
            public_result["truncated"] = bool(
                agent_result.get("truncated", False)
                or len(rows) > len(preview_rows)
                or (isinstance(row_count, int) and row_count > len(preview_rows))
            )
        data["result"] = public_result
        for key in ("rowCount", "summary", "validation", "deliverable", "validationStatus"):
            if key in agent_result:
                data[key] = agent_result[key]
    artifact = result.get("artifact")
    safe_artifact: dict[str, object] = {}
    if isinstance(artifact, dict):
        for key in (
            "artifactId", "artifactType", "fileName", "status", "fileSize",
            "rowCount", "expiresAt", "mediaType", "encrypted",
        ):
            if key in artifact:
                safe_artifact[key] = artifact[key]
        content_type = artifact.get("contentType") or artifact.get("mediaType")
        if content_type:
            safe_artifact["contentType"] = content_type
        if "fileSize" not in safe_artifact and artifact.get("size") is not None:
            safe_artifact["fileSize"] = artifact["size"]
        if safe_artifact.get("artifactId") and status == "COMPLETED":
            safe_artifact.setdefault("status", "READY")
    if safe_artifact:
        data["artifact"] = safe_artifact
        if isinstance(data.get("result"), dict):
            data["result"] = {**data["result"], "artifact": safe_artifact}
    if agent_request_id:
        data["agentRequestId"] = agent_request_id

    result_too_large = issue_codes.intersection({"TOO_MANY_RESULTS", "RESULT_TOO_LARGE"})
    if result_too_large:
        refinement = result.get("refinement") if isinstance(result.get("refinement"), dict) else {}
        refinement_payload: dict[str, object] = {"reason": sorted(result_too_large)[0]}
        for key in ("limit", "resultCount", "countExact", "suggestions"):
            if key in refinement:
                refinement_payload[key] = refinement[key]
        refinement_message = (
            message
            if result.get("message")
            else "요청 결과가 현재 조회 제한을 초과합니다. 기간을 줄이거나 특정 상품을 지정해 주세요."
        )
        return (
            "BLOCKED",
            refinement_message,
            agent_request_id,
            {**data, "refinement": refinement, "errorCategory": "BUSINESS_REFINEMENT"},
            issues,
            [AssistantAction(type="refine_request", payload=refinement_payload)],
        )

    if questions:
        return (
            "REQUIRES_CLARIFICATION",
            message,
            agent_request_id,
            data,
            issues,
            [AssistantAction(type="clarification", payload={"requestId": agent_request_id, "questions": questions})],
        )
    if "QUERY_BLOCKED" in issue_codes:
        blocked_message = message if result.get("message") else "요청을 실행 가능한 조회조건으로 확정하지 못했습니다."
        return (
            "BLOCKED",
            blocked_message,
            agent_request_id,
            {**data, "errorCategory": "BUSINESS_BLOCKED"},
            issues,
            [AssistantAction(type="blocked_message", payload={"requestId": agent_request_id, "issues": issues, "businessCondition": "QUERY_BLOCKED"})],
        )
    action_by_status = {
        "PROCESSING": "request_status",
        "REQUIRES_CLARIFICATION": "clarification",
        "READY_TO_EXECUTE": "confirm_execution",
        "COMPLETED": "result_preview",
        "BLOCKED": "blocked_message",
        "FAILED": "safe_error",
        "ERROR": "safe_error",
    }
    if public_status not in action_by_status:
        return (
            "ERROR",
            "판매 데이터 처리 서버가 지원하지 않는 상태를 반환했습니다.",
            agent_request_id,
            {"errorCode": "SALES_AGENT_UNSUPPORTED_STATUS"},
            issues,
            [AssistantAction(type="safe_error", payload={"errorCode": "SALES_AGENT_UNSUPPORTED_STATUS"})],
        )
    payload: dict[str, object] = {"requestId": agent_request_id}
    if status == "REQUIRES_CLARIFICATION":
        payload["questions"] = questions
    if status == "READY_TO_EXECUTE":
        payload["label"] = "조회 실행"
    actions = [AssistantAction(type=action_by_status[public_status], label="조회 실행" if status == "READY_TO_EXECUTE" else None, payload=payload)]
    logical_download = agent_result.get("download") if isinstance(agent_result, dict) else None
    download_url = result.get("downloadUrl") or (artifact.get("downloadUrl") if isinstance(artifact, dict) else None)
    artifact_id = str(safe_artifact.get("artifactId") or "").strip()
    if (
        status == "COMPLETED"
        and artifact_id
        and not (
            isinstance(logical_download, dict)
            and logical_download.get("downloadId")
        )
    ):
        if isinstance(download_url, str) and download_url.strip():
            register_agent_artifact(artifact_id, download_url)
        actions.append(AssistantAction(
            type="download_file",
            label="Excel 다운로드",
            payload={
                **safe_artifact,
                "url": f"/api/artifacts/{quote(artifact_id, safe='')}/download",
                "requestId": agent_request_id,
            },
        ))
    if (
        status == "COMPLETED"
        and agent_request_id
        and isinstance(logical_download, dict)
        and logical_download.get("available") is True
        and logical_download.get("downloadId")
    ):
        public_download_artifact = {
            "artifactId": logical_download["downloadId"],
            "fileName": logical_download.get("fileName") or logical_download.get("title"),
            "status": logical_download.get("status") or "READY",
            "contentType": logical_download.get("contentType"),
            "fileSize": logical_download.get("fileSize"),
            "rowCount": agent_result.get("rowCount"),
            "expiresAt": logical_download.get("expiresAt"),
        }
        if isinstance(data.get("result"), dict):
            data["result"] = {
                **data["result"],
                "artifact": {
                    key: value for key, value in public_download_artifact.items() if value is not None
                },
            }
        actions.append(AssistantAction(
            type="download_file",
            label=str(logical_download.get("title") or "Excel 다운로드"),
            payload={
                "downloadId": logical_download["downloadId"],
                "format": logical_download.get("format") or "XLSX",
                "status": logical_download.get("status") or "READY",
                "expiresAt": logical_download.get("expiresAt"),
                "url": f"/api/assistant/requests/{quote(agent_request_id, safe='')}/download",
                "requestId": agent_request_id,
            },
        ))
    if status in {"FAILED", "ERROR"}:
        data["errorCategory"] = "BUSINESS_FAILED"
    return public_status, message, agent_request_id, data, issues, actions


@router.post("/message", response_model=AssistantMessageResponse, response_model_by_alias=True)
async def assistant_message(
    http_request: Request,
    body: AssistantMessageRequest = Body(...),
    request_context: RequestContext = Depends(request_context_headers),
    sales_agent_client: SalesAgentClient = Depends(get_sales_agent_client),
    projection: RequestProjectionService = Depends(get_request_projection_service),
) -> AssistantMessageResponse:
    started = time.perf_counter()
    request_id = str(getattr(http_request.state, "request_id", "") or uuid4().hex)
    LOG.info("request.received", extra={"event": "request.received"})
    session_id = (body.session_id or f"assistant-{uuid4().hex[:12]}").strip()
    intent = classify_business_intent(body.message, body.context)
    if body.action and str(body.action.get("type") or "") == "confirm_execution":
        intent = BusinessIntent.SALES_DATA_REQUEST
    status, message, data, issues, actions = "COMPLETED", "", {}, [], []
    response_request_id = request_id
    if intent is not BusinessIntent.SALES_DATA_REQUEST:
        return AssistantMessageResponse(
            requestId=request_id,
            sessionId=session_id,
            intent=intent,
            status="NOT_SUPPORTED",
            message="현재 Gateway는 판매 데이터 조회 요청만 지원합니다.",
            data={},
            issues=[],
            actions=[],
        )
    identity = request_context.resolve(
        tenant_id=body.tenant_id or body.context.tenant_id,
        user_id=body.context.user_id,
    )
    agent_body = body.model_copy(update={"tenant_id": identity.tenant_id})

    try:
        if intent is BusinessIntent.SALES_DATA_REQUEST:
            if body.action and str(body.action.get("type") or "") == "confirm_execution":
                agent_request_id = str(body.action.get("requestId") or "").strip()
                result = await sales_agent_client.execute_request(agent_request_id=agent_request_id, request_id=request_id)
            else:
                result = await sales_agent_client.submit_request(
                    agent_body,
                    request_id=request_id,
                    session_id=session_id,
                )
            result = _normalize_sales_result(result, tenant_id=identity.tenant_id or "default")
            if not body.action and projection.config.enabled:
                created_request_id = str(result.get("requestId") or "").strip()
                projection.project(
                    request_id=created_request_id,
                    agent_request_id=created_request_id,
                    tenant_id=identity.tenant_id or "default",
                    user_id=identity.user_id or "anonymous",
                    request_text=body.message,
                    response=result,
                    intent=intent.value,
                )
                LOG.info(
                    "projection.saved",
                    extra={"event": "projection.saved", "request_id": created_request_id},
                )
            if (
                not body.action
                and body.execution.allow_execute
                and str(result.get("status") or "").upper() == "READY_TO_EXECUTE"
            ):
                agent_request_id = str(result.get("requestId") or "").strip()
                if not agent_request_id:
                    raise SalesAgentError(
                        "Agent 응답에 requestId가 없습니다.",
                        code="INVALID_AGENT_RESPONSE",
                    )
                result = await sales_agent_client.execute_request(
                    agent_request_id=agent_request_id,
                    request_id=request_id,
                )
                result = _normalize_sales_result(result, tenant_id=identity.tenant_id or "default")
            status, message, agent_request_id, data, issues, actions = _sales_response(result)
            response_request_id = agent_request_id or request_id
            if projection.config.enabled:
                projection.project(
                    request_id=response_request_id,
                    agent_request_id=agent_request_id,
                    tenant_id=identity.tenant_id or "default",
                    user_id=identity.user_id or "anonymous",
                    request_text=body.message,
                    response=result,
                    intent=intent.value,
                )
                LOG.info(
                    "projection.saved",
                    extra={"event": "projection.saved", "request_id": response_request_id},
                )
        elif intent in {BusinessIntent.RAG_QA, BusinessIntent.POLICY_INQUIRY}:
            status, message = "NOT_SUPPORTED", "현재 Gateway는 판매 데이터 조회 요청만 지원합니다."
        elif intent is BusinessIntent.CHAT:
            status, message = "NOT_SUPPORTED", "현재 Gateway는 판매 데이터 조회 요청만 지원합니다."
        elif intent is BusinessIntent.ARTIFACT_ACTION:
            status, message = "NOT_SUPPORTED", "현재 Gateway는 판매 데이터 조회 요청만 지원합니다."
        elif intent is BusinessIntent.SALES_INSIGHT:
            status, message = "NOT_IMPLEMENTED", "판매 인사이트 통합 경로는 준비 중입니다."
        elif intent is BusinessIntent.CRM_INTELLIGENCE:
            status, message = "NOT_IMPLEMENTED", "CRM Intelligence 기능은 아직 활성화되지 않았습니다."
        else:
            status, message = "UNKNOWN", "요청 의도를 확인하지 못했습니다. 요청을 조금 더 구체적으로 입력해 주세요."
    except SalesAgentError as exc:
        status, message = "ERROR", str(exc)
        protocol_codes = {"UPSTREAM_PROTOCOL_ERROR", "INVALID_AGENT_RESPONSE"}
        data = {"errorCode": exc.code, "errorCategory": "UPSTREAM_PROTOCOL_ERROR" if exc.code in protocol_codes else "INTEGRATION_ERROR"}
        actions = [AssistantAction(type="safe_error", payload={"errorCode": exc.code})]
    except Exception as exc:  # noqa: BLE001
        LOG.exception("assistant.route.failed", extra={"event": "assistant.route.failed", "intent": intent.value})
        status, message, data = "ERROR", "AI 요청을 처리하지 못했습니다.", {"errorCode": "AI_GATEWAY_ERROR"}
        actions = [AssistantAction(type="safe_error", payload=data)]

    LOG.info(
        "assistant.route.completed",
        extra={
            "event": "assistant.route.completed",
            "session_id": session_id,
            "intent": intent.value,
            "route": intent.value,
            "upstream_service": "tv-sales-agent" if intent is BusinessIntent.SALES_DATA_REQUEST else None,
            "status": status,
            "issue_codes": [str(item.get("code") or "") for item in issues],
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    LOG.info(
        "request.completed",
        extra={
            "event": "request.completed",
            "agent_request_id": response_request_id if response_request_id != request_id else None,
            "status": status,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        },
    )
    return AssistantMessageResponse(
        requestId=response_request_id,
        sessionId=session_id,
        intent=intent,
        status=status,
        message=message,
        data=data,
        issues=issues,
        actions=actions,
    )


@router.post("/requests/{agent_request_id}/execute", response_model=AssistantMessageResponse, response_model_by_alias=True)
async def execute_sales_request(
    agent_request_id: str,
    http_request: Request,
    sales_agent_client: SalesAgentClient = Depends(get_sales_agent_client),
) -> AssistantMessageResponse:
    request_id = str(getattr(http_request.state, "request_id", "") or uuid4().hex)
    result = await sales_agent_client.execute_request(agent_request_id=agent_request_id, request_id=request_id)
    result = _normalize_sales_result(result, tenant_id=get_settings().sales_agent.default_tenant)
    status, message, response_id, data, issues, actions = _sales_response(result)
    return AssistantMessageResponse(
        requestId=response_id or request_id,
        sessionId=f"assistant-{uuid4().hex[:12]}",
        intent=BusinessIntent.SALES_DATA_REQUEST,
        status=status,
        message=message,
        data=data,
        issues=issues,
        actions=actions,
    )


@router.get("/artifacts/{artifact_id}")
async def download_sales_artifact(
    artifact_id: str,
    http_request: Request,
    sales_agent_client: SalesAgentClient = Depends(get_sales_agent_client),
) -> StreamingResponse:
    request_id = str(getattr(http_request.state, "request_id", "") or uuid4().hex)
    try:
        artifact_stream = await sales_agent_client.download_artifact(
            artifact_id=artifact_id,
            request_id=request_id,
        )
    except SalesAgentError as exc:
        raise HTTPException(status_code=exc.http_status, detail={"code": exc.code, "message": str(exc)}) from exc

    upstream = artifact_stream.response
    headers = {
        key: upstream.headers[key]
        for key in ("content-length", "content-disposition")
        if key in upstream.headers
    }
    return StreamingResponse(
        artifact_stream.chunks(),
        status_code=200,
        media_type=upstream.headers.get(
            "content-type",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
        headers=headers,
        background=BackgroundTask(artifact_stream.close),
    )


def _minio_chunks(response: object):
    yield from response.stream(amt=64 * 1024)


def _close_minio_response(response: object) -> None:
    response.close()
    response.release_conn()


@router.get("/requests/{agent_request_id}/download")
async def download_minio_sales_result(
    agent_request_id: str,
    minio_service: MinioStorageService = Depends(get_minio_storage_service),
) -> StreamingResponse:
    metadata = find_minio_download(request_id=agent_request_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail={"code": "OBJECT_NOT_FOUND", "message": "다운로드 정보를 찾을 수 없습니다."})
    if metadata.expires_at is not None:
        expires_at = metadata.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=410, detail={"code": "ARTIFACT_EXPIRED", "message": "다운로드 파일이 만료되었습니다."})
    try:
        response = await asyncio.to_thread(
            minio_service.get_object,
            object_key=metadata.object_key,
            bucket_name=metadata.bucket_name,
        )
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail={"code": exc.code, "message": str(exc)}) from exc
    encoded_name = quote(metadata.file_name, safe="")
    return StreamingResponse(
        _minio_chunks(response),
        media_type=metadata.content_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}"},
        background=BackgroundTask(_close_minio_response, response),
    )
