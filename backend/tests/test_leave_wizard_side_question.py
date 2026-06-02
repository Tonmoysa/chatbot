"""Leave wizard: side questions during reason step must not become leave reason."""

from unittest.mock import patch

import pytest

from chat.services.intent_detector import (
    _message_answers_wizard_step,
    looks_like_wizard_side_question,
)
from chat.services.leave_fsm import read_leave_state
from chat.services.orchestrator import ChatOrchestrator
from chat.services.policy_intent_helpers import is_leave_wizard_misroute_complaint
from chat.services.turn_classifier import TURN_CHITCHAT, TURN_SLOT_ANSWER, classify_workflow_turn


def test_looks_like_wizard_side_question_airplane():
    assert looks_like_wizard_side_question("can I use airplane")
    assert looks_like_wizard_side_question("Can I use airplane?")


def test_family_program_not_side_question():
    assert not looks_like_wizard_side_question("family program")
    assert _message_answers_wizard_step("family program", "reason")


def test_reason_step_classifier():
    turn = classify_workflow_turn(
        "can I use airplane",
        leave_active=True,
        expense_active=False,
        pending_leave_step="reason",
    )
    assert turn == TURN_CHITCHAT

    turn_ok = classify_workflow_turn(
        "family program",
        leave_active=True,
        expense_active=False,
        pending_leave_step="reason",
    )
    assert turn_ok == TURN_SLOT_ANSWER


def test_wizard_misroute_complaint_bangla():
    msg = (
        "ami ekta question korchi can i use airplane?"
        "but tumi amake chutir besoyta janai dila keno?"
    )
    assert is_leave_wizard_misroute_complaint(msg)


@pytest.mark.django_db
def test_airplane_question_during_reason_not_saved_as_reason():
    orch = ChatOrchestrator()
    emp = "leave-side-q-pytest"
    sid = None
    trace = "lsq-"

    for i, msg in enumerate(
        (
            "tomorrow I need unpaid leave",
            "unpaid",
            "full day",
            "family program",
        ),
        start=1,
    ):
        pack = orch.run_chat(
            company_id="company-a",
            message=msg,
            session_id=sid,
            employee_id=emp,
            trace_id=f"{trace}{i}",
        )
        sid = pack["_session_id"]

    with patch(
        "chat.services.orchestrator.conversational_reply",
        return_value="ভ্রমণ/ফ্লাইট নিয়ম কোম্পানির পলিসিতে নেই — HR-কে জিজ্ঞেস করুন।",
    ):
        pack = orch.run_chat(
            company_id="company-a",
            message="can I use airplane",
            session_id=sid,
            employee_id=emp,
            trace_id=f"{trace}reason-q",
        )

    body = pack["response"]["message"] or ""
    assert "ভ্রমণ" in body or "HR" in body or "পলিসি" in body.lower()

    session = orch.memory.get_or_create_session(
        company_id="company-a", employee_id=emp, session_id=sid
    )
    draft = read_leave_state(session.workflow_state).get("draft") or {}
    assert draft.get("reason") != "can I use airplane"
    assert "family" in str(draft.get("reason") or "").lower()


@pytest.mark.django_db
def test_misroute_complaint_gets_human_ack_not_robot_greeting():
    orch = ChatOrchestrator()
    emp = "leave-misroute-pytest"
    sid = None

    pack = orch.run_chat(
        company_id="company-a",
        message="kalke chuti lagbe",
        session_id=None,
        employee_id=emp,
        trace_id="lm-1",
    )
    sid = pack["_session_id"]

    msg = (
        "ami ekta question korchi can i use airplane?"
        "but tumi amake chutir besoyta janai dila keno?"
    )
    pack = orch.run_chat(
        company_id="company-a",
        message=msg,
        session_id=sid,
        employee_id=emp,
        trace_id="lm-complaint",
    )
    body = pack["response"]["message"] or ""
    assert "Hi! আমি আপনার HR assistant" not in body
    assert "কারণ" in body or "প্রশ্ন" in body or "leave form" in body.lower()
