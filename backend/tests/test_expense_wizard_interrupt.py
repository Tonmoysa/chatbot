"""Expense wizard: pause for side questions, resume for summary."""

import pytest

from chat.constants import (
    INTENT_EXPENSE_CLAIM,
    INTENT_HR_POLICY,
    INTENT_LEAVE_REQUEST,
    INTENT_REQUEST_STATUS,
)
from chat.services.expense_workflow import (
    is_expense_in_progress,
    is_expense_paused,
    process_expense_turn,
    wants_expense_summary,
)
from chat.services.intent_detector import _looks_like_chitchat
from chat.services.orchestrator import (
    ChatOrchestrator,
    _detect_intent_during_expense_workflow,
)

COMPANY_ID = "company-a"


def test_yes_is_not_chitchat_in_strict_mode():
    assert not _looks_like_chitchat("yes", strict=True)
    assert not _looks_like_chitchat("হ্যাঁ", strict=True)


def test_yes_during_expense_is_confirm_intent():
    out = _detect_intent_during_expense_workflow(
        "yes",
        {"expense_request": {"active": True, "stage": "review"}},
        balance_probe=False,
    )
    assert out["intent"] == INTENT_EXPENSE_CLAIM
    assert out["source"] == "expense_workflow_gate+confirm"


def test_leave_policy_during_expense_is_hr_policy_not_leave_request():
    out = _detect_intent_during_expense_workflow(
        "amake leave policy ta bolo",
        {"expense_request": {"active": True, "stage": "collecting", "items": []}},
        balance_probe=False,
    )
    assert out["intent"] == INTENT_HR_POLICY
    assert out["intent"] != INTENT_LEAVE_REQUEST


def test_wants_expense_summary_banglish():
    assert wants_expense_summary("expense er summery ta bolo")
    assert wants_expense_summary("okay ekhon summery ta bolo")
    assert wants_expense_summary("শেষ")


@pytest.mark.django_db
def test_greeting_during_expense_does_not_clear_draft():
    orch = ChatOrchestrator()
    emp = "exp-greet-keep"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 100 taka",
        session_id=None,
        employee_id=emp,
        trace_id="exp-greet-1",
    )
    sid = r1["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    assert is_expense_in_progress(session.workflow_state)

    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="hi",
        session_id=sid,
        employee_id=emp,
        trace_id="exp-greet-2",
    )
    session.refresh_from_db()
    msg = r2["response"]["message"] or ""
    assert is_expense_in_progress(session.workflow_state)
    assert (
        "খরচ" in msg
        or "আর কোনো" in msg
        or "Daily expense" in msg
        or "পর্যালোচনা" in msg
        or "Mot:" in msg
    )


@pytest.mark.django_db
def test_policy_then_expense_summary_resumes_draft(settings):
    settings.KB_RAG_ENABLED = True
    from unittest.mock import patch

    orch = ChatOrchestrator()
    emp = "exp-policy-summary"
    wf: dict = {}
    pack = process_expense_turn(
        workflow_state=wf,
        message="lunch 100 taka, bike 50 mirpur to badda",
    )
    while pack.get("question") and not pack.get("complete"):
        pack = process_expense_turn(
            workflow_state=pack["workflow_state"],
            message="শেষ",
        )
        if "পর্যালোচনা" in (pack.get("question") or ""):
            break

    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        session_id="exp-pol-sum-sess",
        employee_id=emp,
    )
    session.workflow_state = pack["workflow_state"]
    session.save(update_fields=["workflow_state", "updated_at"])

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
            session_id=session.session_id,
            employee_id=emp,
            trace_id="exp-pol-1",
        )
    assert pol["intent"] == INTENT_HR_POLICY
    pol_msg = pol["response"]["message"]
    assert "60 টাকা খরচ হয়েছে — বুঝেছি" not in pol_msg
    assert "Lunch, Snack, Bus" not in pol_msg
    assert "খরচের ধরন — বুঝেছি" not in pol_msg
    assert (
        "Leave must be applied" in pol_msg
        or "ছুটি" in pol_msg
        or "leave" in pol_msg.lower()
    )
    session.refresh_from_db()
    assert is_expense_paused(session.workflow_state)

    summ = orch.run_chat(
        company_id=COMPANY_ID,
        message="expense er summery ta bolo",
        session_id=session.session_id,
        employee_id=emp,
        trace_id="exp-pol-2",
    )
    assert summ["intent"] == INTENT_EXPENSE_CLAIM
    msg = summ["response"]["message"]
    assert "পর্যালোচনা" in msg or "মোট" in msg
    session.refresh_from_db()
    assert is_expense_in_progress(session.workflow_state)
    assert not is_expense_paused(session.workflow_state)


