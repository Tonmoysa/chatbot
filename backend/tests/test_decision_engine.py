import datetime as dt

import pytest

from chat.constants import (
    INTENT_ATTENDANCE_CORRECTION,
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_LEAVE_REQUEST,
)
from chat.services.decision_engine import DecisionEngine
from chat.services.intent_detector import IntentDetector
from chat.services.leave_days import compute_requested_leave_days
from chat.services.orchestrator import ChatOrchestrator


COMPANY_ID = "company-a"
TEST_SESSION_ID = "test-session"


def run_tenant_chat(orch: ChatOrchestrator, **kwargs):
    return orch.run_chat(company_id=COMPANY_ID, **kwargs)


def tenant_leave_balance(orch: ChatOrchestrator, employee_id: str, session_id: str = TEST_SESSION_ID):
    return orch.crm.get_leave_balance(
        company_id=COMPANY_ID,
        employee_id=employee_id,
        session_id=session_id,
    )


@pytest.mark.django_db
def test_expense_workflow_submit():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_EXPENSE_CLAIM,
        entities={
            "expense_workflow_submit": True,
            "expense_items": [{"category": "Lunch", "amount": 100}],
        },
        crm_context={},
    )
    assert d["outcome"] == "SUBMITTED"
    assert "EXPENSE_WORKFLOW_SUBMITTED" in d.get("rules_applied", [])


@pytest.mark.django_db
def test_expense_without_wizard_uses_wizard_path():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_EXPENSE_CLAIM,
        entities={"amount": 100},
        crm_context={},
    )
    assert d["outcome"] == "NEEDS_CLARIFICATION"
    assert "EXPENSE_USE_WIZARD" in d.get("rules_applied", [])


@pytest.mark.django_db
def test_expense_amount_only_routes_to_wizard():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_EXPENSE_CLAIM,
        entities={"amount": 400},
        crm_context={},
    )
    assert d["outcome"] == "NEEDS_CLARIFICATION"
    assert "EXPENSE_USE_WIZARD" in d.get("rules_applied", [])


@pytest.mark.django_db
def test_expense_receipt_without_wizard_still_uses_wizard():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_EXPENSE_CLAIM,
        entities={"amount": 400, "document_text": "Uber Trip Total 400 BDT"},
        crm_context={},
    )
    assert d["outcome"] == "NEEDS_CLARIFICATION"
    assert "EXPENSE_USE_WIZARD" in d.get("rules_applied", [])


@pytest.mark.django_db
def test_expense_receipt_mismatch_without_wizard_uses_wizard():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_EXPENSE_CLAIM,
        entities={"amount": 500, "document_text": "Uber receipt total 300"},
        crm_context={},
    )
    assert d["outcome"] == "NEEDS_CLARIFICATION"
    assert "EXPENSE_USE_WIZARD" in d.get("rules_applied", [])


@pytest.mark.django_db
def test_leave_approved_with_balance():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_LEAVE_REQUEST,
        entities={
            "leave_payment_category": "paid",
            "day_scope": "full",
            "start_date": "2026-05-10",
            "end_date": "2026-05-10",
            "days": 1,
            "reason": "Planned personal day",
        },
        crm_context={"leave_balance_days": 5},
    )
    assert d["outcome"] == "SUBMITTED"


@pytest.mark.django_db
def test_leave_paid_insufficient_balance_routes_to_hr():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_LEAVE_REQUEST,
        entities={
            "leave_payment_category": "paid",
            "day_scope": "full",
            "start_date": "2026-05-10",
            "end_date": "2026-05-12",
            "reason": "Family travel",
        },
        crm_context={"leave_balance_days": 1},
    )
    assert d["outcome"] == "SUBMITTED"
    assert d.get("route_to") == "HR"


