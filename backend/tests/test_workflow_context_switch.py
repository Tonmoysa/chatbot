"""Multi-workflow context: suspend leave, complete expense, resume leave with draft intact."""

import datetime as dt

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM, INTENT_HR_POLICY, INTENT_LEAVE_REQUEST
from chat.services.leave_fsm import read_leave_state
from chat.services.leave_workflow import is_leave_in_progress, process_leave_turn
from chat.services.expense_workflow import (
    is_expense_in_progress,
    process_expense_turn,
    wants_resume_or_show_expense,
)
from chat.services.leave_confirm import (
    wants_defer_expense_for_leave_submit,
)
from chat.services.orchestrator import (
    ChatOrchestrator,
    _detect_intent_during_leave_workflow,
    _detect_intent_during_expense_workflow,
)
from chat.services.workflow_suspend import (
    has_suspended_expense,
    has_suspended_leave,
    suspend_leave_for_workflow_switch,
)

COMPANY_ID = "company-a"

BN_FAMILY_LEAVE = (
    "tomarrow ami familly er jonno bahire jabo...tai amar full day chuti lagbe..and paid hobe"
)
EXPENSE_DURING_LEAVE_REVIEW = (
    "amar ajke lunch 100 taka bus 20 taka and 60 taka cost hoyeche"
)
EXPENSE_SIMPLE = "amar ajke 100 taka cost hoyeche bus 10 taka lunch 50 taka"
DEFER_LEAVE_SUBMIT = "leave request ta age submit koro"
RESUME_EXPENSE_MSGS = (
    "ekhon expense e asho",
    "ekhon expense e back koro",
    "ekhon ager expense ta amake daw",
    "expense ta amke again daw",
)


def test_defer_leave_submit_phrase_detected():
    assert wants_defer_expense_for_leave_submit(DEFER_LEAVE_SUBMIT)
    assert not wants_defer_expense_for_leave_submit("expense ta age submit koro")


def test_defer_leave_submit_at_expense_intent_not_expense_confirm():
    wf = {
        "expense_request": {"active": True, "stage": "collecting", "pending_line": {}},
        "suspended_leave": {
            "draft": {"start_date": "2026-06-04", "reason": "family"},
            "review_pending": True,
        },
    }
    out = _detect_intent_during_expense_workflow(
        DEFER_LEAVE_SUBMIT,
        wf,
        balance_probe=False,
    )
    assert out["intent"] == INTENT_LEAVE_REQUEST
    assert "defer_leave_submit" in out.get("source", "")
    assert out["source"] != "expense_workflow_gate+confirm"


def test_expense_at_leave_confirmation_intent_not_freeform():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "review_pending": True,
        "draft": {
            "start_date": "2026-06-04",
            "end_date": "2026-06-04",
            "leave_payment_category": "paid",
            "day_scope": "full",
            "reason": "family",
        },
    }
    out = _detect_intent_during_leave_workflow(
        EXPENSE_DURING_LEAVE_REVIEW,
        wf,
        balance_probe=False,
    )
    assert out["intent"] == INTENT_EXPENSE_CLAIM
    assert "confirm_expense_switch" in out.get("source", "")
    assert "confirm_freeform" not in out.get("source", "")


@pytest.mark.django_db
def test_expense_during_leave_review_suspends_draft_and_keeps_tomorrow_date(
    monkeypatch,
):
    """Regression: expense lines at leave confirm must not patch leave dates (ajke → today)."""
    fixed = dt.date(2026, 6, 3)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )

    orch = ChatOrchestrator()
    emp = "wf-exp-at-leave-review"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message=BN_FAMILY_LEAVE,
        session_id=None,
        employee_id=emp,
        trace_id="wf-exp-leave-start",
    )
    sid = r1["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        employee_id=emp,
        session_id=sid,
    )
    st = read_leave_state(session.workflow_state)
    assert st.get("review_pending") or "জমা দেবেন" in (r1["response"]["message"] or "")
    draft_before = dict(st.get("draft") or {})
    if not st.get("review_pending"):
        r_full = orch.run_chat(
            company_id=COMPANY_ID,
            message="full",
            session_id=sid,
            employee_id=emp,
            trace_id="wf-exp-leave-full",
        )
        session.refresh_from_db()
        draft_before = dict(read_leave_state(session.workflow_state).get("draft") or {})
        assert "জমা দেবেন" in (r_full["response"]["message"] or "")
    assert draft_before.get("start_date") == "2026-06-04"

    r_exp = orch.run_chat(
        company_id=COMPANY_ID,
        message=EXPENSE_DURING_LEAVE_REVIEW,
        session_id=sid,
        employee_id=emp,
        trace_id="wf-exp-leave-switch",
    )
    assert r_exp["intent"] == INTENT_EXPENSE_CLAIM
    msg = r_exp["response"]["message"] or ""
    assert "ছুটি আবেদন — পর্যালোচনা" not in msg
    session.refresh_from_db()
    assert is_expense_in_progress(session.workflow_state)
    assert has_suspended_leave(session.workflow_state)
    sl = session.workflow_state.get("suspended_leave") or {}
    assert sl.get("draft", {}).get("start_date") == "2026-06-04"
    assert sl.get("draft", {}).get("reason") == "family"
    assert sl.get("review_pending") is True


