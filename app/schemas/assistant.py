from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BusinessIntent(StrEnum):
    CHAT = "CHAT"
    RAG_QA = "RAG_QA"
    SALES_DATA_REQUEST = "SALES_DATA_REQUEST"
    SALES_INSIGHT = "SALES_INSIGHT"
    ARTIFACT_ACTION = "ARTIFACT_ACTION"
    CRM_INTELLIGENCE = "CRM_INTELLIGENCE"
    POLICY_INQUIRY = "POLICY_INQUIRY"
    UNKNOWN = "UNKNOWN"


class AssistantContext(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    user_id: str | None = Field(default=None, alias="userId")
    roles: list[str] = Field(default_factory=list)
    store_id: str | None = Field(default=None, alias="storeId")
    current_menu: str | None = Field(default=None, alias="currentMenu")
    tenant_id: str = Field(default="default", alias="tenantId")


class SalesAgentExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    allow_execute: bool = Field(default=False, alias="allowExecute")
    allow_pii: bool = Field(default=False, alias="allowPii")


class AssistantMessageRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    message: str = Field(min_length=1)
    # Accept both the direct Gateway contract and the existing BFF context contract.
    tenant_id: str | None = Field(default=None, alias="tenantId", min_length=1)
    session_id: str | None = Field(default=None, alias="sessionId")
    context: AssistantContext = Field(default_factory=AssistantContext)
    execution: SalesAgentExecution = Field(default_factory=SalesAgentExecution)
    options: dict[str, Any] = Field(default_factory=dict)
    action: dict[str, Any] | None = None


class SalesAgentRequest(BaseModel):
    """Outbound contract for TV Sales Agent POST /api/assistant/message."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tenant_id: str = Field(alias="tenantId", min_length=1)
    message: str = Field(min_length=1)
    execution: SalesAgentExecution = Field(default_factory=SalesAgentExecution)


class AssistantAction(BaseModel):
    type: str
    label: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AssistantMessageResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    request_id: str = Field(alias="requestId")
    session_id: str = Field(alias="sessionId")
    intent: BusinessIntent
    status: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    actions: list[AssistantAction] = Field(default_factory=list)