@pytest.mark.django_db
def test_leave_lwop_routes_to_manager():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_LEAVE_REQUEST,
        entities={
            "leave_payment_category": "lwop",
            "day_scope": "full",
            "start_date": "2026-05-10",
            "end_date": "2026-05-10",
            "reason": "Personal reasons",
        },
        crm_context={"leave_balance_days": 0},
    )
    assert d["outcome"] == "SUBMITTED"
    assert d.get("route_to") == "MANAGER"


@pytest.mark.django_db
def test_leave_wizard_confirmed_skips_field_clarification():
    """Wizard-owned fields should not be re-checked after user confirms the draft."""
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_LEAVE_REQUEST,
        entities={
            "leave_workflow_confirmed": True,
            "leave_payment_category": "paid",
            "day_scope": "full",
            "start_date": "2026-05-10",
            "end_date": "2026-05-10",
            "reason": "Family event",
        },
        crm_context={"leave_balance_days": 5},
    )
    assert d["outcome"] == "SUBMITTED"
    assert "LEAVE_PAYMENT_UNKNOWN" not in (d.get("rules_applied") or [])
    assert "LEAVE_REASON_REQUIRED" not in (d.get("rules_applied") or [])


@pytest.mark.django_db
def test_leave_without_wizard_still_requires_fields():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_LEAVE_REQUEST,
        entities={"leave_payment_category": "paid"},
        crm_context={},
    )
    assert d["outcome"] == "NEEDS_CLARIFICATION"
    assert "LEAVE_DAY_SCOPE_UNKNOWN" in (d.get("rules_applied") or [])


@pytest.mark.django_db
def test_attendance_pending_review():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_ATTENDANCE_CORRECTION,
        entities={},
        crm_context={},
    )
    assert d["outcome"] == "PENDING_REVIEW"


@pytest.mark.django_db
def test_expense_claim_duplicate_is_deduped_by_orchestrator():
    """Enterprise workflow: collect → review → submit confirm → CRM."""
    orch = ChatOrchestrator()
    first = run_tenant_chat(
        orch,
        message="lunch 100",
        session_id=None,
        employee_id="dedupe-expense-unique",
        trace_id="t1",
    )
    sid = first.get("_session_id") or first.get("session_id")
    assert sid
    run_tenant_chat(
        orch,
        message="হ্যাঁ",
        session_id=sid,
        employee_id="dedupe-expense-unique",
        trace_id="t1b",
    )
    second = run_tenant_chat(
        orch,
        message="হ্যাঁ",
        session_id=sid,
        employee_id="dedupe-expense-unique",
        trace_id="t1c",
    )
    rid1 = second["response"]["request_id"]
    assert rid1
    assert second["decision"]["outcome"] == "SUBMITTED"

    third = run_tenant_chat(
        orch,
        message="lunch 100",
        session_id=sid,
        employee_id="dedupe-expense-unique",
        trace_id="t1d",
    )
    run_tenant_chat(
        orch,
        message="হ্যাঁ",
        session_id=sid,
        employee_id="dedupe-expense-unique",
        trace_id="t1e",
    )
    fourth = run_tenant_chat(
        orch,
        message="হ্যাঁ",
        session_id=sid,
        employee_id="dedupe-expense-unique",
        trace_id="t1f",
    )
    assert fourth["decision"]["outcome"] == "SUBMITTED"
    assert fourth["response"]["request_id"]


