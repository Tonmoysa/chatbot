"""HR query LLM fallback during active wizard side questions."""

import pytest

from chat.constants import INTENT_EXPENSE_DAY_SUMMARY
from chat.services.hr_query_classifier import (
    HrQueryDecision,
    QUERY_EXPENSE_DAY_SUMMARY,
    QUERY_UNKNOWN,
    classify_hr_query,
    hr_query_llm_allowed_during_wizard,
)


def test_hr_query_llm_allowed_for_expense_recap():
    assert hr_query_llm_allowed_during_wizard("amar expense koto ajke?")


def test_hr_query_llm_allowed_for_leave_submit():
    assert hr_query_llm_allowed_during_wizard("leave request ta submit koro")


def test_hr_query_llm_classifies_novel_recap_during_wizard(monkeypatch):
    monkeypatch.setattr(
        "chat.services.hr_query_classifier.rules_classify_hr_query",
        lambda *a, **k: HrQueryDecision(
            query_kind=QUERY_UNKNOWN,
            confidence=0.4,
            source="rules_test_miss",
            in_hr_scope=True,
        ),
    )
    monkeypatch.setattr(
        "chat.services.hr_query_classifier.LLMClient.is_configured",
        lambda self: True,
    )
    monkeypatch.setattr(
        "chat.services.hr_query_classifier.LLMClient.chat_json",
        lambda self, **kwargs: {
            "query_kind": "expense_day_summary",
            "date_reference": "today",
            "confidence": 0.9,
            "in_hr_scope": True,
        },
    )
    decision = classify_hr_query(
        "ajke ami koto taka kharcha korechi?",
        trace_id="hr-wiz-llm",
        use_llm=True,
        wizard_side_llm=True,
    )
    assert decision.query_kind == QUERY_EXPENSE_DAY_SUMMARY
    assert decision.maps_to_intent == INTENT_EXPENSE_DAY_SUMMARY
    assert decision.source == "llm"
