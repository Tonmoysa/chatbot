"""Multi-workflow context: suspend leave, complete expense, resume leave with draft intact."""

import datetime as dt

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM, INTENT_LEAVE_REQUEST
from chat.services.leave_fsm import read_leave_state
from chat.services.leave_workflow import is_leave_in_progress, process_leave_turn
from chat.services.expense_workflow import is_expense_in_progress, process_expense_turn
from chat.services.orchestrator import ChatOrchestrator
from chat.services.workflow_suspend import (
    has_suspended_leave,
    suspend_leave_for_workflow_switch,
)

COMPANY_ID = "company-a"


@pytest.mark.django_db
def test_leave_to_expense_to_leave_preserves_draft(monkeypatch):
    fixed = dt.date(2026, 5, 7)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)

    orch = ChatOrchestrator()
    emp = "wf-switch-leave-exp"
    wf: dict = {}
    pack = process_leave_turn(
        workflow_state=wf,
        message="ami kalke sick leave nite chai",
        entities={},
        company_id=COMPANY_ID,
    )
    assert is_leave_in_progress(pack["workflow_state"])
    draft_before = dict(read_leave_state(pack["workflow_state"]).get("draft") or {})
    assert draft_before.get("leave_type") == "sick"
    assert draft_before.get("start_date")

    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        session_id="wf-switch-sess",
        employee_id=emp,
    )
    session.workflow_state = pack["workflow_state"]
    session.save(update_fields=["workflow_state", "updated_at"])

    r_exp_start = orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 100 taka, bus 50 office to badda",
        session_id=session.session_id,
        employee_id=emp,
        trace_id="wf-switch-exp-1",
    )
    assert r_exp_start["intent"] == INTENT_EXPENSE_CLAIM
    session.refresh_from_db()
    assert is_expense_in_progress(session.workflow_state)
    assert has_suspended_leave(session.workflow_state)
    assert not is_leave_in_progress(session.workflow_state)
    sl = session.workflow_state.get("suspended_leave") or {}
    assert sl.get("draft", {}).get("leave_type") == "sick"

    for _ in range(12):
        session.refresh_from_db()
        if not is_expense_in_progress(session.workflow_state):
            break
        stage = (session.workflow_state.get("expense_request") or {}).get("stage")
        if stage == "review":
            out = orch.run_chat(
                company_id=COMPANY_ID,
                message="শেষ",
                session_id=session.session_id,
                employee_id=emp,
                trace_id="wf-switch-exp-review",
            )
            break
        orch.run_chat(
            company_id=COMPANY_ID,
            message="শেষ",
            session_id=session.session_id,
            employee_id=emp,
            trace_id=f"wf-switch-exp-loop-{_}",
        )

    session.refresh_from_db()
    stage = (session.workflow_state.get("expense_request") or {}).get("stage")
    if stage == "review":
        orch.run_chat(
            company_id=COMPANY_ID,
            message="yes",
            session_id=session.session_id,
            employee_id=emp,
            trace_id="wf-switch-exp-yes1",
        )
    session.refresh_from_db()
    stage = (session.workflow_state.get("expense_request") or {}).get("stage")
    if stage == "submit_confirm":
        out_submit = orch.run_chat(
            company_id=COMPANY_ID,
            message="yes",
            session_id=session.session_id,
            employee_id=emp,
            trace_id="wf-switch-exp-submit",
        )
        assert out_submit["decision"]["outcome"] == "SUBMITTED"
        session.refresh_from_db()
        assert is_leave_in_progress(session.workflow_state)
        assert not has_suspended_leave(session.workflow_state)
        msg = out_submit["response"]["message"] or ""
        assert "ছুটি" in msg or "leave" in msg.lower() or "বেতন" in msg

    session.refresh_from_db()
    st = read_leave_state(session.workflow_state)
    draft_after = dict(st.get("draft") or {})
    assert draft_after.get("leave_type") == "sick"
    assert draft_after.get("start_date") == draft_before.get("start_date")

    r_continue = orch.run_chat(
        company_id=COMPANY_ID,
        message="paid",
        session_id=session.session_id,
        employee_id=emp,
        trace_id="wf-switch-leave-paid",
    )
    assert r_continue["intent"] == INTENT_LEAVE_REQUEST
    session.refresh_from_db()
    st2 = read_leave_state(session.workflow_state)
    assert st2.get("draft", {}).get("leave_payment_category") == "paid"
    assert st2.get("draft", {}).get("leave_type") == "sick"


def test_suspend_leave_snapshot_unit():
    wf: dict = {}
    pack = process_leave_turn(
        workflow_state=wf,
        message="ami kalke sick leave nite chai",
        entities={},
        company_id=COMPANY_ID,
    )
    suspended = suspend_leave_for_workflow_switch(pack["workflow_state"])
    assert has_suspended_leave(suspended)
    assert not is_leave_in_progress(suspended)
    sl = suspended["suspended_leave"]
    assert sl["draft"].get("leave_type") == "sick"
    assert sl.get("step")
