from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
from typing import Any

from app.schemas.assistant import ClarificationAnswer


# Korea has used UTC+09:00 without daylight saving time since 1988. A fixed
# offset avoids an optional tzdata package dependency in slim containers.
SEOUL = timezone(timedelta(hours=9), name="Asia/Seoul")


def resolve_relative_period(message: str, *, now: datetime | None = None) -> dict[str, str] | None:
    current = (now or datetime.now(SEOUL)).astimezone(SEOUL)
    today = current.date()
    compact = re.sub(r"\s+", "", message or "")

    last_n = re.search(r"최근(\d+)일", compact)
    if last_n:
        days = max(1, int(last_n.group(1)))
        return _period("LAST_N_DAYS", today - timedelta(days=days - 1), today)
    if "어제" in compact:
        day = today - timedelta(days=1)
        return _period("YESTERDAY", day, day)
    if "지난주" in compact:
        this_monday = today - timedelta(days=today.weekday())
        return _period("LAST_WEEK", this_monday - timedelta(days=7), this_monday - timedelta(days=1))
    if "이번주" in compact:
        monday = today - timedelta(days=today.weekday())
        return _period("THIS_WEEK", monday, monday + timedelta(days=6))
    if "지난달" in compact:
        first_this_month = today.replace(day=1)
        last_previous_month = first_this_month - timedelta(days=1)
        first_previous_month = last_previous_month.replace(day=1)
        return _period("LAST_MONTH", first_previous_month, last_previous_month)
    if "이번달" in compact or "이달" in compact:
        first = today.replace(day=1)
        last = today.replace(day=monthrange(today.year, today.month)[1])
        return _period("THIS_MONTH", first, last)
    if "올해" in compact:
        return _period("THIS_YEAR", today.replace(month=1, day=1), today.replace(month=12, day=31))
    if "오늘" in compact:
        return _period("TODAY", today, today)
    return None


def _period(reference: str, date_from: object, date_to: object) -> dict[str, str]:
    return {
        "reference": reference,
        "expression": reference,
        "from": str(date_from),
        "to": str(date_to),
        "status": "RESOLVED",
        "timezone": "Asia/Seoul",
    }


def build_initial_query_spec(message: str, interpretation: object = None) -> dict[str, Any]:
    query_spec: dict[str, Any] = {}
    if isinstance(interpretation, dict):
        agent_spec = interpretation.get("querySpec")
        if isinstance(agent_spec, dict):
            query_spec.update(agent_spec)
    period = resolve_relative_period(message)
    if period:
        query_spec.setdefault("periods", {})["analysis"] = period
    compact = re.sub(r"\s+", "", message or "")
    if "신규고객" in compact:
        query_spec.setdefault("intent", {
            "type": "DATA_EXTRACTION",
            "requestType": "NEW_CUSTOMER_LIST",
            "description": message,
        })
        query_spec.setdefault("target", {
            "entity": "customer", "scope": "new_customer", "completeness": "EXPLICIT",
        })
        query_spec.setdefault("segments", [{
            "segmentId": "NEW-CUSTOMERS",
            "name": "신규고객",
            "conceptRef": "UNRESOLVED_NEW_CUSTOMER_CONCEPT",
            "parameters": {},
        }])
    elif any(term in compact for term in ("상품", "품목", "제품", "크림")):
        query_spec.setdefault("target", {
            "entity": "product", "scope": "sales", "completeness": "EXPLICIT",
        })
    return query_spec


def extract_clarification(result: dict[str, Any]) -> dict[str, Any] | None:
    raw = result.get("clarification")
    if isinstance(raw, dict):
        return _normalize_prompt(raw, result)
    questions = [item for item in (result.get("questions") or []) if isinstance(item, dict)]
    if not questions:
        return None
    return _normalize_prompt(questions[0], result)


