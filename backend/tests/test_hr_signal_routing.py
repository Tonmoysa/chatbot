"""HR-signal gates, short status queries, and P99 hr_query router fallback."""

import pytest

from chat.constants import (
    INTENT_APPROVAL_ESCALATION,
    INTENT_EXPENSE_STATUS,
    INTENT_LEAVE_BALANCE,
)
from chat.services.expense.session_action_memory import wants_expense_meta_question
from chat.services.hr_query_classifier import (
    CONFIDENCE_RULES,
    QUERY_APPROVAL_ESCALATION,
    rules_classify_hr_query,
)
from chat.services.hr_signal import (
    message_has_hr_signal,
    message_looks_like_expense_status_query,
    should_try_utterance_llm,
)
from chat.services.intent_detector import _strong_expense_claim
from chat.services.session_turn_bridge import (
    apply_hr_query_router_fallback,
    intent_result_from_router_decision,
    router_is_fallback,
)
from chat.services.session_turn_router import TurnKind, _decision


def _empty_snapshot(message: str):
    class _Snap:
        leave_active = False
        expense_active = False
        expense_domain_active = False
        leave_domain_active = False
        has_pending_prompt = False

    return _Snap()


@pytest.mark.parametrize(
    "message",
    [
        "reimbursement ta koi?",
        "expense koi?",
        "claim ta?",
    ],
)
def test_short_expense_status_detected_as_meta(message):
    assert message_looks_like_expense_status_query(message)
    assert wants_expense_meta_question(message)
    assert not _strong_expense_claim(message)


def test_expense_claim_with_amount_still_claim():
    assert _strong_expense_claim("lunch 100 taka")
    assert not message_looks_like_expense_status_query("lunch 100 taka")


def test_short_hr_signal_triggers_utterance_llm_gate():
    snap = _empty_snapshot("reimbursement ta koi?")
    assert message_has_hr_signal("reimbursement ta koi?")
    assert should_try_utterance_llm("reimbursement ta koi?", snap)


def test_pure_chitchat_skips_utterance_llm_gate():
    snap = _empty_snapshot("hi")
    assert not should_try_utterance_llm("hi", snap)


def test_leave_baki_has_hr_signal():
    assert message_has_hr_signal("leave baki?")


def test_approval_query_classified():
    decision = rules_classify_hr_query("manager approve?")
    assert decision.query_kind == QUERY_APPROVAL_ESCALATION
    assert decision.maps_to_intent == INTENT_APPROVAL_ESCALATION
    assert decision.confidence >= CONFIDENCE_RULES


def test_hr_query_fallback_promotes_p99_expense_status():
    p99 = _decision(
        turn_kind=TurnKind.UNKNOWN,
        intent=None,
        target_workflow=None,
        handler_id="global_intent",
        reason="P99_no_match",
        confidence=0.0,
    )
    assert router_is_fallback(p99)
    from chat.services.hr_query_classifier import HrQueryDecision, QUERY_EXPENSE_META

    hr = HrQueryDecision(
        query_kind=QUERY_EXPENSE_META,
        confidence=0.95,
        source="rules_expense_meta",
        in_hr_scope=True,
        maps_to_intent=INTENT_EXPENSE_STATUS,
    )
    patched = apply_hr_query_router_fallback(p99, hr)
    assert patched.turn_kind == TurnKind.META_QUESTION
    assert patched.intent == INTENT_EXPENSE_STATUS
    assert patched.reason == "hr_query_fallback_expense_meta"

    intent_result = intent_result_from_router_decision(patched)
    assert intent_result["intent"] == INTENT_EXPENSE_STATUS
    assert "hr_query_fallback" in intent_result["source"]


def test_hr_query_fallback_promotes_p99_approval():
    p99 = _decision(
        turn_kind=TurnKind.UNKNOWN,
        intent=None,
        target_workflow=None,
        handler_id="global_intent",
        reason="P99_no_match",
        confidence=0.0,
    )
    hr = rules_classify_hr_query("manager approve?")
    patched = apply_hr_query_router_fallback(p99, hr)
    assert patched.intent == INTENT_APPROVAL_ESCALATION
    assert patched.turn_kind == TurnKind.META_QUESTION


def test_hr_query_fallback_ignores_low_confidence_unknown():
    p99 = _decision(
        turn_kind=TurnKind.UNKNOWN,
        intent=None,
        target_workflow=None,
        handler_id="global_intent",
        reason="P99_no_match",
        confidence=0.0,
    )
    from chat.services.hr_query_classifier import HrQueryDecision, QUERY_UNKNOWN

    hr = HrQueryDecision(
        query_kind=QUERY_UNKNOWN,
        confidence=0.3,
        source="rules",
        in_hr_scope=False,
        maps_to_intent=None,
    )
    assert apply_hr_query_router_fallback(p99, hr) is p99


def test_leave_baki_not_unknown_via_rules():
    hr = rules_classify_hr_query("leave baki?")
    assert hr.maps_to_intent == INTENT_LEAVE_BALANCE
