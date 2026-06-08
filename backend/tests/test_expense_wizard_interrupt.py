"""Expense wizard: pause for side questions, resume for summary."""

import pytest

from chat.constants import (
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_HR_POLICY,
    INTENT_LEAVE_REQUEST,
    INTENT_REQUEST_STATUS,
    INTENT_UNKNOWN,
)
from chat.services.expense_workflow import (
    is_expense_in_progress,
    is_expense_paused,
    process_expense_turn,
    wants_expense_summary,
)
from chat.services.intent_detector import _looks_like_chitchat
from chat.services.leave_confirm import is_confirmation_yes, wants_defer_leave_for_expense_submit
from chat.services.leave_fsm import read_leave_state
from chat.services.leave_workflow import is_leave_in_progress
from chat.services.orchestrator import (
    ChatOrchestrator,
    _detect_intent_during_expense_workflow,
    _detect_intent_during_leave_workflow,
)
from chat.services.turn_classifier import TURN_CHITCHAT, TURN_CONFIRM, classify_workflow_turn
from chat.services.workflow_suspend import has_suspended_expense, suspend_expense_for_workflow_switch

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
    assert wants_expense_summary(
        "ajke ami ki ki khorose korechi tar ekta summery bolo"
    )
    assert wants_expense_summary("ajke ki ki khoroch korechi summery bolo")


def test_bangla_expense_recap_not_out_of_scope():
    from chat.services.policy_intent_helpers import is_off_topic_for_hr_assistant

    msg = "ajke ami ki ki khorose korechi tar ekta summery bolo"
    assert not is_off_topic_for_hr_assistant(msg, wizard_active=True)


def test_bangla_expense_recap_intent_during_active_expense():
    out = _detect_intent_during_expense_workflow(
        "ajke ami ki ki khorose korechi tar ekta summery bolo",
        {"expense_request": {"active": True, "stage": "collecting", "items": []}},
        balance_probe=False,
    )
    assert out["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    assert "summary" in out["source"]


def test_expense_summary_during_active_expense_is_day_summary_intent():
    out = _detect_intent_during_expense_workflow(
        "okay expense summery ta bolo",
        {"expense_request": {"active": True, "stage": "collecting", "items": []}},
        balance_probe=False,
    )
    assert out["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    assert out["source"] == "expense_workflow_gate+summary"


def test_expense_summary_during_leave_review_is_day_summary_intent():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "review_pending": True,
        "draft": {"start_date": "2026-06-09", "reason": "sick"},
        "suspended_expense": {
            "expense_request": {
                "active": True,
                "incurred_date_iso": "2026-06-08",
                "items": [{"category": "Lunch", "amount": 100}],
            }
        },
    }
    out = _detect_intent_during_leave_workflow(
        "okay expense summery ta bolo",
        wf,
        balance_probe=False,
    )
    assert out["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    assert "expense_summary" in out.get("source", "")


def test_expense_summary_not_wizard_confirm_turn():
    turn = classify_workflow_turn(
        "okay expense summery ta bolo",
        leave_active=True,
        expense_active=False,
        leave_review_pending=True,
    )
    assert turn == TURN_CHITCHAT
    assert turn != TURN_CONFIRM


@pytest.mark.django_db
def test_eid_kobe_during_active_expense_does_not_resume_wizard(monkeypatch):
    """Unrelated calendar trivia must not hijack an in-progress expense draft."""
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    emp = "exp-eid-interrupt"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="metro 100 uttora to motijheel, lunch 50",
        session_id=None,
        employee_id=emp,
        trace_id="exp-eid-1",
    )
    sid = r1["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    assert is_expense_in_progress(session.workflow_state)
    items_before = list(
        (session.workflow_state.get("expense_request") or {}).get("items") or []
    )
    assert len(items_before) >= 1

    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="eid kobe?",
        session_id=sid,
        employee_id=emp,
        trace_id="exp-eid-2",
    )
    session.refresh_from_db()
    msg = r2["response"]["message"] or ""
    assert r2["intent"] == INTENT_UNKNOWN
    assert "note kora hoyeche" not in msg.lower()
    assert "Ar kono kharcha" not in msg
    assert (
        "বাইরে" in msg
        or "সাহায্য" in msg
        or "scope" in msg.lower()
        or "HR" in msg
    )
    assert "draft" not in msg.lower()
    assert "still saved" not in msg.lower()
    block = session.workflow_state.get("expense_request") or {}
    assert block.get("active") is True
    assert len(block.get("items") or []) == len(items_before)


@pytest.mark.django_db
def test_country_name_during_expense_declines_without_full_resume(monkeypatch):
    """General-knowledge side questions must not resume the expense review screen."""
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
        lambda **_k: "আপনার দেশের নাম বাংলাদেশ।",
    )
    orch = ChatOrchestrator()
    emp = "exp-country-interrupt"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="metro 100 uttora to motijheel, lunch 50",
        session_id=None,
        employee_id=emp,
        trace_id="exp-country-1",
    )
    sid = r1["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    assert is_expense_in_progress(session.workflow_state)

    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="amader desher nam ki?",
        session_id=sid,
        employee_id=emp,
        trace_id="exp-country-2",
    )
    msg = r2["response"]["message"] or ""
    assert r2["intent"] == INTENT_UNKNOWN
    assert "বাংলাদেশ" not in msg
    assert "পর্যালোচনা" not in msg
    assert "Is the information above correct" not in msg
    assert (
        "বাইরে" in msg
        or "HR" in msg
        or "scope" in msg.lower()
        or "general knowledge" in msg.lower()
    )
    assert "draft" not in msg.lower()
    assert "still saved" not in msg.lower()
    session.refresh_from_db()
    assert is_expense_paused(session.workflow_state)


