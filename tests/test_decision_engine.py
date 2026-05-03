import pytest

from chat.constants import (
    INTENT_ATTENDANCE_CORRECTION,
    INTENT_EXPENSE_CLAIM,
    INTENT_LEAVE_REQUEST,
)
from chat.services.decision_engine import DecisionEngine


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
    assert d["outcome"] == "PENDING_APPROVAL"


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
