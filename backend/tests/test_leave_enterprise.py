"""Enterprise leave workflow: approval tiers, overlap, balance split."""

import datetime as dt

import pytest

from chat.constants import INTENT_LEAVE_REQUEST
from chat.services.crm.mock_crm import MockCRMAdapter
from chat.services.decision_engine import DecisionEngine
from chat.services.leave_approval import LEAVE_STATUS_MANAGER_REVIEW


COMPANY_ID = "company-a"


def _leave_entities(**kwargs):
    base = {
        "leave_payment_category": "paid",
        "day_scope": "full",
        "start_date": "2026-05-10",
        "end_date": "2026-05-10",
        "reason": "Personal errand",
    }
    base.update(kwargs)
    return base


@pytest.mark.django_db
def test_one_day_paid_submitted_to_crm_with_balance():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_LEAVE_REQUEST,
        entities=_leave_entities(),
        crm_context={
            "company_id": COMPANY_ID,
            "employee_id": "e1",
            "leave_balance_days": 5,
            "existing_leave_requests": [],
        },
    )
    assert d["outcome"] == "SUBMITTED"
    assert d.get("leave_status") == "pending"


@pytest.mark.django_db
def test_three_day_paid_submitted_with_manager_hint():
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_LEAVE_REQUEST,
        entities=_leave_entities(
            start_date="2026-05-10",
            end_date="2026-05-12",
            reason="Family visit out of town",
        ),
        crm_context={
            "company_id": COMPANY_ID,
            "employee_id": "e1",
            "leave_balance_days": 10,
            "existing_leave_requests": [],
        },
    )
    assert d["outcome"] == "SUBMITTED"
    assert d.get("route_to") == "MANAGER"
    assert d.get("leave_status") == LEAVE_STATUS_MANAGER_REVIEW


@pytest.mark.django_db
def test_overlap_blocks_duplicate_dates():
    eng = DecisionEngine()
    existing = [
        {
            "request_id": "MOCK-OLD1",
            "company_id": COMPANY_ID,
            "employee_id": "e1",
            "status": "APPROVED",
            "entities": {
                "start_date": "2026-05-10",
                "end_date": "2026-05-10",
            },
        }
    ]
    d = eng.evaluate(
        intent=INTENT_LEAVE_REQUEST,
        entities=_leave_entities(),
        crm_context={
            "company_id": COMPANY_ID,
            "employee_id": "e1",
            "leave_balance_days": 5,
            "existing_leave_requests": existing,
        },
    )
    assert d["outcome"] == "NEEDS_CLARIFICATION"
    assert "LEAVE_OVERLAP_BLOCKED" in d.get("rules_applied", [])


@pytest.mark.django_db
def test_insufficient_balance_split_submitted(monkeypatch):
    eng = DecisionEngine()
    d = eng.evaluate(
        intent=INTENT_LEAVE_REQUEST,
        entities=_leave_entities(
            start_date="2026-05-10",
            end_date="2026-05-12",
            reason="Extended family event",
        ),
        crm_context={
            "company_id": COMPANY_ID,
            "employee_id": "e1",
            "leave_balance_days": 1,
            "existing_leave_requests": [],
        },
    )
    assert d["outcome"] == "SUBMITTED"
    assert d.get("paid_leave_days") == 1.0
    assert d.get("unpaid_leave_days") == 2.0

