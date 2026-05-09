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
        message="amar expense lagbe 100 taka",
        session_id=None,
        employee_id="dedupe-expense-unique",
        trace_id="t1",
    )
    sid = first.get("_session_id") or first.get("session_id")
    assert sid
    rid1 = first["response"]["request_id"]
    assert rid1

    session = orch.memory.get_or_create_session(str(sid), "dedupe-expense-unique")
    ctx = orch.memory.recent_context_lines(session)
    # Simulate the second-turn dedupe decision deterministically.
    entities = orch.entities.extract_rules_only("amar expense lagbe 100 taka", intent="EXPENSE_CLAIM")
    decision = {"outcome": "AUTO_APPROVED"}
    assert (
        orch._recent_duplicate_request_id(
            context_lines=ctx,
            intent="EXPENSE_CLAIM",
            entities=entities,
            decision=decision,
            user_message="amar expense lagbe 100 taka",
        )
        == rid1
    )


@pytest.mark.django_db
def test_leave_request_duplicate_is_deduped_by_orchestrator():
    emp = "leave-dedupe-pytest-unique"
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
    session = orch.memory.get_or_create_session(str(sid), emp)
    ctx = orch.memory.recent_context_lines(session)
    entities2 = orch.entities.extract_rules_only("amar abar kalke chuti lagbe", intent="LEAVE_REQUEST")
    decision2 = {"outcome": "APPROVED"}
    assert (
        orch._recent_duplicate_request_id(
            context_lines=ctx,
            intent="LEAVE_REQUEST",
            entities=entities2,
            decision=decision2,
            user_message="amar abar kalke chuti lagbe",
        )
        == rid1
    )
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
    assert "EXPENSE_DAILY_CAP_EXCEEDED" in " ".join(d.get("rules_applied", []))


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
    first = orch.run_chat(
        message="amar ajke 300 taka cost hoyeche",
        session_id=None,
        employee_id=emp,
        trace_id="d1",
    )
    assert first["decision"]["outcome"] == "AUTO_APPROVED"
    sid = first["_session_id"]

    second = orch.run_chat(
        message="amar ajke 200 taka cost hoyeche",
        session_id=sid,
        employee_id=emp,
        trace_id="d2",
    )
    assert second["decision"]["outcome"] == "NEEDS_CLARIFICATION"
    assert "EXPENSE_DAILY_CAP_EXCEEDED" in " ".join(
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
        "amar ajke 100 taka rickshaw vara hoyeche",
        "amar ajke 100 taka lunch cost hoyeche",
        "amar ajke 100 taka tea stall hoyeche",
    )
    for i, msg in enumerate(msgs):
        r = orch.run_chat(
            message=msg,
            session_id=sid,
            employee_id=emp,
            trace_id=f"3x{i}",
        )
        assert r["decision"]["outcome"] == "AUTO_APPROVED", r
        sid = r["_session_id"]
    fourth = orch.run_chat(
        message="amar ajke 50 taka cost hoyeche",
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
        entities={"amount": 300, "expense_incurred_date": "2030-01-02"},
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
    first = orch.run_chat(
        message="amar ajke 300 taka cost hoyeche",
        session_id=None,
        employee_id=emp,
        trace_id="e1",
    )
    assert first["decision"]["outcome"] == "AUTO_APPROVED"
    sid = first["_session_id"]
    rid1 = first["response"]["request_id"]
    assert rid1

    second = orch.run_chat(
        message="amar kalker jonno 300 taka lagbe",
        session_id=sid,
        employee_id=emp,
        trace_id="e2",
    )
    assert second["decision"]["outcome"] == "NEEDS_CLARIFICATION"
    assert "already submitted" not in (second["response"]["message"] or "").lower()
    assert second["response"]["request_id"] == ""
