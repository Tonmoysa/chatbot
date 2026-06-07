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
    assert (
        "ভ্রমণ" in body
        or "HR" in body
        or "পলিসি" in body.lower()
        or "general knowledge" in body.lower()
        or "বাইরে" in body
        or "HR" in body
    )

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


@pytest.mark.django_db
def test_python_ki_during_leave_review_declines_without_full_resume(monkeypatch):
    """General-knowledge side questions must not reopen the leave review screen."""
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.orchestrator.conversational_reply",
        lambda **_k: "পাইথন একটি প্রোগ্রামিং ভাষা।",
    )
    orch = ChatOrchestrator()
    emp = "leave-python-interrupt"
    sid = None
    trace = "lpy-"
    for i, msg in enumerate(
        (
            "tomorrow I need paid leave",
            "paid",
            "full day",
            "familly program e jabo",
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

    pack = orch.run_chat(
        company_id="company-a",
        message="python ki?",
        session_id=sid,
        employee_id=emp,
        trace_id=f"{trace}gk",
    )
    body = pack["response"]["message"] or ""
    assert "পাইথন" not in body
    assert "প্রোগ্রামিং" not in body
    assert "জমা দেবেন" not in body
    assert "পর্যালোচনা" not in body
    assert (
        "general knowledge" in body.lower()
        or "বাইরে" in body
        or "HR" in body
        or "scope" in body.lower()
    )
    assert "draft" not in body.lower()
    assert "সংরক্ষিত" not in body

    session = orch.memory.get_or_create_session(
        company_id="company-a", employee_id=emp, session_id=sid
    )
    from chat.services.leave_workflow import is_leave_paused

    assert is_leave_paused(session.workflow_state)
    draft = read_leave_state(session.workflow_state).get("draft") or {}
    assert "familly" in str(draft.get("reason") or "").lower()


@pytest.mark.django_db
def test_weather_statement_at_leave_review_does_not_mutate_draft(monkeypatch):
    """Casual weather talk without '?' must not overwrite leave review draft."""
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    emp = "leave-weather-interrupt"
    sid = None
    trace = "lwx-"
    for i, msg in enumerate(
        (
            "tomorrow I need paid leave",
            "paid",
            "full day",
            "amar paye betha onek tai",
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

    session = orch.memory.get_or_create_session(
        company_id="company-a", employee_id=emp, session_id=sid
    )
    draft_before = dict(read_leave_state(session.workflow_state).get("draft") or {})
    from chat.services.leave_fsm import is_awaiting_leave_confirmation

    assert is_awaiting_leave_confirmation(session.workflow_state)
    assert draft_before.get("start_date")
    reason_before = str(draft_before.get("reason") or "").lower()
    assert "betha" in reason_before or "paye" in reason_before

    for weather_msg in ("ajke onek gorom porche", "ajke onek gorom"):
        pack = orch.run_chat(
            company_id="company-a",
            message=weather_msg,
            session_id=sid,
            employee_id=emp,
            trace_id=f"{trace}{weather_msg[:8]}",
        )
        body = pack["response"]["message"] or ""
        assert "জমা দেবেন" not in body
        assert "পর্যালোচনা" not in body or "বাইরে" in body
        assert (
            "বাইরে" in body
            or "HR" in body
            or "scope" in body.lower()
            or "general knowledge" in body.lower()
        )
        session.refresh_from_db()
        draft_after = dict(read_leave_state(session.workflow_state).get("draft") or {})
        assert draft_after.get("start_date") == draft_before.get("start_date")
        assert "gorom" not in str(draft_after.get("reason") or "").lower()


@pytest.mark.django_db
def test_leave_back_phrase_at_review_does_not_overwrite_reason(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    emp = "leave-back-nav-pytest"
    sid = None
    for i, msg in enumerate(
        (
            "tomorrow I need paid leave",
            "paid",
            "full day",
            "amar paye betha onek tai",
        ),
        start=1,
    ):
        pack = orch.run_chat(
            company_id="company-a",
            message=msg,
            session_id=sid,
            employee_id=emp,
            trace_id=f"lbn-{i}",
        )
        sid = pack["_session_id"]

    pack = orch.run_chat(
        company_id="company-a",
        message="leave e back koro",
        session_id=sid,
        employee_id=emp,
        trace_id="lbn-back",
    )
    session = orch.memory.get_or_create_session(
        company_id="company-a", employee_id=emp, session_id=sid
    )
    draft = read_leave_state(session.workflow_state).get("draft") or {}
    assert "back koro" not in str(draft.get("reason") or "").lower()
    assert "betha" in str(draft.get("reason") or "").lower()