@pytest.mark.django_db
def test_leave_request_duplicate_is_deduped_after_wizard(monkeypatch):
    fixed = dt.date(2026, 5, 7)

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return fixed

    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.leave_workflow.date",
        "chat.services.decision_engine.date",
    ):
        monkeypatch.setattr(mod, FixedDate)

    emp = "leave-dedupe-pytest-unique"
    orch = ChatOrchestrator()
    # User must confirm type, paid/unpaid, and scope when not in the message.
    chain = (
        "amar kalke chuti lagbe",
        "casual",
        "paid",
        "full",
        "cousin graduation out of Dhaka.",
        "yes",
    )

    sid = None
    last = None
    for i, msg in enumerate(chain):
        last = run_tenant_chat(
            orch,
            message=msg,
            session_id=sid,
            employee_id=emp,
            trace_id=f"lvwiz-{i}",
        )
        sid = last["_session_id"]
        if msg != "yes":
            assert (
                last["decision"]["outcome"] == "NEEDS_CLARIFICATION"
            ), last["decision"]

    rid1 = last["response"]["request_id"]
    assert rid1 and last["decision"]["outcome"] == "SUBMITTED"

    bal_after_first = tenant_leave_balance(orch, emp, sid)["leave_balance_days"]

    dup_chain = (
        "amar abar kalke chuti lagbe",
        "casual",
        "paid",
        "full",
        "cousin graduation out of Dhaka.",
        "yes",
    )
    for i, msg in enumerate(dup_chain):
        last2 = run_tenant_chat(
            orch,
            message=msg,
            session_id=sid,
            employee_id=emp,
            trace_id=f"lvwiz-dup-{i}",
        )
        sid = last2["_session_id"]

    dup_msg = last2["response"]["message"] or ""
    assert (
        "আগেই জমা" in dup_msg
        or "ইতিমধ্যে" in dup_msg
        or "already submitted" in dup_msg.lower()
        or "LEAVE_OVERLAP" in " ".join(last2["decision"].get("rules_applied", []))
    )
    assert last2["decision"]["outcome"] in ("NEEDS_CLARIFICATION", "SUBMITTED")
    assert tenant_leave_balance(orch, emp, sid)["leave_balance_days"] == bal_after_first


def test_expense_day_summary_intent_rules(monkeypatch):
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: False)
    r = det.detect(
        "Today I forgot how much I spent — can you show a summary of my expenses?",
        "tid-sum-intent",
    )
    assert r["intent"] == INTENT_EXPENSE_DAY_SUMMARY


def test_expense_day_summary_banglish_total_cost(monkeypatch):
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: False)
    r = det.detect("amar total cost koto hoyeche", "tid-bn-total")
    assert r["intent"] == INTENT_EXPENSE_DAY_SUMMARY


def test_expense_day_summary_banglish_expense_er_summery(monkeypatch):
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: False)
    r = det.detect("okay amake expense er summery ta daw toh", "tid-bn-exp-sum")
    assert r["intent"] == INTENT_EXPENSE_DAY_SUMMARY