@pytest.mark.django_db
def test_compound_expense_metroral_typo_no_category_reask():
    msg = (
        "ami ajke motejhell theke mirpur aschi bus e 50 taka expense hoyeche "
        "then lunch 100 taka then mirpur to uttora te aschi metroral e expense hoyeche 40 taka"
    )
    r = process_expense_turn(workflow_state={}, message=msg)
    items = r.get("items") or []
    cats = {row.get("category") for row in items}
    assert "Metro Rail" in cats
    assert "Bus" in cats
    assert "Lunch" in cats
    q = (r.get("question") or "").lower()
    assert "category ki" not in q


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
    assert msg.strip()
    assert "draft" not in msg.lower()
    assert "still saved" not in msg.lower()


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
    assert summ["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    msg = summ["response"]["message"]
    assert "মোট" in msg or "Pending" in msg or "pending" in msg.lower()
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


@pytest.mark.django_db
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


def test_defer_expense_submit_is_not_leave_confirmation_yes():
    msg = "expense ta age submit koro"
    assert wants_defer_leave_for_expense_submit(msg)
    assert not is_confirmation_yes(msg)


def test_leave_confirm_gate_defers_to_expense_when_parked():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "review_pending": True,
        "draft": {
            "start_date": "2026-06-03",
            "leave_payment_category": "paid",
            "day_scope": "full",
            "reason": "family",
        },
        "suspended_expense": {
            "expense_request": {
                "active": True,
                "stage": "submit_confirm",
                "items": [{"category": "Lunch", "amount": 50}],
            }
        },
    }
    out = _detect_intent_during_leave_workflow(
        "expense ta age submit koro",
        wf,
        balance_probe=False,
    )
    assert out["intent"] == INTENT_EXPENSE_CLAIM
    assert "defer_expense" in out.get("source", "")


@pytest.mark.django_db
def test_defer_expense_submit_at_leave_review_submits_expense_not_leave(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    emp = "defer-exp-submit-pytest"
    wf: dict = {}
    pack = process_expense_turn(
        workflow_state=wf,
        message="lunch 50, bus 30 mirpur to baridhara, snack 100",
    )
    for _ in range(8):
        stage = (pack.get("workflow_state", {}).get("expense_request") or {}).get("stage")
        if stage == "review":
            pack = process_expense_turn(
                workflow_state=pack["workflow_state"],
                message="শেষ",
            )
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

    leave_wf = {
        "active_flow": "leave",
        "status": "active",
        "review_pending": True,
        "draft": {
            "start_date": "2026-06-03",
            "end_date": "2026-06-03",
            "leave_payment_category": "paid",
            "day_scope": "full",
            "reason": "family",
            "leave_type": "casual",
        },
    }
    leave_wf = suspend_expense_for_workflow_switch(
        {**leave_wf, "expense_request": pack["workflow_state"]["expense_request"]}
    )
    assert has_suspended_expense(leave_wf)
    assert read_leave_state(leave_wf).get("review_pending")

    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        session_id="defer-exp-sess",
        employee_id=emp,
    )
    session.workflow_state = leave_wf
    session.save(update_fields=["workflow_state", "updated_at"])

    out = orch.run_chat(
        company_id=COMPANY_ID,
        message="expense ta age submit koro",
        session_id=session.session_id,
        employee_id=emp,
        trace_id="defer-exp-submit",
    )
    assert out["intent"] == INTENT_EXPENSE_CLAIM
    assert out["decision"]["outcome"] == "SUBMITTED"
    assert "PHP-LEAVE" not in (out["response"]["message"] or "")
    session.refresh_from_db()
    assert read_leave_state(session.workflow_state).get("review_pending")
    assert is_leave_in_progress(session.workflow_state)