@pytest.mark.django_db
def test_policy_during_leave_review_then_expense_preserves_leave(monkeypatch, settings):
    settings.KB_RAG_ENABLED = True
    from unittest.mock import patch

    fixed = dt.date(2026, 6, 3)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )

    orch = ChatOrchestrator()
    emp = "wf-policy-exp-leave"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message=BN_FAMILY_LEAVE,
        session_id=None,
        employee_id=emp,
        trace_id="wf-pol-leave-start",
    )
    sid = r1["_session_id"]

    with patch(
        "chat.services.orchestrator.try_hr_policy_rag",
        return_value={
            "hit": True,
            "text": "Leave must be applied in advance.",
            "sources": [],
            "mode": "rag",
        },
    ):
        pol = orch.run_chat(
            company_id=COMPANY_ID,
            message="amake leave policy ta bolo",
            session_id=sid,
            employee_id=emp,
            trace_id="wf-pol-leave-policy",
        )
    assert pol["intent"] == INTENT_HR_POLICY
    assert "Leave must be applied" in (pol["response"]["message"] or "")

    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        employee_id=emp,
        session_id=sid,
    )
    session.refresh_from_db()
    st = read_leave_state(session.workflow_state)
    assert st.get("review_pending")
    assert st.get("draft", {}).get("start_date") == "2026-06-04"

    r_exp = orch.run_chat(
        company_id=COMPANY_ID,
        message=EXPENSE_DURING_LEAVE_REVIEW,
        session_id=sid,
        employee_id=emp,
        trace_id="wf-pol-leave-exp",
    )
    assert r_exp["intent"] == INTENT_EXPENSE_CLAIM
    session.refresh_from_db()
    assert is_expense_in_progress(session.workflow_state)
    assert has_suspended_leave(session.workflow_state)
    assert (
        session.workflow_state.get("suspended_leave", {})
        .get("draft", {})
        .get("start_date")
        == "2026-06-04"
    )


@pytest.mark.parametrize("msg", RESUME_EXPENSE_MSGS)
def test_resume_expense_phrases_detected(msg: str):
    assert wants_resume_or_show_expense(msg)


@pytest.mark.django_db
def test_defer_leave_submit_during_expense_shows_review_not_auto_submit(monkeypatch):
    """Regression: defer leave must show review + yes/edit, not instant CRM submit."""
    fixed = dt.date(2026, 6, 3)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )

    orch = ChatOrchestrator()
    emp = "wf-defer-leave-submit"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message=BN_FAMILY_LEAVE,
        session_id=None,
        employee_id=emp,
        trace_id="defer-leave-1",
    )
    sid = r1["_session_id"]

    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message=EXPENSE_SIMPLE,
        session_id=sid,
        employee_id=emp,
        trace_id="defer-leave-2",
    )
    assert r2["intent"] == INTENT_EXPENSE_CLAIM
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        employee_id=emp,
        session_id=sid,
    )
    session.refresh_from_db()
    assert is_expense_in_progress(session.workflow_state)
    assert has_suspended_leave(session.workflow_state)

    r3 = orch.run_chat(
        company_id=COMPANY_ID,
        message=DEFER_LEAVE_SUBMIT,
        session_id=sid,
        employee_id=emp,
        trace_id="defer-leave-3",
    )
    assert r3["intent"] == INTENT_LEAVE_REQUEST
    msg = r3["response"]["message"] or ""
    assert "From and To" not in msg
    assert "জমা দেবেন" in msg or "পর্যালোচনা" in msg
    assert r3["decision"]["outcome"] != "SUBMITTED"
    session.refresh_from_db()
    assert is_leave_in_progress(session.workflow_state)
    assert read_leave_state(session.workflow_state).get("review_pending")