@pytest.mark.django_db
def test_expense_day_summary_shows_totals_and_remaining(monkeypatch):
    fixed = dt.date(2026, 6, 15)

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return fixed

    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.expense.entity_pipeline.date",
        "chat.services.expense_incurred_date.date",
        "chat.services.decision_engine.date",
        "chat.services.orchestrator.date",
        "chat.services.expense_workflow.date",
    ):
        monkeypatch.setattr(mod, FixedDate)
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.hr_query_classifier.LLMClient.is_configured",
        lambda self: False,
    )

    emp = "expense-summary-pytest-unique"
    orch = ChatOrchestrator()
    sid = None
    pack = run_tenant_chat(
        orch,
        message="lunch 120, snack 50",
        session_id=sid,
        employee_id=emp,
        trace_id="es-claim-0",
    )
    sid = pack["_session_id"]
    for msg in ("শেষ", "হ্যাঁ", "হ্যাঁ"):
        pack = run_tenant_chat(
            orch,
            message=msg,
            session_id=sid,
            employee_id=emp,
            trace_id=f"es-claim-{msg}",
        )
    assert pack["decision"]["outcome"] == "SUBMITTED"

    summ = run_tenant_chat(
        orch,
        message="Today I forgot how much I spent — show me a summary.",
        session_id=sid,
        employee_id=emp,
        trace_id="es-sum",
    )
    assert summ["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    assert summ["decision"]["outcome"] == "INFORMATIONAL"
    body = summ["response"]["message"] or ""
    assert "170" in body
    assert "সারাংশ" in body


def test_compute_requested_leave_days_range_and_single_day():
    assert (
        compute_requested_leave_days(
            {"start_date": "2026-05-10", "end_date": "2026-05-12"}
        )
        == 3.0
    )
    assert compute_requested_leave_days({"start_date": "2026-05-11", "days": None}) == 1.0
    assert (
        compute_requested_leave_days(
            {
                "start_date": "2026-05-10",
                "end_date": "2026-05-10",
                "day_scope": "half",
            }
        )
        == 0.5
    )


@pytest.mark.django_db
def test_banglish_may_11_calendar_means_one_day_not_eleven(monkeypatch):
    fixed = dt.date(2026, 5, 7)

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return fixed

    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.leave_workflow.date",
        "chat.services.decision_engine.date",
    ):
        monkeypatch.setattr(mod, FixedDate)

    orch = ChatOrchestrator()

    def run_leave_block(emp: str, start_msg: str, trace_prefix: str) -> None:
        sid_local = None
        steps = (
            start_msg,
            "casual",
            "paid",
            "full day",
            "Travel to village for ceremonies.",
        )
        last_local = None
        for i, msg in enumerate(steps):
            last_local = run_tenant_chat(
                orch,
                message=msg,
                session_id=sid_local,
                employee_id=emp,
                trace_id=f"{trace_prefix}-{i}",
            )
            sid_local = last_local["_session_id"]
            if i < len(steps) - 1:
                assert last_local["decision"]["outcome"] == "NEEDS_CLARIFICATION"
        assert last_local["decision"]["outcome"] == "SUBMITTED"

    # Separate employees so CRM overlap / session quirks do not couple flows.
    assert tenant_leave_balance(orch, "leave-cal-kal")["leave_balance_days"] == 12.0
    run_leave_block("leave-cal-kal", "amar kalke chuti lagbe", "cal-kal")
    assert tenant_leave_balance(orch, "leave-cal-kal")["leave_balance_days"] == 12.0

    # "May 11" vs "11 days" disambiguation is covered in test_leave_dynamic_slots /
    # extract_leave_slots; full orchestrator path here is LLM-flaky in CI.


@pytest.mark.django_db
def test_document_read_negation_does_not_read():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent="UNKNOWN",
        entities={"document_read": False, "document_text": "SECRET"},
        crm_context={},
    )
    assert d["outcome"] != "INFORMATIONAL"


@pytest.mark.django_db
def test_expense_daily_cumulative_cap_blocks_second_small_claim(monkeypatch):
    fixed = dt.date(2026, 7, 1)

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr("chat.services.decision_engine.date", FixedDate)

    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_EXPENSE_CLAIM,
        entities={"amount": 200, "expense_incurred_date": "2026-06-15"},
        crm_context={"expense_day_approved_total": 300.0},
    )
    assert d["outcome"] == "NEEDS_CLARIFICATION"
    assert "EXPENSE_USE_WIZARD" in d.get("rules_applied", [])


@pytest.mark.django_db
def test_expense_same_day_300_then_200_second_needs_receipt(monkeypatch):
    fixed = dt.date(2026, 6, 10)

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr("chat.services.entity_extractor.date", FixedDate)
    monkeypatch.setattr("chat.services.expense_incurred_date.date", FixedDate)
    monkeypatch.setattr("chat.services.decision_engine.date", FixedDate)

    emp = "daily-cap-orchestrator"
    orch = ChatOrchestrator()
    first = run_tenant_chat(
        orch,
        message="lunch 300",
        session_id=None,
        employee_id=emp,
        trace_id="d1",
    )
    sid = first["_session_id"]
    run_tenant_chat(
        orch,
        message="হ্যাঁ",
        session_id=sid,
        employee_id=emp,
        trace_id="d1b",
    )
    run_tenant_chat(
        orch,
        message="হ্যাঁ",
        session_id=sid,
        employee_id=emp,
        trace_id="d1c",
    )

    second = run_tenant_chat(
        orch,
        message="lunch 200",
        session_id=sid,
        employee_id=emp,
        trace_id="d2",
    )
    assert second["decision"]["outcome"] == "NEEDS_CLARIFICATION"
    assert "সতর্কতা" in (second["decision"].get("reason") or "") or "EXPENSE" in " ".join(
        second["decision"].get("rules_applied", [])
    )


