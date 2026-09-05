from __future__ import annotations

from app.schemas.assistant import AssistantContext, BusinessIntent


_SALES_DOMAIN_TERMS = (
    "판매",
    "판매현황",
    "매출",
    "매출액",
    "판매량",
    "판매금액",
    "구매",
    "구매고객",
    "구매 고객",
    "고객",
    "고객목록",
    "고객 목록",
    "고객명단",
    "고객 명단",
    "신규고객",
    "신규 고객",
    "휴면고객",
    "휴면 고객",
    "재구매",
    "상품",
    "제품",
    "브랜드",
    "매장",
    "직원",
    "사원",
    "실적",
    "순위",
)

_DATA_REQUEST_TERMS = (
    "조회",
    "현황",
    "보여줘",
    "찾아줘",
    "뽑아줘",
    "추출",
    "확인",
    "계산",
    "비교",
    "리스트",
    # A domain term must also be present, so a general "알려줘" remains chat.
    "알려줘",
)


def classify_business_intent(message: str, context: AssistantContext) -> BusinessIntent:
    """Route only at a high level; never interpret or generate sales SQL here."""
    text = " ".join((message or "").lower().split())
    menu = (context.current_menu or "").lower()
    if any(word in text for word in ("crm", "고객관계", "고객 관계")):
        return BusinessIntent.CRM_INTELLIGENCE
    if any(word in text for word in ("인사이트", "추세", "트렌드", "매출 하락 원인", "매출 분석")):
        return BusinessIntent.SALES_INSIGHT
    if any(word in text for word in ("정책", "규정", "지침", "산출 기준")):
        return BusinessIntent.POLICY_INQUIRY
    if any(word in text for word in ("문서", "매뉴얼", "rag", "검색해줘", "근거")):
        return BusinessIntent.RAG_QA
    has_sales_domain = any(term in text for term in _SALES_DOMAIN_TERMS)
    has_data_request = any(term in text for term in _DATA_REQUEST_TERMS)
    if has_sales_domain and (has_data_request or "sales" in menu):
        return BusinessIntent.SALES_DATA_REQUEST
    if any(word in text for word in ("ppt", "엑셀", "excel", "pdf", "다운로드", "파일로")):
        return BusinessIntent.ARTIFACT_ACTION
    if any(word in text for word in ("안녕", "도와줘", "무엇", "설명", "알려줘", "궁금")):
        return BusinessIntent.CHAT
    return BusinessIntent.UNKNOWN
