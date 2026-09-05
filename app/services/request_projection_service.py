from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy import DateTime, Integer, String, Text, create_engine, func, inspect, or_, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.core.config import RequestProjectionConfig, get_settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


_STATUS_MESSAGES = {
    "RECEIVED": "요청 접수",
    "INTERPRETING": "요청 분석 중",
    "PLANNING": "조회 준비 중",
    "READY": "조회 준비 완료",
    "READY_TO_EXECUTE": "조회 준비 완료",
    "EXECUTING": "데이터 조회 중",
    "FILE_GENERATING": "결과 파일 생성 중",
    "FILE_UPLOADING": "결과 저장 중",
    "COMPLETED": "완료",
    "REQUIRES_CLARIFICATION": "추가 조건 확인 필요",
    "BLOCKED": "요청 조건 수정 필요",
    "FAILED": "처리 실패",
}

_EVENT_TYPES = {
    "RECEIVED": "REQUEST_RECEIVED",
    "INTERPRETING": "AGENT_REQUEST_CREATED",
    "PLANNING": "AGENT_REQUEST_CREATED",
    "READY": "AGENT_REQUEST_CREATED",
    "READY_TO_EXECUTE": "AGENT_REQUEST_CREATED",
    "EXECUTING": "EXECUTION_STARTED",
    "RESULT_PROCESSING": "EXECUTION_COMPLETED",
    "FILE_GENERATING": "EXECUTION_COMPLETED",
    "FILE_UPLOADING": "ARTIFACT_READY",
    "COMPLETED": "REQUEST_COMPLETED",
    "REQUIRES_CLARIFICATION": "AGENT_REQUEST_CREATED",
    "BLOCKED": "REQUEST_FAILED",
    "FAILED": "REQUEST_FAILED",
}


class Base(DeclarativeBase):
    pass


class UserDataRequest(Base):
    __tablename__ = "user_data_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    agent_request_id: Mapped[str | None] = mapped_column(String(128), index=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    user_id: Mapped[str] = mapped_column(String(128), index=True)
    request_text: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    reason_code: Mapped[str | None] = mapped_column(String(128))
    message: Mapped[str | None] = mapped_column(Text)
    result_profile_id: Mapped[str | None] = mapped_column(String(128))
    interpretation_json: Mapped[str | None] = mapped_column(Text)
    issues_json: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserDataRequestResult(Base):
    __tablename__ = "user_data_request_result"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    result_profile_id: Mapped[str | None] = mapped_column(String(128))
    result_mode: Mapped[str | None] = mapped_column(String(64))
    row_count: Mapped[int | None] = mapped_column(Integer)
    truncated: Mapped[bool] = mapped_column(default=False)
    summary_json: Mapped[str | None] = mapped_column(Text)
    columns_json: Mapped[str | None] = mapped_column(Text)
    preview_rows_json: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserDataRequestArtifact(Base):
    __tablename__ = "user_data_request_artifact"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    request_id: Mapped[str] = mapped_column(String(128), index=True)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True)
    artifact_type: Mapped[str | None] = mapped_column(String(64))
    file_name: Mapped[str | None] = mapped_column(String(512))
    content_type: Mapped[str | None] = mapped_column(String(255))
    file_size: Mapped[int | None] = mapped_column(Integer)
    row_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserDataRequestEvent(Base):
    __tablename__ = "user_data_request_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def _load(value: str | None, default: Any) -> Any:
    return default if value is None else json.loads(value)