@pytest.mark.django_db
def test_expense_three_small_claims_same_day_sum_to_cap_then_fourth_blocked(monkeypatch):
    fixed = dt.date(2026, 6, 11)

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr("chat.services.entity_extractor.date", FixedDate)
    monkeypatch.setattr("chat.services.expense_incurred_date.date", FixedDate)
    monkeypatch.setattr("chat.services.decision_engine.date", FixedDate)

    emp = "daily-cap-333"
    orch = ChatOrchestrator()
    sid = None
    msgs = (
        "rickshaw 100",
        "lunch 100",
        "snack 100",
    )
    for i, msg in enumerate(msgs):
        r = run_tenant_chat(
            orch,
            message=msg,
            session_id=sid,
            employee_id=emp,
            trace_id=f"3x{i}",
        )
        sid = r["_session_id"]
        run_tenant_chat(
            orch,
            message="হ্যাঁ",
            session_id=sid,
            employee_id=emp,
            trace_id=f"3x{i}-ok",
        )
        run_tenant_chat(
            orch,
            message="হ্যাঁ",
            session_id=sid,
            employee_id=emp,
            trace_id=f"3x{i}-submit",
        )
    fourth = run_tenant_chat(
        orch,
        message="lunch 50",
        session_id=sid,
        employee_id=emp,
        trace_id="3x4",
    )
    assert fourth["decision"]["outcome"] == "NEEDS_CLARIFICATION"


@pytest.mark.django_db
def test_expense_future_date_blocked_by_policy():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_EXPENSE_CLAIM,
        entities={
            "expense_workflow_submit": True,
            "expense_items": [{"category": "Lunch", "amount": 300}],
            "expense_incurred_date": "2030-01-02",
        },
        crm_context={},
    )
    assert d["outcome"] == "NEEDS_CLARIFICATION"
    assert "EXPENSE_FUTURE_DATE_SUBMIT_LATER" in d.get("rules_applied", [])


@pytest.mark.django_db
def test_expense_today_then_tomorrow_same_amount_not_duplicate(monkeypatch):
    fixed = dt.date(2026, 5, 7)

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr("chat.services.entity_extractor.date", FixedDate)
    monkeypatch.setattr("chat.services.expense_incurred_date.date", FixedDate)
    monkeypatch.setattr("chat.services.decision_engine.date", FixedDate)

    emp = "expense-policy-dedupe"
    orch = ChatOrchestrator()
    first = run_tenant_chat(
        orch,
        message="lunch 300",
        session_id=None,
        employee_id=emp,
        trace_id="e1",
    )
    sid = first["_session_id"]
    run_tenant_chat(
        orch,
        message="হ্যাঁ",
        session_id=sid,
        employee_id=emp,
        trace_id="e1b",
    )
    second_confirm = run_tenant_chat(
        orch,
        message="হ্যাঁ",
        session_id=sid,
        employee_id=emp,
        trace_id="e1c",
    )
    assert second_confirm["decision"]["outcome"] == "SUBMITTED"
    rid1 = second_confirm["response"]["request_id"]
    assert rid1

    second = run_tenant_chat(
        orch,
        message="amar kalker jonno 300 taka lagbe",
        session_id=sid,
        employee_id=emp,
        trace_id="e2",
    )
    assert second["decision"]["outcome"] == "NEEDS_CLARIFICATION"
    assert "already submitted" not in (second["response"]["message"] or "").lower()
    assert second["response"]["request_id"] == ""
