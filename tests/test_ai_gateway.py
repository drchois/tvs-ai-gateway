from __future__ import annotations

import json
import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app.core.config import RequestProjectionConfig, SalesAgentConfig
from app.core.config import MinioConfig
from app.schemas.assistant import AssistantContext, AssistantMessageRequest, BusinessIntent
from app.schemas.sales_agent_response import AgentResponseV2
from app.services.agent_response_adapter import (
    UnsupportedAgentSchemaVersion,
    normalize_agent_response,
)
from app.services.assistant_intent_router import classify_business_intent
from app.services.sales_agent_service import (
    HttpSalesAgentClient,
    SalesAgentError,
    build_sales_agent_request,
)
from app.services.minio_storage_service import (
    MinioStorageService,
    build_export_object_key,
    find_minio_download,
)
from app.services.request_projection_service import RequestProjectionService


class IntentRouterTests(unittest.TestCase):
    def test_chat_routing(self) -> None:
        self.assertEqual(classify_business_intent("안녕하세요", AssistantContext()), BusinessIntent.CHAT)

    def test_rag_routing(self) -> None:
        self.assertEqual(classify_business_intent("매뉴얼 문서를 찾아줘", AssistantContext()), BusinessIntent.RAG_QA)

    def test_sales_data_routing(self) -> None:
        self.assertEqual(classify_business_intent("지난달 구매 고객을 알려줘", AssistantContext()), BusinessIntent.SALES_DATA_REQUEST)

    def test_korean_product_customer_request_routing(self) -> None:
        self.assertEqual(
            classify_business_intent(
                "2026년 6월 1일부터 7월 31일까지 B.A크림50ML를 구매한 고객을 조회해줘",
                AssistantContext(),
            ),
            BusinessIntent.SALES_DATA_REQUEST,
        )

    def test_sales_status_request_intent(self) -> None:
        self.assertEqual(
            classify_business_intent(
                "2026년 7월 B.A크림50ML 판매현황을 조회해줘",
                AssistantContext(),
            ),
            BusinessIntent.SALES_DATA_REQUEST,
        )

    def test_product_sales_request_intent(self) -> None:
        self.assertEqual(
            classify_business_intent("지난달 상품별 판매순위를 알려줘", AssistantContext()),
            BusinessIntent.SALES_DATA_REQUEST,
        )

    def test_customer_purchase_request_intent(self) -> None:
        self.assertEqual(
            classify_business_intent(
                "2026년 7월 1일부터 7월 31일까지 B.A크림50ML를 구매한 고객을 조회해줘",
                AssistantContext(),
            ),
            BusinessIntent.SALES_DATA_REQUEST,
        )

    def test_employee_sales_request_intent(self) -> None:
        self.assertEqual(
            classify_business_intent("직원별 매출현황 보여줘", AssistantContext()),
            BusinessIntent.SALES_DATA_REQUEST,
        )

    def test_store_sales_request_intent(self) -> None:
        self.assertEqual(
            classify_business_intent("매장별 실적을 비교해줘", AssistantContext()),
            BusinessIntent.SALES_DATA_REQUEST,
        )

    def test_sales_excel_routing_precedes_artifact(self) -> None:
        self.assertEqual(
            classify_business_intent("고객 리스트 Excel 만들어줘", AssistantContext()),
            BusinessIntent.SALES_DATA_REQUEST,
        )

    def test_sales_excel_request_routes_to_sales_agent(self) -> None:
        self.assertEqual(
            classify_business_intent(
                "지난달 구매 고객을 조회해서 엑셀로 만들어줘",
                AssistantContext(),
            ),
            BusinessIntent.SALES_DATA_REQUEST,
        )

    def test_general_tell_me_request_remains_chat(self) -> None:
        self.assertEqual(
            classify_business_intent("재미있는 이야기 알려줘", AssistantContext()),
            BusinessIntent.CHAT,
        )

    def test_unknown_fallback(self) -> None:
        self.assertEqual(classify_business_intent("12345", AssistantContext()), BusinessIntent.UNKNOWN)


