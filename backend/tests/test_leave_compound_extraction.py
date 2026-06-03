"""Compound leave messages: extract date, payment, scope, and reason together."""

from unittest.mock import patch

import pytest

from chat.services.intent_detector import looks_like_wizard_side_question
from chat.services.leave_fsm import read_leave_state
from chat.services.leave_slot_extraction import (
    extract_reason_from_message,
    extract_leave_slots,
    is_payment_only_message,
)
from chat.services.leave_slots import get_missing_slots, prefill_draft_from_extraction
from chat.services.leave_workflow import _apply_slots_from_message, process_leave_turn
from chat.services.orchestrator import ChatOrchestrator

USER_MSG = (
    "tomorrow I need a leave for my family program that live will be unpaid"
)


def _draft_from(msg: str, entities: dict | None = None) -> dict:
    draft: dict = {}
    _apply_slots_from_message(draft, msg, entities or {})
    return draft


BN_FAMILY_LEAVE = (
    "tomarrow ami familly er jonno bahire jabo...tai amar full day chuti lagbe..and paid hobe"
)


@pytest.mark.parametrize(
    "message,expected_reason",
    [
        (USER_MSG, "family program"),
        (BN_FAMILY_LEAVE, "family"),
        ("kalke chuti lagbe family wedding er jonno unpaid", "wedding"),
        ("need leave tomorrow because of relative death unpaid", "relative death"),
        ("family program", "family program"),
        ("fever and headache", "fever and headache"),
    ],
)
def test_extract_reason_from_message(message: str, expected_reason: str):
    reason = extract_reason_from_message(message)
    assert reason
    assert expected_reason.lower() in reason.lower()


@pytest.mark.parametrize(
    "message",
    [
        "can i use train?",
        "can I use airplane",
        "ami ki train e jete pari?",
    ],
)
def test_travel_questions_are_not_reasons(message: str):
    assert extract_reason_from_message(message) is None
    assert looks_like_wizard_side_question(message)


def test_banglish_family_jonno_prefills_reason_and_skips_reason_slot():
    draft = _draft_from(BN_FAMILY_LEAVE)
    assert draft.get("start_date")
    assert draft.get("leave_payment_category") == "paid"
    assert "family" in str(draft.get("reason") or "").lower()
    missing = get_missing_slots(draft)
    assert "reason" not in missing


def test_compound_user_message_prefills_date_payment_reason():
    draft = _draft_from(USER_MSG)
    assert draft.get("start_date")
    assert draft.get("leave_payment_category") == "lwop"
    assert "family" in str(draft.get("reason") or "").lower()
    missing = get_missing_slots(draft)
    assert "leave_dates" not in missing
    assert "reason" not in missing
    assert "leave_payment_category" not in missing
    assert "day_scope" in missing


@pytest.mark.django_db
def test_orchestrator_first_turn_asks_scope_not_date_or_reason(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    pack = orch.run_chat(
        company_id="company-a",
        message=USER_MSG,
        session_id=None,
        employee_id="compound-leave-pytest",
        trace_id="cl-1",
    )
    body = pack["response"]["message"] or ""
    assert "কোন তারিখ" not in body
    assert "Reason টা" not in body
    assert "Full Day" in body or "Half Day" in body or "full" in body.lower()

    session = orch.memory.get_or_create_session(
        company_id="company-a",
        employee_id="compound-leave-pytest",
        session_id=pack["_session_id"],
    )
    draft = read_leave_state(session.workflow_state).get("draft") or {}
    assert draft.get("start_date")
    assert "family" in str(draft.get("reason") or "").lower()


@pytest.mark.django_db
def test_full_flow_after_compound_start_skips_reason_question(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    emp = "compound-flow-pytest"
    sid = None

    pack = orch.run_chat(
        company_id="company-a",
        message=USER_MSG,
        session_id=None,
        employee_id=emp,
        trace_id="cl-flow-1",
    )
    sid = pack["_session_id"]

    pack = orch.run_chat(
        company_id="company-a",
        message="full day",
        session_id=sid,
        employee_id=emp,
        trace_id="cl-flow-2",
    )
    body = pack["response"]["message"] or ""
    assert "Reason টা" not in body, body
    assert "পর্যালোচনা" in body or "জমা দেবেন" in body


@pytest.mark.django_db
def test_train_question_at_reason_step_is_side_interrupt(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    with patch(
        "chat.services.orchestrator.conversational_reply",
        return_value="ট্রেন/যাতায়াত নিয়ম পলিসিতে নেই — HR-কে জিজ্ঞেস করুন।",
    ):
        orch = ChatOrchestrator()
        emp = "train-side-q-pytest"
        wf = {
            "active_flow": "leave",
            "status": "active",
            "step": "reason",
            "draft": {
                "start_date": "2026-06-03",
                "end_date": "2026-06-03",
                "leave_payment_category": "lwop",
                "day_scope": "full",
            },
        }
        session = orch.memory.get_or_create_session(
            company_id="company-a",
            employee_id=emp,
            session_id="train-side-session",
        )
        session.workflow_state = wf
        session.save(update_fields=["workflow_state", "updated_at"])

        pack = orch.run_chat(
            company_id="company-a",
            message="can i use train?",
            session_id=session.session_id,
            employee_id=emp,
            trace_id="cl-train",
        )
    body = pack["response"]["message"] or ""
    assert "পর্যালোচনা" not in body
    assert "ট্রেন" in body or "HR" in body
    session.refresh_from_db()
    draft = read_leave_state(session.workflow_state).get("draft") or {}
    assert draft.get("reason") != "can i use train?"