class RequestProjectionService:
    def __init__(self, config: RequestProjectionConfig) -> None:
        self.config = config
        if config.database_url.startswith("sqlite:///"):
            db_path = config.database_url.removeprefix("sqlite:///")
            if db_path != ":memory:":
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        connect_args = {"check_same_thread": False} if config.database_url.startswith("sqlite") else {}
        self.engine = create_engine(config.database_url, future=True, connect_args=connect_args)
        Base.metadata.create_all(self.engine)
        self._apply_compat_migrations()

    def _apply_compat_migrations(self) -> None:
        """Upgrade existing SQLite projection databases without destructive rebuilds."""
        if not self.config.database_url.startswith("sqlite"):
            return
        db_inspector = inspect(self.engine)
        request_columns = {column["name"] for column in db_inspector.get_columns("user_data_request")}
        artifact_columns = {
            column["name"] for column in db_inspector.get_columns("user_data_request_artifact")
        }
        with self.engine.begin() as connection:
            if "intent" not in request_columns:
                connection.execute(text("ALTER TABLE user_data_request ADD COLUMN intent VARCHAR(64)"))
            if "tenant_id" not in artifact_columns:
                connection.execute(
                    text("ALTER TABLE user_data_request_artifact ADD COLUMN tenant_id VARCHAR(128)")
                )
                connection.execute(text(
                    "UPDATE user_data_request_artifact "
                    "SET tenant_id = (SELECT tenant_id FROM user_data_request "
                    "WHERE user_data_request.request_id = user_data_request_artifact.request_id) "
                    "WHERE tenant_id IS NULL"
                ))
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_user_data_request_intent "
                     "ON user_data_request (intent)")
            )
            connection.execute(
                text("CREATE INDEX IF NOT EXISTS ix_user_data_request_artifact_tenant_id "
                     "ON user_data_request_artifact (tenant_id)")
            )

    def project(
        self,
        *,
        request_id: str,
        agent_request_id: str | None,
        tenant_id: str,
        user_id: str,
        request_text: str,
        response: dict[str, Any],
        intent: str = "SALES_DATA_REQUEST",
    ) -> None:
        now = _now()
        status = str(response.get("status") or "RECEIVED")
        result = response.get("result") if isinstance(response.get("result"), dict) else None
        terminal = status in {"COMPLETED", "FAILED", "BLOCKED"}
        with Session(self.engine) as session:
            item = session.scalar(select(UserDataRequest).where(UserDataRequest.request_id == request_id))
            if item is None:
                item = UserDataRequest(
                    request_id=request_id,
                    agent_request_id=agent_request_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    request_text=request_text,
                    intent=intent,
                    status=status,
                    requested_at=now,
                    started_at=now,
                    created_at=now,
                    updated_at=now,
                )
                session.add(item)
            item.agent_request_id = agent_request_id or item.agent_request_id
            item.intent = intent or item.intent
            item.status = status
            item.reason_code = response.get("reasonCode")
            item.message = response.get("message")
            item.interpretation_json = _dump(response.get("interpretation"))
            item.issues_json = _dump(response.get("issues") or [])
            item.result_profile_id = result.get("profileId") if result else None
            item.completed_at = now if terminal else None
            item.updated_at = now
            if result is not None:
                projected = session.scalar(
                    select(UserDataRequestResult).where(UserDataRequestResult.request_id == request_id)
                )
                if projected is None:
                    projected = UserDataRequestResult(request_id=request_id, created_at=now, updated_at=now)
                    session.add(projected)
                rows = result.get("rows") if isinstance(result.get("rows"), list) else []
                projected.result_profile_id = result.get("profileId")
                projected.result_mode = result.get("resultMode")
                projected.row_count = result.get("rowCount")
                preview_rows = rows[: self.config.preview_row_limit]
                row_count = result.get("rowCount")
                projected.truncated = bool(
                    result.get("truncated", False)
                    or len(rows) > len(preview_rows)
                    or (isinstance(row_count, int) and row_count > len(preview_rows))
                )
                projected.summary_json = _dump(result.get("summary"))
                projected.columns_json = _dump(result.get("columns") or [])
                projected.preview_rows_json = _dump(preview_rows)
                projected.completed_at = now if terminal else None
                projected.updated_at = now
                download = result.get("download") if isinstance(result.get("download"), dict) else None
                if download and download.get("downloadId"):
                    self._upsert_artifact(
                        session, request_id, tenant_id, download, projected.row_count, now
                    )
            artifact = response.get("artifact") if isinstance(response.get("artifact"), dict) else None
            if artifact and artifact.get("artifactId"):
                self._upsert_artifact(
                    session,
                    request_id,
                    tenant_id,
                    artifact,
                    result.get("rowCount") if result else None,
                    now,
                )
            last = session.scalar(
                select(UserDataRequestEvent).where(UserDataRequestEvent.request_id == request_id)
                .order_by(UserDataRequestEvent.id.desc()).limit(1)
            )
            if last is None or last.status != status:
                session.add(UserDataRequestEvent(
                    request_id=request_id,
                    event_type=_EVENT_TYPES.get(status, "STATUS_CHANGED"),
                    status=status,
                    message=_STATUS_MESSAGES.get(status, response.get("message")),
                    occurred_at=now,
                ))
            session.commit()

    @staticmethod
    def _upsert_artifact(
        session: Session,
        request_id: str,
        tenant_id: str,
        raw: dict[str, Any],
        row_count: int | None,
        now: datetime,
    ) -> None:
        artifact_id = str(raw.get("artifactId") or raw.get("downloadId"))
        artifact = session.scalar(select(UserDataRequestArtifact).where(UserDataRequestArtifact.artifact_id == artifact_id))
        if artifact is None:
            artifact = UserDataRequestArtifact(
                artifact_id=artifact_id,
                request_id=request_id,
                tenant_id=tenant_id,
                status="READY",
                created_at=now,
            )
            session.add(artifact)
        artifact.tenant_id = tenant_id
        artifact.artifact_type = raw.get("format") or raw.get("artifactType") or "XLSX"
        artifact.file_name = raw.get("fileName") or raw.get("title")
        artifact.content_type = raw.get("contentType") or raw.get("mediaType")
        artifact.file_size = raw.get("fileSize")
        artifact.row_count = row_count
        artifact.status = str(raw.get("status") or "READY")
        expires = raw.get("expiresAt")
        if expires:
            try:
                artifact.expires_at = datetime.fromisoformat(str(expires))
            except ValueError:
                artifact.expires_at = None

    def mark_artifact_failed(self, artifact_id: str) -> None:
        with Session(self.engine) as session:
            artifact = session.scalar(
                select(UserDataRequestArtifact).where(
                    UserDataRequestArtifact.artifact_id == artifact_id
                )
            )
            if artifact is not None:
                artifact.status = "FAILED"
                session.commit()

    def get_owned(self, request_id: str, tenant_id: str, user_id: str, roles: list[str]) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            item = session.scalar(select(UserDataRequest).where(UserDataRequest.request_id == request_id))
            if item is None or item.tenant_id != tenant_id:
                return None
            if item.user_id != user_id and not set(roles).intersection(self.config.admin_roles):
                return None
            result = session.scalar(select(UserDataRequestResult).where(UserDataRequestResult.request_id == request_id))
            artifacts = session.scalars(select(UserDataRequestArtifact).where(UserDataRequestArtifact.request_id == request_id)).all()
            events = session.scalars(select(UserDataRequestEvent).where(UserDataRequestEvent.request_id == request_id).order_by(UserDataRequestEvent.id)).all()
            return self._detail(item, result, artifacts, events)

    def list_owned(self, *, tenant_id: str, user_id: str, roles: list[str], status: str | None, date_from: datetime | None,
                   date_to: datetime | None, keyword: str | None, page: int, size: int) -> dict[str, Any]:
        with Session(self.engine) as session:
            clauses = [UserDataRequest.tenant_id == tenant_id]
            if not set(roles).intersection(self.config.admin_roles):
                clauses.append(UserDataRequest.user_id == user_id)
            if status:
                clauses.append(UserDataRequest.status == status)
            if date_from:
                clauses.append(UserDataRequest.requested_at >= date_from)
            if date_to:
                clauses.append(UserDataRequest.requested_at <= date_to)
            if keyword:
                clauses.append(or_(UserDataRequest.request_text.contains(keyword), UserDataRequest.request_id.contains(keyword)))
            total = session.scalar(select(func.count()).select_from(UserDataRequest).where(*clauses)) or 0
            rows = session.scalars(select(UserDataRequest).where(*clauses).order_by(UserDataRequest.requested_at.desc())
                                   .offset((page - 1) * size).limit(size)).all()
            items = []
            for item in rows:
                result = session.scalar(select(UserDataRequestResult).where(UserDataRequestResult.request_id == item.request_id))
                artifact = session.scalar(select(UserDataRequestArtifact).where(UserDataRequestArtifact.request_id == item.request_id))
                items.append({
                    "requestId": item.request_id, "requestText": item.request_text, "status": item.status,
                    "resultProfileId": item.result_profile_id,
                    "resultProfile": item.result_profile_id,
                    "rowCount": result.row_count if result else None,
                    "requestedAt": item.requested_at, "completedAt": item.completed_at,
                    "downloadAvailable": bool(artifact and artifact.status == "READY"),
                })
            return {"items": items, "page": page, "size": size, "total": total}

    def artifact_owner(self, artifact_id: str) -> tuple[str, str, str] | None:
        with Session(self.engine) as session:
            artifact = session.scalar(select(UserDataRequestArtifact).where(UserDataRequestArtifact.artifact_id == artifact_id))
            if artifact is None:
                return None
            item = session.scalar(select(UserDataRequest).where(UserDataRequest.request_id == artifact.request_id))
            return (item.request_id, item.tenant_id, item.user_id) if item else None

    @staticmethod
    def _detail(item: UserDataRequest, result: UserDataRequestResult | None, artifacts: list[UserDataRequestArtifact], events: list[UserDataRequestEvent]) -> dict[str, Any]:
        return {
            "requestId": item.request_id, "agentRequestId": item.agent_request_id,
            "tenantId": item.tenant_id, "userId": item.user_id, "intent": item.intent,
            "requestText": item.request_text, "status": item.status, "reasonCode": item.reason_code,
            "message": item.message, "interpretation": _load(item.interpretation_json, None),
            "issues": _load(item.issues_json, []), "requestedAt": item.requested_at,
            "startedAt": item.started_at, "completedAt": item.completed_at,
            "result": None if result is None else {
                "profileId": result.result_profile_id, "resultMode": result.result_mode,
                "rowCount": result.row_count, "truncated": result.truncated,
                "summary": _load(result.summary_json, None), "columns": _load(result.columns_json, []),
                "rows": _load(result.preview_rows_json, []),
            },
            "artifacts": [{
                "artifactId": value.artifact_id, "artifactType": value.artifact_type,
                "fileName": value.file_name, "contentType": value.content_type,
                "fileSize": value.file_size, "rowCount": value.row_count,
                "status": value.status, "expiresAt": value.expires_at,
            } for value in artifacts],
            "events": [{"eventType": value.event_type, "status": value.status,
                        "message": value.message, "occurredAt": value.occurred_at} for value in events],
        }


_SERVICE: RequestProjectionService | None = None
_LOCK = Lock()


def get_request_projection_service() -> RequestProjectionService:
    global _SERVICE
    config = get_settings().request_projection
    with _LOCK:
        if _SERVICE is None or _SERVICE.config.database_url != config.database_url:
            _SERVICE = RequestProjectionService(config)
    return _SERVICE
