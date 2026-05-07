import datetime as dt

import pytest

from chat.constants import (
    INTENT_ATTENDANCE_CORRECTION,
    INTENT_EXPENSE_CLAIM,
    INTENT_LEAVE_REQUEST,
)
from chat.services.decision_engine import DecisionEngine
from chat.services.leave_days import compute_requested_leave_days
from chat.services.orchestrator import ChatOrchestrator


@pytest.mark.django_db
def test_expense_auto_threshold():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_EXPENSE_CLAIM,
        entities={"amount": 100},
        crm_context={},
    )
    assert d["outcome"] == "AUTO_APPROVED"


@pytest.mark.django_db
def test_expense_pending_over_threshold():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_EXPENSE_CLAIM,
        entities={"amount": 400},
        crm_context={},
    )
    assert d["outcome"] == "NEEDS_CLARIFICATION"


@pytest.mark.django_db
def test_expense_over_threshold_with_receipt_routes_to_hr():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_EXPENSE_CLAIM,
        entities={"amount": 400, "document_text": "Uber Trip Total 400 BDT"},
        crm_context={},
    )
    assert d["outcome"] == "PENDING_APPROVAL"
    assert d.get("route_to") == "HR"


@pytest.mark.django_db
def test_expense_receipt_amount_mismatch_pending_hr():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_EXPENSE_CLAIM,
        entities={"amount": 500, "document_text": "Uber receipt total 300"},
        crm_context={},
    )
    assert d["outcome"] == "PENDING_APPROVAL"
    assert d.get("route_to") == "HR"


@pytest.mark.django_db
def test_leave_approved_with_balance():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_LEAVE_REQUEST,
        entities={"start_date": "2026-05-10", "days": 1},
        crm_context={"leave_balance_days": 5},
    )
    assert d["outcome"] == "APPROVED"


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
    orch = ChatOrchestrator()
    first = orch.run_chat(
        message="amar expense lagbe 300 taka",
        session_id=None,
        employee_id="demo-employee",
        trace_id="t1",
    )
    sid = first.get("_session_id") or first.get("session_id")
    assert sid
    rid1 = first["response"]["request_id"]
    assert rid1

    second = orch.run_chat(
        message="amar abar expense lagbe 300",
        session_id=str(sid),
        employee_id="demo-employee",
        trace_id="t2",
    )
    rid2 = second["response"]["request_id"]
    assert rid2 == rid1
    assert "already submitted" in (second["response"]["message"] or "").lower()


@pytest.mark.django_db
def test_leave_request_duplicate_is_deduped_by_orchestrator():
    emp = "leave-dedupe-pytest"
    orch = ChatOrchestrator()
    first = orch.run_chat(
        message="amar kalke chuti lagbe",
        session_id=None,
        employee_id=emp,
        trace_id="lv1",
    )
    sid = first.get("_session_id")
    assert sid
    rid1 = first["response"]["request_id"]
    assert rid1
    bal_after_first = orch.crm.get_leave_balance(emp)["leave_balance_days"]

    second = orch.run_chat(
        message="amar abar kalke chuti lagbe",
        session_id=str(sid),
        employee_id=emp,
        trace_id="lv2",
    )
    assert second["response"]["request_id"] == rid1
    assert "already submitted" in (second["response"]["message"] or "").lower()
    assert orch.crm.get_leave_balance(emp)["leave_balance_days"] == bal_after_first


def test_compute_requested_leave_days_range_and_single_day():
    assert (
        compute_requested_leave_days(
            {"start_date": "2026-05-10", "end_date": "2026-05-12"}
        )
        == 3.0
    )
    assert compute_requested_leave_days({"start_date": "2026-05-11", "days": None}) == 1.0


@pytest.mark.django_db
def test_banglish_may_11_calendar_means_one_day_not_eleven(monkeypatch):
    fixed = dt.date(2026, 5, 7)

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr("chat.services.entity_extractor.date", FixedDate)

    emp = "leave-cal-pytest"
    orch = ChatOrchestrator()
    first = orch.run_chat(
        message="amar kalke chuti lagbe",
        session_id=None,
        employee_id=emp,
        trace_id="cal1",
    )
    sid = first["_session_id"]
    assert orch.crm.get_leave_balance(emp)["leave_balance_days"] == 11.0

    orch.run_chat(
        message="amar may er 11 tarik chuti lagbe",
        session_id=sid,
        employee_id=emp,
        trace_id="cal2",
    )
    assert orch.crm.get_leave_balance(emp)["leave_balance_days"] == 10.0