class AgentResponseAdapterTests(unittest.TestCase):
    fixtures = Path("tests/fixtures")

    def fixture(self, name: str) -> dict:
        return json.loads((self.fixtures / name).read_text(encoding="utf-8"))

    def test_v2_model_alias_round_trip(self) -> None:
        raw = self.fixture("agent_response_v2_completed.json")
        parsed = AgentResponseV2.model_validate(raw)
        serialized = parsed.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)
        self.assertEqual(serialized["responseSchemaVersion"], "2.0")
        self.assertEqual(serialized["result"]["columns"], raw["result"]["columns"])
        self.assertEqual(serialized["result"]["rows"], raw["result"]["rows"])

    def test_v2_completed_is_normalized_without_column_changes(self) -> None:
        raw = self.fixture("agent_response_v2_completed.json")
        normalized = normalize_agent_response(raw).model_dump(by_alias=True, exclude_none=True)
        self.assertFalse(normalized["legacy"])
        self.assertEqual(normalized["result"]["columns"], raw["result"]["columns"])
        self.assertEqual(list(normalized["result"]["rows"][0]), list(raw["result"]["rows"][0]))
        self.assertEqual(normalized["result"]["download"]["downloadId"], "DL-V2-COMPLETED")
        self.assertNotIn("bucketName", normalized["result"]["download"])
        self.assertNotIn("objectKey", normalized["result"]["download"])

    def test_v2_ready_maps_to_front_compatible_status(self) -> None:
        normalized = normalize_agent_response(self.fixture("agent_response_v2_ready.json"))
        self.assertEqual(normalized.status, "READY_TO_EXECUTE")

    def test_v2_ready_to_execute_from_live_agent_is_accepted(self) -> None:
        normalized = normalize_agent_response({
            "requestId": "REQ-LIVE-READY",
            "responseSchemaVersion": "2.0",
            "status": "READY_TO_EXECUTE",
            "message": "실행 준비가 완료되었습니다.",
            "interpretation": {},
            "questions": [],
            "issues": [],
        })
        self.assertEqual(normalized.request_id, "REQ-LIVE-READY")
        self.assertEqual(normalized.status, "READY_TO_EXECUTE")

    def test_v2_result_processing_from_live_agent_is_accepted(self) -> None:
        from app.api.assistant import _sales_response

        normalized = normalize_agent_response({
            "requestId": "REQ-LIVE-PROCESSING",
            "responseSchemaVersion": "2.0",
            "status": "RESULT_PROCESSING",
            "message": "결과를 처리하고 있습니다.",
        })
        self.assertEqual(normalized.status, "RESULT_PROCESSING")
        status, _, _, _, _, actions = _sales_response(
            normalized.model_dump(by_alias=True, exclude_none=True)
        )
        self.assertEqual(status, "PROCESSING")
        self.assertEqual(actions[0].type, "request_status")

    def test_v2_failed_allows_empty_result_object(self) -> None:
        normalized = normalize_agent_response({
            "requestId": "REQ-LIVE-FAILED",
            "responseSchemaVersion": "2.0",
            "status": "FAILED",
            "message": "처리 실패",
            "interpretation": {},
            "questions": [],
            "result": {},
            "artifact": None,
            "issues": [],
        })
        self.assertEqual(normalized.request_id, "REQ-LIVE-FAILED")
        self.assertEqual(normalized.status, "FAILED")
        self.assertEqual(normalized.result, {})

    def test_v2_clarification_preserves_question_id_and_reason(self) -> None:
        normalized = normalize_agent_response(self.fixture("agent_response_v2_clarification.json"))
        self.assertEqual(normalized.reason_code, "BUSINESS_DEFINITION_AMBIGUOUS")
        self.assertEqual(normalized.questions[0]["questionId"], "q-1")

    def test_v1_completed_preserves_legacy_column_order(self) -> None:
        raw = self.fixture("agent_response_v1_completed.json")
        normalized = normalize_agent_response(raw)
        self.assertTrue(normalized.legacy)
        self.assertEqual(normalized.response_schema_version, "1.0")
        self.assertEqual(normalized.result["columns"], ["custom.second", "custom.first"])

    def test_v1_query_blocked_is_normalized(self) -> None:
        normalized = normalize_agent_response(self.fixture("agent_response_v1_query_blocked.json"))
        self.assertEqual(normalized.issues[0]["code"], "QUERY_BLOCKED")

    def test_v2_too_many_results_preserves_refinement_contract(self) -> None:
        normalized = normalize_agent_response(self.fixture("agent_response_v2_too_many_results.json"))
        serialized = normalized.model_dump(by_alias=True, exclude_none=True)
        self.assertEqual(serialized["issues"][0]["code"], "TOO_MANY_RESULTS")
        self.assertEqual(serialized["refinement"], {
            "limit": 100,
            "resultCount": 1842,
            "countExact": True,
            "suggestions": ["SPECIFY_PRODUCT", "SHORTEN_PERIOD"],
        })

    def test_too_many_results_maps_to_refinement_without_download(self) -> None:
        from app.api.assistant import _sales_response

        raw = self.fixture("agent_response_v2_too_many_results.json")
        normalized = normalize_agent_response(raw).model_dump(by_alias=True, exclude_none=True)
        status, message, _, data, issues, actions = _sales_response(normalized)

        self.assertEqual(status, "BLOCKED")
        self.assertEqual(message, raw["message"])
        self.assertEqual(data["errorCategory"], "BUSINESS_REFINEMENT")
        self.assertEqual(data["refinement"]["resultCount"], 1842)
        self.assertEqual(issues[0]["code"], "TOO_MANY_RESULTS")
        self.assertEqual([action.type for action in actions], ["refine_request"])
        self.assertEqual(actions[0].payload, {
            "reason": "TOO_MANY_RESULTS",
            "limit": 100,
            "resultCount": 1842,
            "countExact": True,
            "suggestions": ["SPECIFY_PRODUCT", "SHORTEN_PERIOD"],
        })

    def test_refinement_boundary_cases_follow_agent_decision(self) -> None:
        from app.api.assistant import _sales_response

        for result_count, is_blocked in ((99, False), (100, False), (101, True), (1842, True)):
            with self.subTest(result_count=result_count):
                response = {
                    "requestId": f"REQ-{result_count}",
                    "status": "BLOCKED" if is_blocked else "COMPLETED",
                    "refinement": {"limit": 100, "resultCount": result_count, "countExact": True},
                    "issues": ([{"code": "TOO_MANY_RESULTS", "severity": "BLOCKING"}] if is_blocked else []),
                }
                status, _, _, _, _, actions = _sales_response(response)
                self.assertEqual(status, "BLOCKED" if is_blocked else "COMPLETED")
                self.assertEqual(any(action.type == "refine_request" for action in actions), is_blocked)
                self.assertFalse(any(action.type == "download_file" for action in actions))
                if is_blocked:
                    self.assertEqual(actions[0].payload["resultCount"], result_count)

    def test_too_many_results_uses_deterministic_fallback_message(self) -> None:
        from app.api.assistant import _sales_response

        status, message, _, _, _, _ = _sales_response({
            "status": "BLOCKED",
            "issues": [{"code": "TOO_MANY_RESULTS"}],
            "refinement": {"limit": 100, "resultCount": 101},
        })
        self.assertEqual(status, "BLOCKED")
        self.assertEqual(message, "요청 결과가 현재 조회 제한을 초과합니다. 기간을 줄이거나 특정 상품을 지정해 주세요.")

    def test_result_too_large_is_business_response(self) -> None:
        from app.api.assistant import _sales_response

        status, _, _, data, issues, actions = _sales_response({
            "requestId": "REQ-RESULT-LARGE",
            "status": "BLOCKED",
            "issues": [{"code": "RESULT_TOO_LARGE", "severity": "BLOCKING"}],
            "refinement": {"limit": 100, "resultCount": 1000},
        })
        self.assertEqual(status, "BLOCKED")
        self.assertEqual(data["errorCategory"], "BUSINESS_REFINEMENT")
        self.assertEqual(issues[0]["code"], "RESULT_TOO_LARGE")
        self.assertEqual(actions[0].payload["reason"], "RESULT_TOO_LARGE")

    def test_unsupported_schema_version_is_rejected(self) -> None:
        with self.assertRaises(UnsupportedAgentSchemaVersion):
            normalize_agent_response({"responseSchemaVersion": "3.0", "status": "COMPLETED"})

    def test_v2_minio_metadata_is_registered_but_not_exposed(self) -> None:
        from app.api.assistant import _normalize_sales_result, _sales_response

        raw = self.fixture("agent_response_v2_completed.json")
        normalized = _normalize_sales_result(raw, tenant_id="default")
        download = normalized["result"]["download"]
        self.assertNotIn("bucketName", download)
        self.assertNotIn("objectKey", download)
        metadata = find_minio_download(download_id="DL-V2-COMPLETED")
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.object_key, raw["result"]["download"]["objectKey"])
        _, _, _, _, _, actions = _sales_response(normalized)
        action = next(item for item in actions if item.type == "download_file")
        self.assertEqual(action.payload["url"], "/api/assistant/requests/REQ-V2-COMPLETED/download")
        self.assertNotIn("objectKey", action.payload)


