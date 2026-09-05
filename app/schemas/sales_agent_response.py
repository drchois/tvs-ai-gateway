from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentContractModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class ResultColumn(AgentContractModel):
    key: str
    label: str
    data_type: Literal["STRING", "INTEGER", "DECIMAL", "DATE", "DATETIME", "BOOLEAN"] = Field(alias="dataType")
    format: str | None = None
    visible: bool = True
    sortable: bool = False
    pii: bool = False
    masked: bool = False
    exportable: bool = True


class ResultSummaryItem(AgentContractModel):
    key: str
    label: str
    value: Any
    data_type: str = Field(alias="dataType")
    format: str | None = None


class ResultDownload(AgentContractModel):
    available: bool = False
    download_id: str | None = Field(default=None, alias="downloadId")
    format: str = "XLSX"
    title: str | None = None
    status: str | None = None
    expires_at: str | None = Field(default=None, alias="expiresAt")
    reason_code: str | None = Field(default=None, alias="reasonCode")
    storage_type: str | None = Field(default=None, alias="storageType", exclude=True)
    bucket_name: str | None = Field(default=None, alias="bucketName", exclude=True)
    object_key: str | None = Field(default=None, alias="objectKey", exclude=True)


class ResultRefinement(AgentContractModel):
    limit: int | None = Field(default=None, ge=0)
    result_count: int | None = Field(default=None, alias="resultCount", ge=0)
    count_exact: bool | None = Field(default=None, alias="countExact")
    suggestions: list[str] = Field(default_factory=list)


class ResultPayload(AgentContractModel):
    profile_id: str = Field(alias="profileId")
    result_mode: str = Field(alias="resultMode")
    summary: dict[str, Any] | None = None
    columns: list[ResultColumn]
    rows: list[dict[str, Any]]
    row_count: int = Field(alias="rowCount", ge=0)
    truncated: bool = False
    deduplication_applied: bool = Field(default=False, alias="deduplicationApplied")
    definitions: list[dict[str, Any]] = Field(default_factory=list)
    presentation: dict[str, Any] | None = None
    download: ResultDownload | None = None


class AgentResponseV2(AgentContractModel):
    request_id: str = Field(alias="requestId", min_length=1)
    status: Literal[
        "RECEIVED", "INTERPRETING", "PLANNING", "READY", "READY_TO_EXECUTE",
        "EXECUTING", "RESULT_PROCESSING", "FILE_GENERATING", "FILE_UPLOADING", "COMPLETED",
        "REQUIRES_CLARIFICATION", "BLOCKED", "FAILED",
    ]
    response_schema_version: Literal["2.0"] = Field(alias="responseSchemaVersion")
    reason_code: str | None = Field(default=None, alias="reasonCode")
    message: str | None = None
    interpretation: dict[str, Any] | None = None
    # FAILED/BLOCKED responses from the live Agent may carry an empty or
    # diagnostic result object rather than a completed tabular payload.
    result: ResultPayload | dict[str, Any] | None = None
    row_estimate: dict[str, Any] | None = Field(default=None, alias="rowEstimate")
    suggested_refinements: list[dict[str, Any]] = Field(default_factory=list, alias="suggestedRefinements")
    refinement: ResultRefinement | None = None
    questions: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    artifact: dict[str, Any] | None = None


class NormalizedAgentResult(AgentContractModel):
    request_id: str | None = Field(default=None, alias="requestId")
    status: str
    response_schema_version: str = Field(alias="responseSchemaVersion")
    reason_code: str | None = Field(default=None, alias="reasonCode")
    message: str | None = None
    interpretation: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    row_estimate: dict[str, Any] | None = Field(default=None, alias="rowEstimate")
    suggested_refinements: list[dict[str, Any]] = Field(default_factory=list, alias="suggestedRefinements")
    refinement: ResultRefinement | None = None
    questions: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    artifact: dict[str, Any] | None = None
    legacy: bool = False
