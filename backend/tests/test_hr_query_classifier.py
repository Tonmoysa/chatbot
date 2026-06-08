"""Unified HR query classifier — rules + LLM fallback."""

import datetime as dt

import pytest

from chat.constants import (
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_EXPENSE_STATUS,
    INTENT_LEAVE_REQUEST,
    INTENT_UNKNOWN,
)
from chat.services.hr_query_classifier import (
    CONFIDENCE_RULES,
    HrQueryContext,
    QUERY_EXPENSE_META,
    QUERY_EXPENSE_RECALL,
    apply_hr_query_to_intent,
    classify_hr_query,
    clear_hr_query_cache,
    decision_suppresses_out_of_scope,
    rules_classify_hr_query,
)
from chat.services.orchestrator import ChatOrchestrator
from chat.services.policy_intent_helpers import is_off_topic_for_hr_assistant

COST_ADD_BN = "কত কস্ট এড করছি আমি"
RECALL_BN = "লাস্ট দিনে কোন এক্সপেন্স দিছিলাম আমি"
NOVEL_RECALL_BN = "shoptaho age je kharcha ta chilo setar breakdown ta ki chilo"


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_hr_query_cache()
    yield
    clear_hr_query_cache()


def test_rules_expense_meta_bengali() -> None:
    d = rules_classify_hr_query(COST_ADD_BN)
    assert d.query_kind == QUERY_EXPENSE_META
    assert d.confidence >= CONFIDENCE_RULES
    assert d.maps_to_intent == INTENT_EXPENSE_STATUS
    assert decision_suppresses_out_of_scope(d)


def test_rules_expense_recall_bengali() -> None:
    d = rules_classify_hr_query(RECALL_BN)
    assert d.query_kind == QUERY_EXPENSE_RECALL
    assert d.date_reference == "yesterday"
    assert d.maps_to_intent == INTENT_EXPENSE_DAY_SUMMARY
    assert d.date_iso(today=dt.date(2026, 6, 8)) == "2026-06-07"


def test_rules_recall_not_out_of_scope() -> None:
    assert not is_off_topic_for_hr_assistant(RECALL_BN)


def test_wizard_confirm_yes_not_chitchat() -> None:
    ctx = HrQueryContext(expense_active=True, expense_stage="submit_confirm")
    d = rules_classify_hr_query("yes", context=ctx)
    assert d.query_kind != "chitchat"


def test_apply_upgrades_unknown_intent() -> None:
    d = rules_classify_hr_query(RECALL_BN)
    intent, result = apply_hr_query_to_intent(
        INTENT_UNKNOWN, {"confidence": 0.3}, d, message=RECALL_BN
    )
    assert intent == INTENT_EXPENSE_DAY_SUMMARY
    assert "hr_query" in result.get("source", "")


def test_llm_fallback_novel_recall_phrase(monkeypatch) -> None:
    monkeypatch.setattr(
        "chat.services.hr_query_classifier.LLMClient.is_configured",
        lambda self: True,
    )
    monkeypatch.setattr(
        "chat.services.hr_query_classifier.LLMClient.chat_json",
        lambda self, **kwargs: {
            "query_kind": "expense_recall",
            "date_reference": "yesterday",
            "confidence": 0.88,
            "in_hr_scope": True,
        },
    )
    rules = rules_classify_hr_query(NOVEL_RECALL_BN)
    assert rules.query_kind != QUERY_EXPENSE_RECALL
    d = classify_hr_query(NOVEL_RECALL_BN, trace_id="novel-recall", use_llm=True)
    assert d.query_kind == QUERY_EXPENSE_RECALL
    assert d.source == "llm"
    assert d.maps_to_intent == INTENT_EXPENSE_DAY_SUMMARY
    assert decision_suppresses_out_of_scope(d)


@pytest.mark.django_db
def test_orchestrator_novel_recall_via_llm_not_oos(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.hr_query_classifier.LLMClient.is_configured",
        lambda self: True,
    )
    monkeypatch.setattr(
        "chat.services.hr_query_classifier.LLMClient.chat_json",
        lambda self, **kwargs: {
            "query_kind": "expense_recall",
            "date_reference": "yesterday",
            "confidence": 0.9,
            "in_hr_scope": True,
        },
    )
    monkeypatch.setattr(
        "chat.services.expense.session_ledger.infer_session_expense_summary_date",
        lambda *a, **k: "2026-06-07",
    )

    orch = ChatOrchestrator()
    r = orch.run_chat(
        company_id="company-a",
        message=NOVEL_RECALL_BN,
        session_id=None,
        employee_id="hr-query-novel",
        trace_id="hr-query-novel-1",
    )
    msg = r["response"]["message"] or ""
    assert r["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    assert "HR বটের কাজ নয়" not in msg
    assert "স্কোপের বাইরে" not in msg
