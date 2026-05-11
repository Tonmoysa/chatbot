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
    assert d["outcome"] == "APPROVED"


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
    assert d["outcome"] == "PENDING_APPROVAL"
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
    assert d["outcome"] == "PENDING_APPROVAL"
    assert d.get("route_to") == "MANAGER"


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
            session=session,
            context_lines=ctx,
            intent="EXPENSE_CLAIM",
            entities=entities,
            decision=decision,
            user_message="amar expense lagbe 100 taka",
        )
        == rid1
    )


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
    chain = (
        "amar kalke chuti lagbe",
        "paid",
        "full day",
        "cousin graduation out of Dhaka.",
    )

    sid = None
    last = None
    for i, msg in enumerate(chain):
        last = orch.run_chat(
            message=msg,
            session_id=sid,
            employee_id=emp,
            trace_id=f"lvwiz-{i}",
        )
        sid = last["_session_id"]
        if i < len(chain) - 1:
            assert (
                last["decision"]["outcome"] == "NEEDS_CLARIFICATION"
            ), last["decision"]

    rid1 = last["response"]["request_id"]
    assert rid1 and last["decision"]["outcome"] == "APPROVED"

    bal_after_first = orch.crm.get_leave_balance(emp)["leave_balance_days"]

    dup_chain = (
        "amar abar kalke chuti lagbe",
        "paid",
        "full day",
        "cousin graduation out of Dhaka.",
    )
    for i, msg in enumerate(dup_chain):
        last2 = orch.run_chat(
            message=msg,
            session_id=sid,
            employee_id=emp,
            trace_id=f"lvwiz-dup-{i}",
        )
        sid = last2["_session_id"]

    assert "আগেই জমা" in (last2["response"]["message"] or "")
    assert orch.crm.get_leave_balance(emp)["leave_balance_days"] == bal_after_first


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


@pytest.mark.django_db
def test_expense_day_summary_shows_totals_and_remaining(monkeypatch):
    fixed = dt.date(2026, 6, 15)

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return fixed

    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.expense_incurred_date.date",
        "chat.services.decision_engine.date",
        "chat.services.orchestrator.date",
    ):
        monkeypatch.setattr(mod, FixedDate)

    emp = "expense-summary-pytest-unique"
    orch = ChatOrchestrator()
    sid = None
    for i, msg in enumerate(
        (
            "amar ajke 50 taka tea",
            "amar ajke 120 taka lunch",
        )
    ):
        r = orch.run_chat(
            message=msg,
            session_id=sid,
            employee_id=emp,
            trace_id=f"es-claim-{i}",
        )
        assert r["decision"]["outcome"] == "AUTO_APPROVED", r
        sid = r["_session_id"]

    summ = orch.run_chat(
        message="Today I forgot how much I spent — show me a summary.",
        session_id=sid,
        employee_id=emp,
        trace_id="es-sum",
    )
    assert summ["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    assert summ["decision"]["outcome"] == "INFORMATIONAL"
    body = summ["response"]["message"] or ""
    assert "170" in body
    assert "130" in body
    assert "MOCK-" in body


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

    emp = "leave-cal-pytest"
    orch = ChatOrchestrator()

    def run_leave_block(start_msg: str, trace_prefix: str) -> None:
        sid_local = None
        steps = (
            start_msg,
            "paid",
            "full day",
            "Travel to village for ceremonies.",
        )
        last_local = None
        for i, msg in enumerate(steps):
            last_local = orch.run_chat(
                message=msg,
                session_id=sid_local,
                employee_id=emp,
                trace_id=f"{trace_prefix}-{i}",
            )
            sid_local = last_local["_session_id"]
            if i < len(steps) - 1:
                assert last_local["decision"]["outcome"] == "NEEDS_CLARIFICATION"
        assert last_local["decision"]["outcome"] == "APPROVED"

    assert orch.crm.get_leave_balance(emp)["leave_balance_days"] == 12.0
    run_leave_block("amar kalke chuti lagbe", "cal-kal")
    assert orch.crm.get_leave_balance(emp)["leave_balance_days"] == 11.0

    run_leave_block("amar may er 11 tarik chuti lagbe", "cal-may")
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
