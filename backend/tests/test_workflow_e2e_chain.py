"""End-to-end workflow chain: leave → expense → policy → back → edit (P3)."""

import datetime as dt
from unittest.mock import patch

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM, INTENT_HR_POLICY, INTENT_LEAVE_REQUEST
from chat.services.expense_workflow import is_expense_in_progress, process_expense_turn
from chat.services.leave_fsm import read_leave_state
from chat.services.leave_workflow import is_leave_in_progress, process_leave_turn
from chat.services.orchestrator import ChatOrchestrator
from chat.services.workflow_suspend import (
    has_suspended_expense,
    has_suspended_leave,
    suspend_leave_for_workflow_switch,
)

COMPANY_ID = "company-a"
BN_FAMILY_LEAVE = (
    "tomarrow ami familly er jonno bahire jabo...tai amar full day chuti lagbe..and paid hobe"
)
EXPENSE_MSG = "amar ajke lunch 100 taka bus 20 taka"


@pytest.mark.django_db
def test_leave_expense_policy_back_edit_chain(monkeypatch, settings):
    """Leave review → expense → policy → resume leave → edit reason."""
    settings.KB_RAG_ENABLED = True
    fixed = dt.date(2026, 6, 3)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )

    orch = ChatOrchestrator()
    emp = "wf-e2e-chain"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message=BN_FAMILY_LEAVE,
        session_id=None,
        employee_id=emp,
        trace_id="e2e-leave-start",
    )
    sid = r1["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    assert is_leave_in_progress(session.workflow_state)

    r_exp = orch.run_chat(
        company_id=COMPANY_ID,
        message=EXPENSE_MSG,
        session_id=sid,
        employee_id=emp,
        trace_id="e2e-exp-switch",
    )
    assert r_exp["intent"] == INTENT_EXPENSE_CLAIM
    session.refresh_from_db()
    assert is_expense_in_progress(session.workflow_state)
    assert has_suspended_leave(session.workflow_state)

    with patch(
        "chat.services.orchestrator.try_hr_policy_rag",
        return_value={
            "hit": True,
            "text": "Apply leave in advance when possible.",
            "sources": [],
            "mode": "rag",
        },
    ):
        pol = orch.run_chat(
            company_id=COMPANY_ID,
            message="leave policy ta bolo",
            session_id=sid,
            employee_id=emp,
            trace_id="e2e-policy",
        )
    assert pol["intent"] == INTENT_HR_POLICY
    session.refresh_from_db()
    assert has_suspended_leave(session.workflow_state)

    r_back = orch.run_chat(
        company_id=COMPANY_ID,
        message="leave e back koro",
        session_id=sid,
        employee_id=emp,
        trace_id="e2e-leave-back",
    )
    assert r_back["intent"] == INTENT_LEAVE_REQUEST
    session.refresh_from_db()
    assert is_leave_in_progress(session.workflow_state)
    sl_draft = read_leave_state(session.workflow_state).get("draft") or {}
    assert sl_draft.get("reason")

    r_edit = orch.run_chat(
        company_id=COMPANY_ID,
        message="edit",
        session_id=sid,
        employee_id=emp,
        trace_id="e2e-leave-edit-menu",
    )
    edit_msg = r_edit["response"]["message"] or ""
    assert "reason" in edit_msg.lower() or "কারণ" in edit_msg

    r_pick = orch.run_chat(
        company_id=COMPANY_ID,
        message="reason",
        session_id=sid,
        employee_id=emp,
        trace_id="e2e-leave-edit-pick",
    )
    assert "reason" in (r_pick["response"]["message"] or "").lower() or "কারণ" in (
        r_pick["response"]["message"] or ""
    )

    r_reason = orch.run_chat(
        company_id=COMPANY_ID,
        message="family wedding",
        session_id=sid,
        employee_id=emp,
        trace_id="e2e-leave-edit-reason",
    )
    session.refresh_from_db()
    draft = read_leave_state(session.workflow_state).get("draft") or {}
    assert "wedding" in draft.get("reason", "").lower()
    assert has_suspended_expense(session.workflow_state)


def test_process_turns_leave_then_expense_without_orchestrator(monkeypatch):
    """Lightweight chain: leave draft + expense processor (orchestrator handles suspend)."""
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    wf = {}
    wf = process_leave_turn(
        workflow_state=wf,
        message="2026-06-05 paid full day family visit",
        entities={},
    )["workflow_state"]
    assert is_leave_in_progress(wf)
    wf = suspend_leave_for_workflow_switch(wf)
    wf = process_expense_turn(workflow_state=wf, message=EXPENSE_MSG)["workflow_state"]
    assert is_expense_in_progress(wf)
    assert has_suspended_leave(wf)
