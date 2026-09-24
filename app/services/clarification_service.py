from __future__ import annotations

from calendar import monthrange
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
    return {"reference": reference, "from": str(date_from), "to": str(date_to)}


def build_initial_query_spec(message: str, interpretation: object = None) -> dict[str, Any]:
    query_spec: dict[str, Any] = {}
    if isinstance(interpretation, dict):
        agent_spec = interpretation.get("querySpec")
        if isinstance(agent_spec, dict):
            query_spec.update(agent_spec)
    period = resolve_relative_period(message)
    if period:
        query_spec.setdefault("periods", {})["analysis"] = period
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
        value = str(source.get("value") or source.get("code") or source.get("id") or label)
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
    return {"field": field or "clarification", "type": str(raw.get("type") or "SINGLE_SELECT"), "options": options}


def resolve_clarification_answer(
    prompt: dict[str, Any],
    message: str,
    explicit: ClarificationAnswer | None,
) -> dict[str, str] | None:
    field = str(prompt.get("field") or "clarification")
    options = [item for item in (prompt.get("options") or []) if isinstance(item, dict)]
    if explicit is not None:
        if explicit.field != field:
            return None
        allowed_values = {str(item.get("value") or "") for item in options}
        if allowed_values and explicit.value not in allowed_values:
            return None
        label = next((str(item.get("label") or "") for item in options if str(item.get("value")) == explicit.value), message)
        return {"field": field, "value": explicit.value, "label": label}

    normalized = _normalize_text(message)
    for option in options:
        value = str(option.get("value") or "")
        label = str(option.get("label") or value)
        if normalized in {_normalize_text(value), _normalize_text(label)} or (
            normalized and _normalize_text(label) and _normalize_text(label) in normalized
        ):
            return {"field": field, "value": value, "label": label}

    aliases = {
        "MEMBER_SIGNUP": ("회원가입기준으로", "가입한고객", "신규가입자", "회원신규가입고객"),
        "FIRST_PURCHASE": ("처음구매한고객", "최초구매자", "구매이력이없던고객", "전체최초구매고객"),
    }
    for value, phrases in aliases.items():
        if normalized in phrases or any(phrase in normalized for phrase in phrases):
            return {"field": field, "value": value, "label": message.strip()}

    period = resolve_relative_period(message)
    if field.lower() in {"period", "date", "analysisperiod"} and period:
        return {"field": field, "value": period["reference"], "label": message.strip()}
    return None


def merge_query_spec(query_spec: dict[str, Any], answer: dict[str, str]) -> dict[str, Any]:
    merged = dict(query_spec)
    answers = dict(merged.get("answers") or {})
    answers[answer["field"]] = answer["value"]
    merged["answers"] = answers
    if answer["field"].lower() in {"period", "date", "analysisperiod"}:
        period = resolve_relative_period(answer.get("label") or "")
        if period:
            periods = dict(merged.get("periods") or {})
            periods["analysis"] = period
            merged["periods"] = periods
    return merged


def build_merged_message(original_message: str, answer: dict[str, str]) -> str:
    return (
        f"{original_message.strip()}\n"
        f"추가 조건: {answer['label']} ({answer['field']}={answer['value']})"
    )


def _normalize_text(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", (value or "").lower())
