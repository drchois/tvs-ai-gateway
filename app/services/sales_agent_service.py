from __future__ import annotations

from dataclasses import dataclass
import logging
import time
from typing import Any, AsyncIterator, Protocol
from urllib.parse import quote, urljoin, urlparse

import httpx

from app.core.config import SalesAgentConfig, get_settings
from app.schemas.assistant import AssistantMessageRequest, SalesAgentExecution, SalesAgentRequest

LOG = logging.getLogger(__name__)


class SalesAgentError(RuntimeError):
    def __init__(self, message: str, *, code: str, http_status: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


_ARTIFACT_PATHS: dict[str, str] = {}


def register_agent_artifact(artifact_id: str, download_url: str) -> None:
    if artifact_id.strip() and download_url.strip():
        _ARTIFACT_PATHS[artifact_id.strip()] = download_url.strip()


@dataclass
class AgentArtifactStream:
    response: httpx.Response
    client: httpx.AsyncClient

    async def chunks(self) -> AsyncIterator[bytes]:
        async for chunk in self.response.aiter_bytes():
            yield chunk

    async def close(self) -> None:
        await self.response.aclose()
        await self.client.aclose()


def build_sales_agent_request(
    request: AssistantMessageRequest, *, default_tenant: str
) -> SalesAgentRequest:
    return SalesAgentRequest(
        tenantId=request.tenant_id or request.context.tenant_id or default_tenant,
        message=request.message,
        execution=SalesAgentExecution(
            allowExecute=False,
            allowPii=request.execution.allow_pii,
        ),
    )


class SalesAgentClient(Protocol):
    async def submit_request(
        self, request: AssistantMessageRequest, *, request_id: str, session_id: str
    ) -> dict[str, Any]: ...

    async def execute_request(self, *, agent_request_id: str, request_id: str) -> dict[str, Any]: ...

    async def get_request(self, *, agent_request_id: str, request_id: str) -> dict[str, Any]: ...

    async def health(self) -> str: ...

    async def download_artifact(self, *, artifact_id: str, request_id: str) -> AgentArtifactStream: ...


class HttpSalesAgentClient:
    def __init__(self, config: SalesAgentConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self.transport = transport

    def _headers(self, request_id: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Agent-Caller": self.config.caller,
            "X-Request-Id": request_id,
        }
        if self.config.api_key:
            headers[self.config.api_key_header] = self.config.api_key
        return headers

    def _url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            result = response.json()
        except ValueError as exc:
            raise SalesAgentError(
                "판매 데이터 처리 서버가 올바르지 않은 응답을 반환했습니다.",
                code="INVALID_AGENT_RESPONSE",
            ) from exc
        if not isinstance(result, dict):
            raise SalesAgentError(
                "판매 데이터 처리 서버가 올바르지 않은 응답을 반환했습니다.",
                code="INVALID_AGENT_RESPONSE",
            )
        return result

    async def submit_request(
        self, request: AssistantMessageRequest, *, request_id: str, session_id: str
    ) -> dict[str, Any]:
        if not self.config.enabled:
            raise SalesAgentError("판매 데이터 요청 기능이 비활성화되어 있습니다.", code="AGENT_DISABLED", http_status=503)
        agent_request = build_sales_agent_request(
            request,
            default_tenant=self.config.default_tenant,
        )
        payload = agent_request.model_dump(by_alias=True, exclude_none=True)
        started = time.perf_counter()
        tenant_id = request.tenant_id or request.context.tenant_id or self.config.default_tenant
        LOG.info(
            "agent.create.started",
            extra={"event": "agent.create.started", "tenant_id": tenant_id},
        )
        timeout = httpx.Timeout(
            float(self.config.request_timeout_sec),
            connect=float(self.config.connect_timeout_sec),
        )
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                response = await client.post(
                    self._url(self.config.request_path),
                    json=payload,
                    headers=self._headers(request_id),
                )
                response.raise_for_status()
                result = self._json_object(response)
                required = ("requestId", "status", "message")
                if any(not str(result.get(field) or "").strip() for field in required):
                    raise SalesAgentError("Agent 응답에 필수 필드가 없습니다.", code="INVALID_AGENT_RESPONSE")
                LOG.info("agent.create.completed", extra={
                    "event": "agent.create.completed",
                    "agent_request_id": result["requestId"],
                    "status": result["status"],
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                })
                return result
        except httpx.ConnectTimeout as exc:
            raise SalesAgentError("Agent 연결 시간이 초과되었습니다.", code="AGENT_CONNECT_TIMEOUT", http_status=504) from exc
        except httpx.TimeoutException as exc:
            raise SalesAgentError("Agent 요청 시간이 초과되었습니다.", code="AGENT_REQUEST_TIMEOUT", http_status=504) from exc
        except httpx.HTTPStatusError as exc:
            code = "AGENT_UNAUTHORIZED" if exc.response.status_code in {401, 403} else "AGENT_REQUEST_FAILED"
            raise SalesAgentError("Agent가 요청을 처리하지 못했습니다.", code=code) from exc
        except SalesAgentError:
            raise
        except httpx.RequestError as exc:
            raise SalesAgentError("Agent에 연결할 수 없습니다.", code="AGENT_CONNECTION_FAILED") from exc

    async def execute_request(self, *, agent_request_id: str, request_id: str) -> dict[str, Any]:
        if not self.config.enabled:
            raise SalesAgentError("판매 데이터 요청 기능이 비활성화되어 있습니다.", code="AGENT_DISABLED", http_status=503)
        safe_id = quote(agent_request_id.strip(), safe="")
        if not safe_id:
            raise SalesAgentError("실행할 Agent 요청 ID가 필요합니다.", code="INVALID_EXECUTE_REQUEST", http_status=400)
        path = self.config.execute_path.replace("{requestId}", safe_id)
        started = time.perf_counter()
        LOG.info(
            "agent.execute.started",
            extra={"event": "agent.execute.started", "agent_request_id": agent_request_id},
        )
        timeout = httpx.Timeout(
            float(self.config.execute_timeout_sec),
            connect=float(self.config.connect_timeout_sec),
        )
        try:
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                response = await client.post(self._url(path), json={}, headers=self._headers(request_id))
                response.raise_for_status()
                result = self._json_object(response)
                LOG.info("agent.execute.completed", extra={
                    "event": "agent.execute.completed",
                    "agent_request_id": agent_request_id,
                    "status": result.get("status"),
                    "elapsed_ms": round((time.perf_counter() - started) * 1000),
                })
                return result
        except httpx.ConnectTimeout as exc:
            raise SalesAgentError("Agent 연결 시간이 초과되었습니다.", code="AGENT_CONNECT_TIMEOUT", http_status=504) from exc
        except httpx.TimeoutException as exc:
            raise SalesAgentError("Agent 실행 시간이 초과되었습니다.", code="AGENT_EXECUTE_TIMEOUT", http_status=504) from exc
        except httpx.HTTPStatusError as exc:
            code = "AGENT_UNAUTHORIZED" if exc.response.status_code in {401, 403} else "AGENT_EXECUTION_FAILED"
            raise SalesAgentError("Agent가 실행 요청을 처리하지 못했습니다.", code=code) from exc
        except SalesAgentError:
            raise
        except httpx.RequestError as exc:
            raise SalesAgentError("Agent에 연결할 수 없습니다.", code="AGENT_CONNECTION_FAILED") from exc

    async def get_request(self, *, agent_request_id: str, request_id: str) -> dict[str, Any]:
        safe_id = quote(agent_request_id.strip(), safe="")
        if not safe_id:
            raise SalesAgentError("Agent request ID is required.", code="INVALID_REQUEST_ID", http_status=400)
        path = self.config.status_path.replace("{requestId}", safe_id)
        try:
            async with httpx.AsyncClient(timeout=float(self.config.request_timeout_sec), transport=self.transport) as client:
                response = await client.get(self._url(path), headers=self._headers(request_id))
                response.raise_for_status()
                return self._json_object(response)
        except httpx.TimeoutException as exc:
            raise SalesAgentError("Agent status request timed out.", code="AGENT_TIMEOUT", http_status=504) from exc
        except httpx.HTTPStatusError as exc:
            not_found = exc.response.status_code == 404
            raise SalesAgentError(
                "Agent request status could not be retrieved.",
                code="AGENT_REQUEST_NOT_FOUND" if not_found else "AGENT_CONNECTION_FAILED",
                http_status=404 if not_found else 502,
            ) from exc
        except SalesAgentError:
            raise
        except httpx.RequestError as exc:
            raise SalesAgentError("Agent connection failed.", code="AGENT_CONNECTION_FAILED") from exc

    async def health(self) -> str:
        if not self.config.enabled:
            return "DISABLED"
        try:
            async with httpx.AsyncClient(timeout=min(float(self.config.request_timeout_sec), 3.0), transport=self.transport) as client:
                response = await client.get(self._url(self.config.health_path), headers=self._headers("health"))
                return "UP" if response.is_success else "DOWN"
        except httpx.HTTPError:
            return "DOWN"

    async def download_artifact(self, *, artifact_id: str, request_id: str) -> AgentArtifactStream:
        safe_id = artifact_id.strip()
        download_url = _ARTIFACT_PATHS.get(safe_id)
        if not safe_id or not download_url:
            raise SalesAgentError("다운로드 파일을 찾을 수 없습니다.", code="ARTIFACT_NOT_FOUND", http_status=404)

        parsed = urlparse(download_url)
        base = urlparse(self.config.base_url)
        if parsed.netloc and (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            raise SalesAgentError("허용되지 않은 파일 경로입니다.", code="ARTIFACT_FORBIDDEN", http_status=403)
        target_url = download_url if parsed.netloc else urljoin(f"{self.config.base_url.rstrip('/')}/", download_url.lstrip("/"))
        client = httpx.AsyncClient(timeout=float(self.config.download_timeout_sec), transport=self.transport)
        try:
            upstream_request = client.build_request("GET", target_url, headers=self._headers(request_id))
            response = await client.send(upstream_request, stream=True)
            error_by_status = {
                404: ("ARTIFACT_NOT_FOUND", "다운로드 파일을 찾을 수 없습니다.", 404),
                410: ("ARTIFACT_EXPIRED", "다운로드 파일이 만료되었습니다.", 410),
                401: ("ARTIFACT_FORBIDDEN", "다운로드 권한이 없습니다.", 403),
                403: ("ARTIFACT_FORBIDDEN", "다운로드 권한이 없습니다.", 403),
            }
            if response.status_code in error_by_status:
                code, message, http_status = error_by_status[response.status_code]
                await response.aclose()
                await client.aclose()
                raise SalesAgentError(message, code=code, http_status=http_status)
            response.raise_for_status()
            return AgentArtifactStream(response=response, client=client)
        except SalesAgentError:
            raise
        except httpx.HTTPError as exc:
            await client.aclose()
            raise SalesAgentError("파일 다운로드 서버 오류가 발생했습니다.", code="ARTIFACT_UPSTREAM_ERROR") from exc

    # 기존 내부 호출 호환성. 신규 코드는 명시적인 메서드명을 사용한다.
    async def process(
        self, request: AssistantMessageRequest, *, request_id: str, session_id: str
    ) -> dict[str, Any]:
        return await self.submit_request(request, request_id=request_id, session_id=session_id)

    async def execute(self, *, agent_request_id: str, request_id: str) -> dict[str, Any]:
        return await self.execute_request(agent_request_id=agent_request_id, request_id=request_id)


def get_sales_agent_client() -> SalesAgentClient:
    return HttpSalesAgentClient(get_settings().sales_agent)
