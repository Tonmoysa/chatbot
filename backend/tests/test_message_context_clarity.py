"""Context clarification for underspecified short replies (e.g. bare '7 days')."""

import pytest

from chat.constants import INTENT_LEAVE_BALANCE, INTENT_UNKNOWN
from chat.services.message_context_clarity import (
    build_context_clarification_message,
    looks_underspecified_message,
    should_ask_context_clarification,
)
from chat.services.orchestrator import ChatOrchestrator

COMPANY_ID = "company-a"
EMP_ID = "ctx-clarify-emp"


@pytest.mark.parametrize(
    "msg",
    ["7 days", "ha 7 days", "7 din", "১০ দিন"],
)
def test_looks_underspecified_duration_only(msg):
    assert looks_underspecified_message(msg)


@pytest.mark.parametrize(
    "msg",
    [
        "kotodin chuti ache",
        "how many leave days do I have left",
        "7 days leave lagbe kal theke",
        "amar balance koto",
    ],
)
def test_not_underspecified_when_intent_clear(msg):
    assert not looks_underspecified_message(msg)


def test_should_ask_after_ambiguous_chat_not_balance_probe():
    ctx = [
        "User: eid kobe?",
        "Assistant: আপনি কি ঈদের সময় কোথাও যাচ্ছেন? কোম্পানিতে ছুটি দেওয়া হয়েছে?",
        "User: ha 7 days",
    ]
    assert should_ask_context_clarification(
        "ha 7 days",
        ctx,
        intent=INTENT_LEAVE_BALANCE,
        balance_probe=False,
        leave_active=False,
        expense_active=False,
        workflow_continuation=False,
    )


def test_should_not_ask_on_balance_probe():
    assert not should_ask_context_clarification(
        "7 days",
        [],
        intent=INTENT_LEAVE_BALANCE,
        balance_probe=True,
        leave_active=False,
        expense_active=False,
        workflow_continuation=False,
    )


def test_clarification_message_bn_mentions_options():
    msg = build_context_clarification_message("7 days", [], lang="bn")
    assert "৭ দিন" in msg or "7" in msg
    assert "balance" in msg.lower() or "ব্যালান্স" in msg


@pytest.mark.django_db
def test_orchestrator_seven_days_after_eid_chat_asks_clarification_not_balance(
    monkeypatch,
):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        employee_id=EMP_ID,
        session_id="ctx-clarify-7days",
    )
    orch.memory.append(
        session,
        "assistant",
        "আপনি কি ঈদের সময় কোথাও যাচ্ছেন? কোম্পানিতে ছুটি দেওয়া হয়েছে?",
    )
    out = orch.run_chat(
        company_id=COMPANY_ID,
        message="ha 7 days",
        session_id=session.session_id,
        employee_id=EMP_ID,
        trace_id="ctx-7days",
    )
    text = (out.get("response") or {}).get("message") or ""
    assert "12.0" not in text
    assert "approximately" not in text.lower()
    assert out.get("intent") == INTENT_UNKNOWN
    assert "CONTEXT_CLARIFICATION" in (out.get("decision") or {}).get(
        "rules_applied", []
    )
    assert "বিস্তারিত" in text or "প্রসঙ্গ" in text


def test_leave_wizard_date_prompt_does_not_trigger_clarification():
    ctx = [
        "Assistant: **কোন তারিখ(গুলো)** ছুটি চান?\n_(ছুটি আবেদন — নিচে উত্তর দিন)_",
    ]
    assert not should_ask_context_clarification(
        "7 days",
        ctx,
        intent=INTENT_UNKNOWN,
        balance_probe=False,
        leave_active=True,
        expense_active=False,
        workflow_continuation=True,
    )