@pytest.mark.django_db
def test_yes_at_review_advances_to_submit_confirm():
    orch = ChatOrchestrator()
    emp = "exp-yes-review"
    wf: dict = {}
    pack = process_expense_turn(
        workflow_state=wf,
        message="lunch 100, bus 50 office to badda",
    )
    for _ in range(6):
        if (pack.get("workflow_state", {}).get("expense_request") or {}).get("stage") == "review":
            break
        pack = process_expense_turn(
            workflow_state=pack["workflow_state"],
            message="শেষ",
        )
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        session_id="exp-yes-review-sess",
        employee_id=emp,
    )
    session.workflow_state = pack["workflow_state"]
    session.save(update_fields=["workflow_state", "updated_at"])

    out = orch.run_chat(
        company_id=COMPANY_ID,
        message="yes",
        session_id=session.session_id,
        employee_id=emp,
        trace_id="exp-yes-r2",
    )
    assert "All good on my end" not in (out["response"]["message"] or "")
    assert (
        "জমা" in out["response"]["message"]
        or "submit" in out["response"]["message"].lower()
        or "CRM" in out["response"]["message"]
    )


@pytest.mark.django_db
def test_yes_during_expense_review_after_leave_submitted():
    """Expense yes must not be swallowed by a prior locked leave submission."""
    from chat.services.leave_fsm import mark_submitted

    orch = ChatOrchestrator()
    emp = "exp-after-leave-yes"
    wf: dict = {}
    pack = process_expense_turn(
        workflow_state=wf,
        message="bus 100 office to baada, lunch 100",
    )
    for _ in range(8):
        stage = (pack.get("workflow_state", {}).get("expense_request") or {}).get("stage")
        if stage == "review":
            break
        pack = process_expense_turn(
            workflow_state=pack["workflow_state"],
            message="done",
        )

    wf_state = mark_submitted(
        pack["workflow_state"],
        draft={"leave_type": "casual", "start_date": "2026-05-25"},
        submission_id="PHP-LEAVE-TEST123",
    )
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        session_id="exp-after-leave-sess",
        employee_id=emp,
    )
    session.workflow_state = wf_state
    session.save(update_fields=["workflow_state", "updated_at"])

    out = orch.run_chat(
        company_id=COMPANY_ID,
        message="yes",
        session_id=session.session_id,
        employee_id=emp,
        trace_id="exp-after-leave-yes",
    )
    msg = out["response"]["message"] or ""
    assert "already submitted" not in msg.lower()
    assert "PHP-LEAVE-TEST123" not in msg
    assert out["intent"] == INTENT_EXPENSE_CLAIM
    assert (
        "submit" in msg.lower()
        or "জমা" in msg
        or "CRM" in msg
        or "correct" in msg.lower()
    )


@pytest.mark.django_db
def test_double_yes_submits_expense():
    orch = ChatOrchestrator()
    emp = "exp-yes-submit"
    wf: dict = {}
    pack = process_expense_turn(
        workflow_state=wf,
        message="lunch 100, bus 50 office to badda",
    )
    for _ in range(6):
        if (pack.get("workflow_state", {}).get("expense_request") or {}).get("stage") == "review":
            break
        pack = process_expense_turn(
            workflow_state=pack["workflow_state"],
            message="শেষ",
        )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="yes",
    )
    assert (pack.get("workflow_state", {}).get("expense_request") or {}).get("stage") == "submit_confirm"

    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        session_id="exp-yes-submit-sess",
        employee_id=emp,
    )
    session.workflow_state = pack["workflow_state"]
    session.save(update_fields=["workflow_state", "updated_at"])

    out = orch.run_chat(
        company_id=COMPANY_ID,
        message="yes",
        session_id=session.session_id,
        employee_id=emp,
        trace_id="exp-ys-submit",
    )
    assert out["decision"]["outcome"] == "SUBMITTED"
    assert "All good on my end" not in (out["response"]["message"] or "")
    assert "জমা" in out["response"]["message"] or "EXP-" in out["response"]["message"]


def test_leave_submit_status_during_expense_wizard():
    from chat.services.leave_fsm import mark_submitted

    out = _detect_intent_during_expense_workflow(
        "amar leave request ki submit hoyeche",
        {"expense_request": {"active": True, "stage": "submit_confirm"}},
        balance_probe=False,
    )
    assert out["intent"] == INTENT_REQUEST_STATUS

    orch = ChatOrchestrator()
    emp = "leave-status-during-exp"
    wf = mark_submitted(
        {
            "expense_request": {
                "active": True,
                "stage": "submit_confirm",
                "items": [{"category": "Lunch", "amount": 70}],
                "incurred_date_iso": "2026-05-23",
            }
        },
        draft={"leave_type": "sick", "start_date": "2026-05-25"},
        submission_id="PHP-LEAVE-STATUS1",
    )
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        session_id="leave-status-exp-sess",
        employee_id=emp,
    )
    session.workflow_state = wf
    session.save(update_fields=["workflow_state", "updated_at"])

    out = orch.run_chat(
        company_id=COMPANY_ID,
        message="amar leave request ki submit hoyeche",
        session_id=session.session_id,
        employee_id=emp,
        trace_id="leave-status-exp",
    )
    msg = out["response"]["message"] or ""
    assert out["intent"] == INTENT_REQUEST_STATUS
    assert "PHP-LEAVE-STATUS1" in msg
    assert "জমা হয়েছে" in msg
    assert "Company policy: submit each day's expense" not in msg
