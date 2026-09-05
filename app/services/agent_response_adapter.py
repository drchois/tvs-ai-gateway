from __future__ import annotations

from typing import Any

from app.schemas.sales_agent_response import AgentResponseV2, NormalizedAgentResult


class UnsupportedAgentSchemaVersion(ValueError):
    pass


def normalize_agent_response(raw: dict[str, Any]) -> NormalizedAgentResult:
    version = raw.get("responseSchemaVersion")
    if version is None:
        return _adapt_v1(raw)
    if str(version) != "2.0":
        raise UnsupportedAgentSchemaVersion(f"Unsupported Agent response schema version: {version}")
    return _adapt_v2(AgentResponseV2.model_validate(raw))


def _adapt_v2(response: AgentResponseV2) -> NormalizedAgentResult:
    # Validation must not cause default metadata to be injected into Agent-owned columns.
    payload = response.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)
    status = "READY_TO_EXECUTE" if response.status == "READY" else response.status
    return NormalizedAgentResult.model_validate({**payload, "status": status, "legacy": False})


def _adapt_v1(raw: dict[str, Any]) -> NormalizedAgentResult:
    # Preserve legacy result dictionaries and insertion order; do not infer columns.
    raw_status = str(raw.get("status") or "FAILED").upper()
    return NormalizedAgentResult.model_validate({
        "requestId": raw.get("requestId"),
        "status": "READY_TO_EXECUTE" if raw_status == "READY" else raw_status,
        "responseSchemaVersion": "1.0",
        "reasonCode": raw.get("reasonCode"),
        "message": raw.get("message"),
        "interpretation": raw.get("interpretation"),
        "result": raw.get("result") if isinstance(raw.get("result"), dict) else None,
        "rowEstimate": raw.get("rowEstimate"),
        "suggestedRefinements": raw.get("suggestedRefinements") or [],
        "refinement": raw.get("refinement") if isinstance(raw.get("refinement"), dict) else None,
        "questions": raw.get("questions") or [],
        "issues": raw.get("issues") or [],
        "artifact": raw.get("artifact") if isinstance(raw.get("artifact"), dict) else None,
        "legacy": True,
    })