def _normalize_prompt(raw: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    options = []
    for index, option in enumerate(raw.get("options") or []):
        source = option if isinstance(option, dict) else {"label": str(option), "value": str(option)}
        label = str(source.get("label") or source.get("text") or source.get("value") or f"선택 {index + 1}")
        value = _canonical_option(str(source.get("value") or source.get("code") or source.get("id") or label))
        options.append({"label": label, "value": value})
    field = str(raw.get("field") or raw.get("key") or raw.get("questionId") or "").strip()
    combined = " ".join(
        [str(result.get("message") or ""), str(raw.get("message") or raw.get("text") or "")]
        + [item["label"] for item in options]
    )
    if not field or field.startswith("q-") or field == "new-customer-definition":
        if any(term in combined for term in ("신규고객", "신규 고객", "최초 구매", "회원 신규")):
            field = "newCustomerType"
        elif any(term in combined for term in ("기간", "오늘", "이번달", "지난달")):
            field = "period"
    ambiguity_id = str(raw.get("ambiguityId") or raw.get("questionId") or field or "clarification")
    if field == "newCustomerType" and ambiguity_id in {"new-customer-definition", "newCustomerType"}:
        ambiguity_id = "AMB-DET-001"
    return {
        "ambiguityId": ambiguity_id,
        "field": field or "clarification",
        "type": str(raw.get("type") or "SINGLE_SELECT"),
        "options": options,
    }


def resolve_clarification_answer(
    prompt: dict[str, Any],
    message: str,
    explicit: ClarificationAnswer | None,
) -> dict[str, str] | None:
    field = str(prompt.get("field") or "clarification")
    options = [item for item in (prompt.get("options") or []) if isinstance(item, dict)]
    ambiguity_id = str(prompt.get("ambiguityId") or field)
    if explicit is not None:
        explicit_id = explicit.ambiguity_id or explicit.field
        explicit_value = explicit.selected_option or explicit.value
        if explicit_id not in {ambiguity_id, field}:
            return None
        allowed_values = {str(item.get("value") or "") for item in options}
        canonical_value = _canonical_option(str(explicit_value or ""))
        if allowed_values and canonical_value not in allowed_values:
            return None
        label = next((str(item.get("label") or "") for item in options if str(item.get("value")) == canonical_value), message)
        return {"ambiguityId": ambiguity_id, "field": field, "value": canonical_value, "label": label}

    normalized = _normalize_text(message)
    for option in options:
        value = str(option.get("value") or "")
        label = str(option.get("label") or value)
        if normalized in {_normalize_text(value), _normalize_text(label)} or (
            normalized and _normalize_text(label) and _normalize_text(label) in normalized
        ):
            return {"ambiguityId": ambiguity_id, "field": field, "value": value, "label": label}

    aliases = {
        "JOIN_NEW_CUSTOMER": ("회원가입기준으로", "가입한고객", "신규가입자", "회원신규가입고객"),
        "FIRST_PURCHASE_CUSTOMER": ("처음구매한고객", "최초구매자", "구매이력이없던고객", "전체최초구매고객"),
    }
    for value, phrases in aliases.items():
        if normalized in phrases or any(phrase in normalized for phrase in phrases):
            return {"ambiguityId": ambiguity_id, "field": field, "value": value, "label": message.strip()}

    period = resolve_relative_period(message)
    if field.lower() in {"period", "date", "analysisperiod"} and period:
        return {"ambiguityId": ambiguity_id, "field": field, "value": period["reference"], "label": message.strip()}
    return None


def merge_query_spec(query_spec: dict[str, Any], answer: dict[str, str]) -> dict[str, Any]:
    merged = deepcopy(query_spec)
    answers = dict(merged.get("answers") or {})
    answers[answer["field"]] = answer["value"]
    merged["answers"] = answers
    ambiguity_id = answer.get("ambiguityId") or answer["field"]
    open_ambiguities = []
    resolved_ambiguities = list(merged.get("resolvedAmbiguities") or [])
    matched = False
    for raw in merged.get("ambiguities") or []:
        ambiguity = dict(raw) if isinstance(raw, dict) else {}
        current_id = str(ambiguity.get("ambiguityId") or ambiguity.get("id") or "")
        if current_id == ambiguity_id:
            ambiguity.update({
                "ambiguityId": ambiguity_id,
                "status": "RESOLVED",
                "selectedOption": answer["value"],
                "blocking": False,
                "clarificationRequired": False,
            })
            resolved_ambiguities.append(ambiguity)
            matched = True
        else:
            open_ambiguities.append(ambiguity)
    if not matched:
        resolved_ambiguities.append({
            "ambiguityId": ambiguity_id,
            "status": "RESOLVED",
            "selectedOption": answer["value"],
            "blocking": False,
            "clarificationRequired": False,
        })
    merged["ambiguities"] = open_ambiguities
    merged["resolvedAmbiguities"] = resolved_ambiguities

    concept_values = {
        "JOIN_NEW_CUSTOMER",
        "FIRST_PURCHASE_CUSTOMER",
        "BRAND_FIRST_PURCHASE_CUSTOMER",
        "PRODUCT_FIRST_PURCHASE_CUSTOMER",
    }
    if answer["value"] in concept_values:
        for segment in merged.get("segments") or []:
            if isinstance(segment, dict) and segment.get("conceptRef") == "UNRESOLVED_NEW_CUSTOMER_CONCEPT":
                segment["conceptRef"] = answer["value"]
                segment["name"] = answer.get("label") or segment.get("name")
    if answer["field"].lower() in {"period", "date", "analysisperiod"}:
        period = resolve_relative_period(answer.get("label") or "")
        if period:
            periods = dict(merged.get("periods") or {})
            periods["analysis"] = period
            merged["periods"] = periods
    merged["queryExecutable"] = validate_query_spec(merged)
    return merged


def validate_query_spec(query_spec: dict[str, Any]) -> bool:
    ambiguities = query_spec.get("ambiguities") or []
    has_open_blocker = any(
        isinstance(item, dict)
        and str(item.get("status") or "OPEN").upper() == "OPEN"
        and bool(item.get("blocking", True))
        for item in ambiguities
    )
    analysis = (query_spec.get("periods") or {}).get("analysis") or {}
    target = query_spec.get("target") or {}
    segments = query_spec.get("segments") or []
    unresolved_concept = any(
        isinstance(item, dict) and str(item.get("conceptRef") or "").startswith("UNRESOLVED_")
        for item in segments
    )
    period_resolved = bool(analysis.get("from") and analysis.get("to")) and str(analysis.get("status") or "RESOLVED") == "RESOLVED"
    target_resolved = bool(target.get("entity"))
    return not has_open_blocker and period_resolved and target_resolved and not unresolved_concept


def add_pending_ambiguity(query_spec: dict[str, Any], prompt: dict[str, Any]) -> dict[str, Any]:
    enriched = deepcopy(query_spec)
    ambiguity_id = str(prompt.get("ambiguityId") or prompt.get("field") or "clarification")
    ambiguities = [dict(item) for item in enriched.get("ambiguities") or [] if isinstance(item, dict)]
    if not any(str(item.get("ambiguityId") or item.get("id") or "") == ambiguity_id for item in ambiguities):
        ambiguities.append({
            "ambiguityId": ambiguity_id,
            "status": "OPEN",
            "blocking": True,
            "clarificationRequired": True,
        })
    enriched["ambiguities"] = ambiguities
    enriched["queryExecutable"] = False
    return enriched


def _canonical_option(value: str) -> str:
    return {
        "MEMBER_SIGNUP": "JOIN_NEW_CUSTOMER",
        "FIRST_PURCHASE": "FIRST_PURCHASE_CUSTOMER",
        "BRAND_FIRST": "BRAND_FIRST_PURCHASE_CUSTOMER",
        "PRODUCT_FIRST": "PRODUCT_FIRST_PURCHASE_CUSTOMER",
    }.get(value, value)


def _normalize_text(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", (value or "").lower())