class RequestProjectionTests(unittest.TestCase):
    def test_projection_preview_history_and_access_control(self) -> None:
        with TemporaryDirectory() as directory:
            service = RequestProjectionService(RequestProjectionConfig(
                database_url=f"sqlite:///{Path(directory) / 'projection.db'}",
                preview_row_limit=100,
            ))
            response = {
                "status": "COMPLETED",
                "message": "완료",
                "result": {
                    "profileId": "customers",
                    "resultMode": "DETAIL",
                    "rowCount": 1842,
                    "truncated": False,
                    "summary": {"count": 1842},
                    "columns": [{"key": "customerId", "label": "고객 ID"}],
                    "rows": [{"customerId": index} for index in range(150)],
                    "download": {
                        "available": True,
                        "downloadId": "DL-PROJECTION-1",
                        "fileName": "customers.xlsx",
                        "status": "READY",
                        "bucketName": "must-not-be-projected",
                        "objectKey": "must-not-be-projected",
                    },
                },
            }
            service.project(
                request_id="REQ-PROJECTION-1",
                agent_request_id="REQ-PROJECTION-1",
                tenant_id="tenant-a",
                user_id="user-a",
                request_text="고객 자료를 조회해줘",
                response=response,
            )

            detail = service.get_owned("REQ-PROJECTION-1", "tenant-a", "user-a", [])
            self.assertEqual(detail["result"]["rowCount"], 1842)
            self.assertEqual(len(detail["result"]["rows"]), 100)
            self.assertTrue(detail["result"]["truncated"])
            self.assertEqual(detail["intent"], "SALES_DATA_REQUEST")
            self.assertNotIn("bucketName", detail["artifacts"][0])
            self.assertNotIn("objectKey", detail["artifacts"][0])
            self.assertIsNone(service.get_owned("REQ-PROJECTION-1", "tenant-a", "user-b", []))
            self.assertIsNone(service.get_owned("REQ-PROJECTION-1", "tenant-b", "user-a", ["ADMIN"]))
            self.assertIsNotNone(service.get_owned("REQ-PROJECTION-1", "tenant-a", "admin", ["ADMIN"]))

            listing = service.list_owned(
                tenant_id="tenant-a", user_id="user-a", roles=[], status="COMPLETED",
                date_from=None, date_to=None, keyword="고객", page=1, size=20,
            )
            self.assertEqual(listing["total"], 1)
            self.assertTrue(listing["items"][0]["downloadAvailable"])
            self.assertEqual(listing["items"][0]["resultProfileId"], "customers")
            service.engine.dispose()

    def test_artifact_failure_does_not_change_completed_request(self) -> None:
        with TemporaryDirectory() as directory:
            service = RequestProjectionService(RequestProjectionConfig(
                database_url=f"sqlite:///{Path(directory) / 'projection.db'}",
            ))
            service.project(
                request_id="REQ-ARTIFACT-FAILED",
                agent_request_id="REQ-ARTIFACT-FAILED",
                tenant_id="tenant-a",
                user_id="user-a",
                request_text="결과 파일 요청",
                response={
                    "status": "COMPLETED",
                    "result": {"rowCount": 1, "rows": [{"id": 1}]},
                    "artifact": {"artifactId": "ART-FAILED", "status": "READY"},
                },
            )

            service.mark_artifact_failed("ART-FAILED")
            detail = service.get_owned("REQ-ARTIFACT-FAILED", "tenant-a", "user-a", [])

            self.assertEqual(detail["status"], "COMPLETED")
            self.assertEqual(detail["artifacts"][0]["status"], "FAILED")
            service.engine.dispose()

    def test_existing_sqlite_projection_schema_is_migrated(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite:///{Path(directory) / 'projection.db'}"
            initial = RequestProjectionService(RequestProjectionConfig(database_url=database_url))
            with initial.engine.begin() as connection:
                connection.execute(text("DROP INDEX ix_user_data_request_intent"))
                connection.execute(text("DROP INDEX ix_user_data_request_artifact_tenant_id"))
                connection.execute(text("ALTER TABLE user_data_request DROP COLUMN intent"))
                connection.execute(text(
                    "ALTER TABLE user_data_request_artifact DROP COLUMN tenant_id"
                ))
            initial.engine.dispose()

            migrated = RequestProjectionService(RequestProjectionConfig(database_url=database_url))
            db_inspector = inspect(migrated.engine)
            request_columns = {
                column["name"] for column in db_inspector.get_columns("user_data_request")
            }
            artifact_columns = {
                column["name"]
                for column in db_inspector.get_columns("user_data_request_artifact")
            }
            request_indexes = {
                index["name"] for index in db_inspector.get_indexes("user_data_request")
            }
            artifact_indexes = {
                index["name"]
                for index in db_inspector.get_indexes("user_data_request_artifact")
            }
            self.assertIn("intent", request_columns)
            self.assertIn("tenant_id", artifact_columns)
            self.assertIn("ix_user_data_request_intent", request_indexes)
            self.assertIn("ix_user_data_request_artifact_tenant_id", artifact_indexes)
            migrated.engine.dispose()

    def test_progress_status_uses_business_friendly_event_message(self) -> None:
        with TemporaryDirectory() as directory:
            service = RequestProjectionService(RequestProjectionConfig(
                database_url=f"sqlite:///{Path(directory) / 'projection.db'}",
            ))
            service.project(
                request_id="REQ-PROGRESS-1",
                agent_request_id="REQ-PROGRESS-1",
                tenant_id="tenant-a",
                user_id="user-a",
                request_text="자료 요청",
                response={"status": "FILE_UPLOADING"},
            )
            detail = service.get_owned("REQ-PROGRESS-1", "tenant-a", "user-a", [])
            self.assertEqual(detail["events"][0]["message"], "결과 저장 중")
            self.assertEqual(detail["events"][0]["eventType"], "ARTIFACT_READY")
            service.engine.dispose()


class RequestProjectionEndpointTests(unittest.TestCase):
    def test_request_list_detail_and_tenant_ownership(self) -> None:
        from app.core.config import get_settings
        from app.main import app
        from app.services.request_projection_service import get_request_projection_service
        from app.services.sales_agent_service import get_sales_agent_client

        with TemporaryDirectory() as directory:
            service = RequestProjectionService(RequestProjectionConfig(
                database_url=f"sqlite:///{Path(directory) / 'projection.db'}",
            ))

            class MockClient:
                async def submit_request(self, request, *, request_id: str, session_id: str):
                    return {
                        "requestId": "REQ-HISTORY-1",
                        "status": "COMPLETED",
                        "message": "완료",
                        "result": {"rowCount": 1, "columns": ["customerId"], "rows": [{"customerId": "C1"}]},
                    }

                async def execute_request(self, *, agent_request_id: str, request_id: str):
                    return await self.submit_request(None, request_id=request_id, session_id="test")

            app.dependency_overrides[get_request_projection_service] = lambda: service
            app.dependency_overrides[get_sales_agent_client] = lambda: MockClient()
            try:
                with patch.dict(os.environ, {"FASTAPI_API_KEY": "test-fastapi-key"}):
                    get_settings.cache_clear()
                    client = TestClient(app)
                    auth = {"X-API-Key": "test-fastapi-key"}
                    created = client.post(
                        "/api/assistant/message",
                        headers=auth,
                        json={
                            "message": "고객 자료를 조회해줘",
                            "action": {"type": "confirm_execution", "requestId": "REQ-HISTORY-1"},
                            "context": {"tenantId": "tenant-a", "userId": "user-a"},
                        },
                    )
                    self.assertEqual(created.status_code, 200)
                    owner_headers = {**auth, "X-Tenant-Id": "tenant-a", "X-User-Id": "user-a"}
                    listing = client.get("/api/requests", headers=owner_headers)
                    self.assertEqual(listing.status_code, 200)
                    self.assertEqual(listing.json()["items"][0]["requestId"], "REQ-HISTORY-1")
                    detail = client.get("/api/requests/REQ-HISTORY-1", headers=owner_headers)
                    self.assertEqual(detail.status_code, 200)
                    self.assertEqual(detail.json()["result"]["rows"], [{"customerId": "C1"}])
                    forbidden = client.get(
                        "/api/requests/REQ-HISTORY-1",
                        headers={**auth, "X-Tenant-Id": "tenant-b", "X-User-Id": "user-a", "X-Roles": "ADMIN"},
                    )
                    self.assertEqual(forbidden.status_code, 404)
            finally:
                get_settings.cache_clear()
                app.dependency_overrides.clear()
                service.engine.dispose()

    def test_header_identity_and_ready_execute_are_projected(self) -> None:
        from app.core.config import get_settings
        from app.main import app
        from app.services.request_projection_service import get_request_projection_service
        from app.services.sales_agent_service import get_sales_agent_client

        with TemporaryDirectory() as directory:
            service = RequestProjectionService(RequestProjectionConfig(
                database_url=f"sqlite:///{Path(directory) / 'projection.db'}",
                preview_row_limit=100,
            ))

            class MockClient:
                submitted_tenant = None

                async def submit_request(self, request, *, request_id: str, session_id: str):
                    self.submitted_tenant = request.tenant_id
                    return {"requestId": "REQ-STAGED", "status": "READY", "message": "준비"}

                async def execute_request(self, *, agent_request_id: str, request_id: str):
                    return {
                        "requestId": agent_request_id,
                        "status": "COMPLETED",
                        "message": "완료",
                        "result": {
                            "profileId": "PRODUCT_RANKING",
                            "resultMode": "DETAIL",
                            "rowCount": 150,
                            "truncated": False,
                            "columns": [{"key": "rank"}],
                            "rows": [{"rank": index} for index in range(150)],
                        },
                        "artifact": {
                            "artifactId": "ART-STAGED",
                            "fileName": "ranking.xlsx",
                            "status": "READY",
                        },
                    }

            mock = MockClient()
            app.dependency_overrides[get_request_projection_service] = lambda: service
            app.dependency_overrides[get_sales_agent_client] = lambda: mock
            try:
                with patch.dict(os.environ, {"FASTAPI_API_KEY": "test-fastapi-key"}):
                    get_settings.cache_clear()
                    response = TestClient(app).post(
                        "/api/assistant/message",
                        headers={
                            "X-API-Key": "test-fastapi-key",
                            "X-Tenant-Id": "header-tenant",
                            "X-User-Id": "header-user",
                        },
                        json={
                            "tenantId": "body-tenant",
                            "message": "상품별 판매순위를 조회해줘",
                            "execution": {"allowExecute": True, "allowPii": False},
                        },
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["requestId"], "REQ-STAGED")
                self.assertEqual(len(response.json()["data"]["result"]["rows"]), 100)
                self.assertTrue(response.json()["data"]["result"]["truncated"])
                self.assertEqual(mock.submitted_tenant, "header-tenant")
                detail = service.get_owned(
                    "REQ-STAGED", "header-tenant", "header-user", []
                )
                self.assertEqual(detail["intent"], "SALES_DATA_REQUEST")
                self.assertEqual(detail["status"], "COMPLETED")
                self.assertEqual(
                    [event["status"] for event in detail["events"]],
                    ["READY_TO_EXECUTE", "COMPLETED"],
                )
                self.assertIsNone(
                    service.get_owned("REQ-STAGED", "body-tenant", "header-user", [])
                )
            finally:
                get_settings.cache_clear()
                app.dependency_overrides.clear()
                service.engine.dispose()

    def test_download_failure_only_marks_artifact_failed(self) -> None:
        from app.core.config import get_settings
        from app.main import app
        from app.services.minio_storage_service import (
            ObjectStorageError,
            get_minio_storage_service,
        )
        from app.services.request_projection_service import get_request_projection_service
        from app.services.sales_agent_service import get_sales_agent_client

        with TemporaryDirectory() as directory:
            service = RequestProjectionService(RequestProjectionConfig(
                database_url=f"sqlite:///{Path(directory) / 'projection.db'}",
            ))
            service.project(
                request_id="REQ-DOWNLOAD-FAILED",
                agent_request_id="REQ-DOWNLOAD-FAILED",
                tenant_id="tenant-a",
                user_id="user-a",
                request_text="파일 요청",
                response={
                    "status": "COMPLETED",
                    "result": {"rowCount": 1, "rows": [{"id": 1}]},
                    "artifact": {"artifactId": "ART-DOWNLOAD-FAILED", "status": "READY"},
                },
            )

            class FailingDownloadClient:
                calls = 0

                async def download_artifact(self, *, artifact_id: str, request_id: str):
                    self.calls += 1
                    raise SalesAgentError(
                        "파일을 찾을 수 없습니다.",
                        code="ARTIFACT_NOT_FOUND",
                        http_status=404,
                    )

            class MissingStorage:
                def find_artifact_object_key(self, artifact_id: str):
                    raise ObjectStorageError(
                        "Artifact object was not found.",
                        code="ARTIFACT_NOT_FOUND",
                    )

            failing_client = FailingDownloadClient()
            app.dependency_overrides[get_request_projection_service] = lambda: service
            app.dependency_overrides[get_sales_agent_client] = lambda: failing_client
            app.dependency_overrides[get_minio_storage_service] = lambda: MissingStorage()
            try:
                with patch.dict(os.environ, {"FASTAPI_API_KEY": "test-fastapi-key"}):
                    get_settings.cache_clear()
                    client = TestClient(app)
                    forbidden = client.get(
                        "/api/artifacts/ART-DOWNLOAD-FAILED/download",
                        headers={
                            "X-API-Key": "test-fastapi-key",
                            "X-Tenant-Id": "tenant-a",
                            "X-User-Id": "other-user",
                        },
                    )
                    self.assertEqual(forbidden.status_code, 403)
                    self.assertEqual(failing_client.calls, 0)
                    response = client.get(
                        "/api/artifacts/ART-DOWNLOAD-FAILED/download",
                        headers={
                            "X-API-Key": "test-fastapi-key",
                            "X-Tenant-Id": "tenant-a",
                            "X-User-Id": "user-a",
                        },
                    )
                self.assertEqual(response.status_code, 404)
                self.assertEqual(failing_client.calls, 1)
                detail = service.get_owned(
                    "REQ-DOWNLOAD-FAILED", "tenant-a", "user-a", []
                )
                self.assertEqual(detail["status"], "COMPLETED")
                self.assertEqual(detail["artifacts"][0]["status"], "FAILED")
            finally:
                get_settings.cache_clear()
                app.dependency_overrides.clear()
                service.engine.dispose()

    def test_download_falls_back_to_minio_artifact_lookup(self) -> None:
        from app.core.config import get_settings
        from app.main import app
        from app.services.minio_storage_service import get_minio_storage_service
        from app.services.request_projection_service import get_request_projection_service
        from app.services.sales_agent_service import get_sales_agent_client

        with TemporaryDirectory() as directory:
            service = RequestProjectionService(RequestProjectionConfig(
                database_url=f"sqlite:///{Path(directory) / 'projection.db'}",
            ))
            service.project(
                request_id="REQ-MINIO-FALLBACK",
                agent_request_id="REQ-MINIO-FALLBACK",
                tenant_id="tenant-a",
                user_id="user-a",
                request_text="고객 목록",
                response={
                    "status": "COMPLETED",
                    "result": {"rowCount": 1, "rows": []},
                    "artifact": {
                        "artifactId": "ART-MINIO-FALLBACK",
                        "fileName": "customers.xlsx",
                        "contentType": (
                            "application/vnd.openxmlformats-officedocument."
                            "spreadsheetml.sheet"
                        ),
                        "status": "READY",
                    },
                },
            )

            class MissingAgentDownload:
                async def download_artifact(self, *, artifact_id: str, request_id: str):
                    raise SalesAgentError(
                        "Agent download URL is unavailable.",
                        code="ARTIFACT_NOT_FOUND",
                        http_status=404,
                    )

            class ObjectResponse:
                def stream(self, amt: int):
                    yield b"PK\x03\x04xlsx"

                def close(self):
                    return None

                def release_conn(self):
                    return None

            class MinioFallback:
                def find_artifact_object_key(self, artifact_id: str):
                    self.artifact_id = artifact_id
                    return "2026/09/REQ-1/ART-MINIO-FALLBACK/artifact.xlsx"

                def get_object(self, *, object_key: str, bucket_name=None):
                    self.object_key = object_key
                    return ObjectResponse()

            storage = MinioFallback()
            app.dependency_overrides[get_request_projection_service] = lambda: service
            app.dependency_overrides[get_sales_agent_client] = lambda: MissingAgentDownload()
            app.dependency_overrides[get_minio_storage_service] = lambda: storage
            try:
                with patch.dict(os.environ, {"FASTAPI_API_KEY": "test-fastapi-key"}):
                    get_settings.cache_clear()
                    response = TestClient(app).get(
                        "/api/artifacts/ART-MINIO-FALLBACK/download",
                        headers={
                            "X-API-Key": "test-fastapi-key",
                            "X-Tenant-Id": "tenant-a",
                            "X-User-Id": "user-a",
                        },
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b"PK\x03\x04xlsx")
                self.assertIn("customers.xlsx", response.headers["content-disposition"])
                self.assertEqual(storage.artifact_id, "ART-MINIO-FALLBACK")
                detail = service.get_owned(
                    "REQ-MINIO-FALLBACK", "tenant-a", "user-a", []
                )
                self.assertEqual(detail["artifacts"][0]["status"], "READY")
            finally:
                get_settings.cache_clear()
                app.dependency_overrides.clear()
                service.engine.dispose()


class MinioStorageTests(unittest.TestCase):
    def test_object_key_contains_only_scope_and_opaque_download_id(self) -> None:
        from datetime import datetime

        key = build_export_object_key(
            environment="prod",
            tenant_id="tenant-a",
            request_id="REQ-20260903-0001",
            now=datetime(2026, 9, 3),
        )
        self.assertRegex(key, r"^prod/tenant-a/agent-results/2026/09/REQ-20260903-0001/DL-[a-f0-9]{32}\.xlsx$")
        self.assertNotIn("고객", key)

    def test_upload_uses_internal_bucket_and_returns_metadata(self) -> None:
        class Result:
            etag = "etag-1"

        class Client:
            def put_object(self, bucket, object_key, stream, *, length, content_type):
                self.args = (bucket, object_key, stream.read(), length, content_type)
                return Result()

        fake = Client()
        service = MinioStorageService(
            MinioConfig(enabled=True, bucket_exports="exports"),
            client=fake,
        )
        stored = service.upload_bytes(
            object_key="dev/default/agent-results/2026/09/REQ-1/DL-1.xlsx",
            content=b"PK\x03\x04",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertEqual(stored.size_bytes, 4)
        self.assertEqual(stored.etag, "etag-1")
        self.assertEqual(fake.args[0], "exports")

    def test_artifact_object_key_is_resolved_by_exact_path_segment(self) -> None:
        class Item:
            def __init__(self, object_name: str) -> None:
                self.object_name = object_name

        class Client:
            def list_objects(self, bucket, *, recursive):
                self.args = (bucket, recursive)
                return [
                    Item("2026/09/REQ-1/ART-OTHER/artifact.xlsx"),
                    Item("2026/09/REQ-1/ART-TARGET/artifact.xlsx"),
                ]

        fake = Client()
        service = MinioStorageService(
            MinioConfig(enabled=True, bucket_exports="exports"),
            client=fake,
        )

        key = service.find_artifact_object_key("ART-TARGET")

        self.assertEqual(key, "2026/09/REQ-1/ART-TARGET/artifact.xlsx")
        self.assertEqual(fake.args, ("exports", True))


class SalesAgentClientTests(unittest.IsolatedAsyncioTestCase):
    def test_request_serialization_matches_agent_fixture(self) -> None:
        fixture_path = Path("tests/fixtures/sales_agent_request_valid.json")
        expected = json.loads(fixture_path.read_text(encoding="utf-8"))
        request = AssistantMessageRequest.model_validate({
            "message": "지난달 특정 상품 구매 고객을 조회해줘",
            "context": {"tenantId": "default"},
        })

        actual = build_sales_agent_request(
            request,
            default_tenant="default",
        ).model_dump(by_alias=True, exclude_none=True)

        self.assertEqual(actual, expected)
        self.assertEqual(set(actual), {"tenantId", "message", "execution"})
        self.assertEqual(set(actual["execution"]), {"allowExecute", "allowPii"})

    async def test_disabled(self) -> None:
        client = HttpSalesAgentClient(SalesAgentConfig(enabled=False))
        with self.assertRaisesRegex(SalesAgentError, "비활성화"):
            await client.process(AssistantMessageRequest(message="구매 고객"), request_id="r1", session_id="s1")

    async def test_timeout(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timeout", request=request)
        client = HttpSalesAgentClient(SalesAgentConfig(enabled=True), transport=httpx.MockTransport(handler))
        with self.assertRaises(SalesAgentError) as raised:
            await client.process(AssistantMessageRequest(message="구매 고객"), request_id="r1", session_id="s1")
        self.assertEqual(raised.exception.code, "AGENT_REQUEST_TIMEOUT")

    async def test_connect_timeout(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timeout", request=request)
        client = HttpSalesAgentClient(SalesAgentConfig(enabled=True), transport=httpx.MockTransport(handler))
        with self.assertRaises(SalesAgentError) as raised:
            await client.process(AssistantMessageRequest(message="구매 고객"), request_id="r1", session_id="s1")
        self.assertEqual(raised.exception.code, "AGENT_CONNECT_TIMEOUT")

    async def test_connection_error(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)
        client = HttpSalesAgentClient(SalesAgentConfig(enabled=True), transport=httpx.MockTransport(handler))
        with self.assertRaises(SalesAgentError) as raised:
            await client.process(AssistantMessageRequest(message="구매 고객"), request_id="r1", session_id="s1")
        self.assertEqual(raised.exception.code, "AGENT_CONNECTION_FAILED")

    async def test_invalid_json(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not-json")
        client = HttpSalesAgentClient(SalesAgentConfig(enabled=True), transport=httpx.MockTransport(handler))
        with self.assertRaises(SalesAgentError) as raised:
            await client.process(AssistantMessageRequest(message="구매 고객"), request_id="r1", session_id="s1")
        self.assertEqual(raised.exception.code, "INVALID_AGENT_RESPONSE")

    async def test_missing_create_request_id(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "READY", "message": "ready"})
        client = HttpSalesAgentClient(SalesAgentConfig(enabled=True), transport=httpx.MockTransport(handler))
        with self.assertRaises(SalesAgentError) as raised:
            await client.process(AssistantMessageRequest(message="구매 고객"), request_id="r1", session_id="s1")
        self.assertEqual(raised.exception.code, "INVALID_AGENT_RESPONSE")

    async def test_unauthorized(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "unauthorized"})
        client = HttpSalesAgentClient(SalesAgentConfig(enabled=True), transport=httpx.MockTransport(handler))
        with self.assertRaises(SalesAgentError) as raised:
            await client.process(AssistantMessageRequest(message="구매 고객"), request_id="r1", session_id="s1")
        self.assertEqual(raised.exception.code, "AGENT_UNAUTHORIZED")

    async def test_request_id_propagation(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["X-Request-Id"], "correlation-1")
            return httpx.Response(200, json={"requestId": "agent-1", "status": "COMPLETED", "message": "done"})
        client = HttpSalesAgentClient(SalesAgentConfig(enabled=True), transport=httpx.MockTransport(handler))
        await client.process(AssistantMessageRequest(message="구매 고객"), request_id="correlation-1", session_id="s1")

    async def test_context_mapping(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertEqual(payload["tenantId"], "tenant-a")
            self.assertEqual(payload["message"], "구매 고객")
            self.assertEqual(payload["execution"], {"allowExecute": False, "allowPii": False})
            self.assertEqual(set(payload), {"tenantId", "message", "execution"})
            self.assertEqual(request.headers["X-Agent-Api-Key"], "agent-secret")
            self.assertEqual(request.headers["X-Agent-Caller"], "fastapi")
            return httpx.Response(200, json={"requestId": "agent-1", "status": "COMPLETED", "message": "done"})
        request = AssistantMessageRequest.model_validate({
            "message": "구매 고객",
            "context": {"tenantId": "tenant-a", "userId": "user-a", "roles": ["SALES"], "storeId": "store-a", "currentMenu": "sales"},
        })
        client = HttpSalesAgentClient(SalesAgentConfig(enabled=True, api_key="agent-secret"), transport=httpx.MockTransport(handler))
        await client.process(request, request_id="r1", session_id="s1")

    async def test_http_500(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal"})
        client = HttpSalesAgentClient(SalesAgentConfig(enabled=True), transport=httpx.MockTransport(handler))
        with self.assertRaises(SalesAgentError) as raised:
            await client.process(AssistantMessageRequest(message="구매 고객"), request_id="r1", session_id="s1")
        self.assertEqual(raised.exception.code, "AGENT_REQUEST_FAILED")

    async def test_top_level_tenant_and_pii_mapping(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.assertEqual(payload["tenantId"], "tenant-direct")
            self.assertEqual(payload["execution"], {"allowExecute": False, "allowPii": True})
            return httpx.Response(200, json={"requestId": "agent-1", "status": "READY", "message": "ready"})

        client = HttpSalesAgentClient(
            SalesAgentConfig(enabled=True),
            transport=httpx.MockTransport(handler),
        )
        request = AssistantMessageRequest.model_validate({
            "tenantId": "tenant-direct",
            "message": "구매 고객 조회",
            "execution": {"allowExecute": True, "allowPii": True},
        })
        await client.process(request, request_id="r1", session_id="s1")

    async def test_health_200(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/admin/dashboard")
            return httpx.Response(200, json={"status": "UP"})
        client = HttpSalesAgentClient(SalesAgentConfig(enabled=True), transport=httpx.MockTransport(handler))
        self.assertEqual(await client.health(), "UP")

    async def test_execute_api_called_once(self) -> None:
        calls = 0
        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            self.assertEqual(request.url.path, "/api/v1/agent/requests/agent-123/execute")
            self.assertEqual(request.headers["X-Agent-Caller"], "fastapi")
            return httpx.Response(200, json={"requestId": "agent-123", "status": "COMPLETED"})
        client = HttpSalesAgentClient(SalesAgentConfig(enabled=True), transport=httpx.MockTransport(handler))
        result = await client.execute(agent_request_id="agent-123", request_id="gateway-1")
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(calls, 1)

    async def test_execute_timeout_is_not_retried(self) -> None:
        calls = 0
        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            raise httpx.ReadTimeout("timeout", request=request)
        client = HttpSalesAgentClient(SalesAgentConfig(enabled=True), transport=httpx.MockTransport(handler))
        with self.assertRaises(SalesAgentError) as raised:
            await client.execute(agent_request_id="agent-123", request_id="gateway-1")
        self.assertEqual(raised.exception.code, "AGENT_EXECUTE_TIMEOUT")
        self.assertEqual(calls, 1)


class GatewayPolicyTests(unittest.TestCase):
    def test_canonical_tvsales_agent_environment_names_take_precedence(self) -> None:
        from app.core.config import get_settings

        with patch.dict(
            os.environ,
            {
                "TV_SALES_AGENT_BASE_URL": "http://legacy-agent:8080",
                "TVS_AGENT_BASE_URL": "http://intermediate-agent:8080",
                "TVSALES_AGENT_BASE_URL": "http://canonical-agent:7071",
                "TVSALES_AGENT_HEALTH_PATH": "/api/admin/dashboard",
                "TVSALES_AGENT_API_KEY": "canonical-secret",
            },
        ):
            get_settings.cache_clear()
            settings = get_settings().sales_agent
            self.assertEqual(settings.base_url, "http://canonical-agent:7071")
            self.assertEqual(settings.health_path, "/api/admin/dashboard")
            self.assertEqual(settings.api_key, "canonical-secret")
        get_settings.cache_clear()

    def test_sales_agent_api_key_compatibility_env_name(self) -> None:
        from app.core.config import get_settings

        with patch.dict(
            os.environ,
            {"TV_SALES_AGENT_API_KEY": "compat-agent-secret"},
        ):
            os.environ.pop("TVSALES_AGENT_API_KEY", None)
            os.environ.pop("TVS_AGENT_API_KEY", None)
            get_settings.cache_clear()
            self.assertEqual(get_settings().sales_agent.api_key, "compat-agent-secret")
        get_settings.cache_clear()

    def test_manual_11_health_compatibility_routes(self) -> None:
        from app.main import app

        client = TestClient(app)
        response = client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "UP", "service": "tvs-ai-gateway"})

        with patch(
            "app.api.health.HttpSalesAgentClient.health",
            new=AsyncMock(return_value="UP"),
        ):
            response = client.get("/health/agent")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "UP", "agent": "UP"})

    def test_requires_clarification_mapping(self) -> None:
        from app.api.assistant import _sales_response
        status, _, _, _, _, actions = _sales_response({"status": "REQUIRES_CLARIFICATION"})
        self.assertEqual((status, actions[0].type), ("REQUIRES_CLARIFICATION", "clarification"))

    def test_completed_mapping(self) -> None:
        from app.api.assistant import _sales_response
        status, _, _, data, _, actions = _sales_response({
            "requestId": "agent-1",
            "status": "COMPLETED",
            "result": {
                "rowCount": 12,
                "validation": {"status": "PASS", "deliverable": True},
                "rows": [{"customerName": "must-not-leak"}],
            },
            "artifact": {
                "artifactId": "artifact-1",
                "fileName": "result.xlsx",
                "mediaType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "encrypted": True,
                "downloadUrl": "/api/artifacts/artifact-1/download",
            },
        })
        self.assertEqual(status, "COMPLETED")
        self.assertIn("download_file", [item.type for item in actions])
        self.assertEqual(data["rowCount"], 12)
        self.assertEqual(data["result"]["rows"], [{"customerName": "must-not-leak"}])
        download = next(item for item in actions if item.type == "download_file")
        self.assertEqual(download.payload["url"], "/api/artifacts/artifact-1/download")
        self.assertNotIn("downloadUrl", download.payload)

    def test_completed_artifact_id_without_upstream_url_is_downloadable(self) -> None:
        from app.api.assistant import _sales_response

        status, _, _, data, _, actions = _sales_response({
            "requestId": "REQ-LIVE-COMPLETED",
            "status": "COMPLETED",
            "result": {
                "profileId": "CUSTOMER_LIST",
                "resultMode": "DETAIL",
                "rowCount": 6,
                "columns": [],
                "rows": [],
                "download": {
                    "available": True,
                    "format": "XLSX",
                    "title": "고객 목록",
                },
            },
            "artifact": {
                "artifactId": "ART-LIVE-1",
                "fileName": "customers.xlsx",
                "mediaType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "size": 10752,
            },
        })

        self.assertEqual(status, "COMPLETED")
        self.assertEqual(data["result"]["artifact"]["status"], "READY")
        self.assertEqual(data["result"]["artifact"]["fileSize"], 10752)
        download = next(item for item in actions if item.type == "download_file")
        self.assertEqual(download.payload["url"], "/api/artifacts/ART-LIVE-1/download")

    def test_ready_blocked_failed_mapping(self) -> None:
        from app.api.assistant import _sales_response
        cases = {
            "READY_TO_EXECUTE": "confirm_execution",
            "BLOCKED": "blocked_message",
            "FAILED": "safe_error",
        }
        for status, action_type in cases.items():
            with self.subTest(status=status):
                mapped, _, _, _, _, actions = _sales_response({"status": status})
                self.assertEqual((mapped, actions[0].type), (status, action_type))

    def test_unsupported_status_safe_error(self) -> None:
        from app.api.assistant import _sales_response
        status, _, _, data, _, actions = _sales_response({"status": "SOMETHING_NEW"})
        self.assertEqual(status, "ERROR")
        self.assertEqual(data["errorCode"], "SALES_AGENT_UNSUPPORTED_STATUS")
        self.assertEqual(actions[0].type, "safe_error")

    def test_query_blocked_is_business_block(self) -> None:
        from app.api.assistant import _sales_response
        status, _, _, data, issues, actions = _sales_response({
            "requestId": "agent-1",
            "status": "FAILED",
            "issues": [{"code": "QUERY_BLOCKED", "message": "조건이 확정되지 않았습니다.", "sql": "hidden"}],
        })
        self.assertEqual(status, "BLOCKED")
        self.assertEqual(data["errorCategory"], "BUSINESS_BLOCKED")
        self.assertEqual(issues, [{"code": "QUERY_BLOCKED", "message": "조건이 확정되지 않았습니다."}])
        self.assertEqual(actions[0].type, "blocked_message")

    def test_live_agent_query_blocked_response_is_preserved_safely(self) -> None:
        from app.api.assistant import _sales_response

        status, message, request_id, data, issues, actions = _sales_response({
            "requestId": "REQ-20260830-000001",
            "status": "FAILED",
            "message": "처리 중 오류가 발생했습니다.",
            "interpretation": None,
            "questions": [],
            "result": None,
            "artifact": None,
            "issues": [{
                "code": "QUERY_BLOCKED",
                "severity": "BLOCKING",
                "field": None,
                "message": "조회 조건 검증을 통과하지 못했습니다.",
            }],
        })

        self.assertEqual(status, "BLOCKED")
        self.assertEqual(message, "처리 중 오류가 발생했습니다.")
        self.assertEqual(request_id, "REQ-20260830-000001")
        self.assertEqual(data["agentRequestId"], "REQ-20260830-000001")
        self.assertEqual(data["errorCategory"], "BUSINESS_BLOCKED")
        self.assertEqual(issues[0]["code"], "QUERY_BLOCKED")
        self.assertEqual(issues[0]["severity"], "BLOCKING")
        self.assertNotIn("interpretation", data)
        self.assertEqual(actions[0].type, "blocked_message")

    def test_questions_override_failed(self) -> None:
        from app.api.assistant import _sales_response
        status, _, _, _, _, actions = _sales_response({
            "requestId": "agent-1",
            "status": "FAILED",
            "questions": [{"questionId": "q-1", "message": "상품을 선택하세요", "options": []}],
        })
        self.assertEqual(status, "REQUIRES_CLARIFICATION")
        self.assertEqual(actions[0].payload["questions"][0]["questionId"], "q-1")

    def test_no_secret_literals(self) -> None:
        text = "\n".join(path.read_text(encoding="utf-8") for path in Path("configs").glob("app*.yaml"))
        self.assertIsNone(re.search(r"sk-[A-Za-z0-9_-]{20,}|qwer1234|password123|local-dev-key-change-me", text))

    def test_no_sales_db_fallback(self) -> None:
        source = Path("app/api/assistant.py").read_text(encoding="utf-8")
        self.assertNotIn("/api/db/query", source)
        self.assertNotIn("QueryService", source)

    def test_no_pii_debug_logging(self) -> None:
        sources = "\n".join(
            Path(path).read_text(encoding="utf-8")
            for path in ("app/api/assistant.py", "app/services/sales_agent_service.py")
        )
        self.assertNotRegex(sources, r"print\(|LOG\.(?:info|warning|error)\([^\n]*(?:message|context|result)")


class MockSalesAgentIntegrationTests(unittest.TestCase):
    def test_all_agent_statuses_through_assistant_endpoint(self) -> None:
        from app.main import app
        from app.core.config import get_settings
        from app.services.sales_agent_service import get_sales_agent_client

        class MockClient:
            def __init__(self) -> None:
                self.status = "COMPLETED"
                self.executed_request_id = None

            async def submit_request(self, request, *, request_id: str, session_id: str):
                response = {
                    "requestId": "agent-request-1",
                    "status": self.status,
                    "message": "mock",
                }
                if self.status == "COMPLETED":
                    response["result"] = {
                        "rowCount": 1,
                        "validation": {"status": "PASS", "deliverable": True},
                    }
                    response["artifact"] = {
                        "artifactId": "artifact-1",
                        "fileName": "result.xlsx",
                        "mediaType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        "encrypted": True,
                        "downloadUrl": "/downloads/artifact-1",
                    }
                if self.status == "REQUIRES_CLARIFICATION":
                    response["questions"] = [{"questionId": "q-1", "text": "기간을 선택하세요"}]
                return response

            async def execute_request(self, *, agent_request_id: str, request_id: str):
                self.executed_request_id = agent_request_id
                return {"requestId": agent_request_id, "status": "COMPLETED", "message": "executed"}

            async def download_artifact(self, *, artifact_id: str, request_id: str):
                class MockStream:
                    def __init__(self) -> None:
                        self.response = httpx.Response(
                            200,
                            headers={
                                "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                "Content-Disposition": "attachment; filename=result.xlsx",
                            },
                        )

                    async def chunks(self):
                        yield b"PK\x03\x04mock-xlsx"

                    async def close(self):
                        return None

                self.assert_artifact_id = artifact_id
                return MockStream()

        mock = MockClient()
        app.dependency_overrides[get_sales_agent_client] = lambda: mock
        try:
            with patch.dict(os.environ, {"FASTAPI_API_KEY": "test-fastapi-key"}):
                get_settings.cache_clear()
                client = TestClient(app)
                headers = {"X-API-Key": "test-fastapi-key", "X-Request-Id": "mock-rid"}
                self.assertEqual(
                    client.post("/api/assistant/message", json={"message": "구매 고객"}).status_code,
                    401,
                )
                self.assertEqual(
                    client.post(
                        "/api/assistant/message",
                        headers={"Authorization": "Bearer wrong-key"},
                        json={"message": "구매 고객"},
                    ).status_code,
                    401,
                )
                bearer_response = client.post(
                    "/api/assistant/message",
                    headers={"Authorization": "Bearer test-fastapi-key"},
                    json={"message": "지난달 구매 고객을 알려줘"},
                )
                self.assertEqual(bearer_response.status_code, 200)
                self.assertEqual(bearer_response.json()["intent"], "SALES_DATA_REQUEST")

                expected = {
                    "REQUIRES_CLARIFICATION": "clarification",
                    "READY_TO_EXECUTE": "confirm_execution",
                    "COMPLETED": "result_preview",
                    "BLOCKED": "blocked_message",
                    "FAILED": "safe_error",
                }
                for status, action_type in expected.items():
                    with self.subTest(status=status):
                        mock.status = status
                        response = client.post(
                            "/api/assistant/message",
                            headers=headers,
                            json={"message": "지난달 구매 고객을 알려줘", "context": {"tenantId": "default"}},
                        )
                        body = response.json()
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(body["requestId"], "agent-request-1")
                        self.assertEqual(body["status"], status)
                        self.assertEqual(body["actions"][0]["type"], action_type)
                        if status == "REQUIRES_CLARIFICATION":
                            self.assertEqual(body["actions"][0]["payload"]["questions"][0]["questionId"], "q-1")

                mock.status = "READY"
                mock.executed_request_id = None
                response = client.post(
                    "/api/assistant/message",
                    headers=headers,
                    json={
                        "tenantId": "default",
                        "message": "구매 고객 조회",
                        "execution": {"allowExecute": True, "allowPii": False},
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "COMPLETED")
                self.assertEqual(mock.executed_request_id, "agent-request-1")

                response = client.post(
                    "/api/assistant/message",
                    headers=headers,
                    json={
                        "message": "조회 실행",
                        "action": {"type": "confirm_execution", "requestId": "agent-request-1"},
                        "context": {"tenantId": "default"},
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "COMPLETED")
                self.assertEqual(mock.executed_request_id, "agent-request-1")

                response = client.post(
                    "/api/assistant/requests/agent-request-1/execute",
                    headers=headers,
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["requestId"], "agent-request-1")

                response = client.get(
                    "/api/assistant/artifacts/artifact-1",
                    headers=headers,
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.headers["content-type"],
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
                self.assertTrue(response.content.startswith(b"PK\x03\x04"))
                self.assertEqual(mock.assert_artifact_id, "artifact-1")
        finally:
            get_settings.cache_clear()
            app.dependency_overrides.clear()


class TooManyResultsEndpointTests(unittest.TestCase):
    def test_too_many_results_is_http_200_business_response(self) -> None:
        from app.core.config import get_settings
        from app.main import app
        from app.services.sales_agent_service import get_sales_agent_client

        fixture = json.loads(
            Path("tests/fixtures/agent_response_v2_too_many_results.json").read_text(encoding="utf-8")
        )

        class MockClient:
            async def submit_request(self, request, *, request_id: str, session_id: str):
                return fixture

        app.dependency_overrides[get_sales_agent_client] = lambda: MockClient()
        try:
            with patch.dict(os.environ, {"FASTAPI_API_KEY": "test-fastapi-key"}):
                get_settings.cache_clear()
                response = TestClient(app).post(
                    "/api/assistant/message",
                    headers={"X-API-Key": "test-fastapi-key"},
                    json={
                        "message": "지난달 특정 상품 구매 고객을 조회해줘",
                        "sessionId": "session-refinement-1",
                        "context": {"tenantId": "default"},
                    },
                )

            body = response.json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(body["status"], "BLOCKED")
            self.assertEqual(body["sessionId"], "session-refinement-1")
            self.assertEqual(body["requestId"], "REQ-V2-TOO-MANY")
            self.assertEqual(body["issues"][0]["code"], "TOO_MANY_RESULTS")
            self.assertEqual(body["actions"][0]["type"], "refine_request")
            self.assertEqual(body["actions"][0]["payload"]["resultCount"], 1842)
            self.assertNotIn("download_file", [action["type"] for action in body["actions"]])
        finally:
            get_settings.cache_clear()
            app.dependency_overrides.clear()


if __name__ == "__main__":
    unittest.main()