@pytest.mark.django_db
def test_resume_expense_after_leave_submit_shows_pending_amount(monkeypatch):
    fixed = dt.date(2026, 6, 3)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )

    orch = ChatOrchestrator()
    emp = "wf-resume-exp-after-leave"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message=BN_FAMILY_LEAVE,
        session_id=None,
        employee_id=emp,
        trace_id="resume-exp-1",
    )
    sid = r1["_session_id"]
    orch.run_chat(
        company_id=COMPANY_ID,
        message="amar ajke 100 taka cost hoyeche",
        session_id=sid,
        employee_id=emp,
        trace_id="resume-exp-2",
    )
    orch.run_chat(
        company_id=COMPANY_ID,
        message=DEFER_LEAVE_SUBMIT,
        session_id=sid,
        employee_id=emp,
        trace_id="resume-exp-3",
    )
    r_yes = orch.run_chat(
        company_id=COMPANY_ID,
        message="yes",
        session_id=sid,
        employee_id=emp,
        trace_id="resume-exp-4",
    )
    assert r_yes["decision"]["outcome"] == "SUBMITTED"

    r_back = orch.run_chat(
        company_id=COMPANY_ID,
        message="ekhon expense e asho",
        session_id=sid,
        employee_id=emp,
        trace_id="resume-exp-5",
    )
    msg = r_back["response"]["message"] or ""
    assert "খরচের ধরন বুঝতে পারিনি" not in msg
    assert (
        "100" in msg
        or "জমা হয়নি" in msg
        or "submit hoyni" in msg.lower()
        or "not submitted" in msg.lower()
    )
    assert (
        "category" in msg.lower()
        or "ধরন" in msg
        or "Lunch" in msg
    )


@pytest.mark.django_db
def test_leave_expense_leave_expense_preserves_pending_amount(monkeypatch):
    """Regression: switching leave→expense→leave→expense must keep pending expense lines."""
    fixed = dt.date(2026, 6, 3)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )

    orch = ChatOrchestrator()
    emp = "wf-double-switch-bug"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message=BN_FAMILY_LEAVE,
        session_id=None,
        employee_id=emp,
        trace_id="ds-1",
    )
    sid = r1["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    session.refresh_from_db()
    st = read_leave_state(session.workflow_state)
    if not st.get("review_pending"):
        orch.run_chat(
            company_id=COMPANY_ID,
            message="full",
            session_id=sid,
            employee_id=emp,
            trace_id="ds-1b",
        )

    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="amar ajke 100 taka cost hoyeche",
        session_id=sid,
        employee_id=emp,
        trace_id="ds-2",
    )
    assert r2["intent"] == INTENT_EXPENSE_CLAIM
    assert "100" in (r2["response"]["message"] or "")

    r3 = orch.run_chat(
        company_id=COMPANY_ID,
        message="okay leave request amake again daw",
        session_id=sid,
        employee_id=emp,
        trace_id="ds-3",
    )
    assert r3["intent"] == INTENT_LEAVE_REQUEST
    assert "জমা দেবেন" in (r3["response"]["message"] or "")

    session.refresh_from_db()
    assert has_suspended_expense(session.workflow_state)
    sus = session.workflow_state.get("suspended_expense", {}).get("expense_request") or {}
    assert sus.get("pending_line", {}).get("amount") == 100

    r4 = orch.run_chat(
        company_id=COMPANY_ID,
        message="expense ta amke again daw",
        session_id=sid,
        employee_id=emp,
        trace_id="ds-4",
    )
    msg4 = r4["response"]["message"] or ""
    session.refresh_from_db()
    exp = session.workflow_state.get("expense_request") or {}
    pending = exp.get("pending_line") or {}

    assert r4["intent"] == INTENT_EXPENSE_CLAIM
    assert pending.get("amount") == 100, f"pending lost; wf={exp!r}; msg={msg4!r}"
    assert "100" in msg4 or "category" in msg4.lower() or "ধরন" in msg4
    assert "আজকের খরচ বলুন" not in msg4

    r5 = orch.run_chat(
        company_id=COMPANY_ID,
        message="kichu somoy age toh ami ekta expense er information dilam seta amake daw",
        session_id=sid,
        employee_id=emp,
        trace_id="ds-5",
    )
    msg5 = r5["response"]["message"] or ""
    assert "100" in msg5
    assert "আজকের খরচ বলুন" not in msg5


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
