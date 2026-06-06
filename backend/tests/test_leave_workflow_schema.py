"""Leave workflow schema — missing field detection."""

import datetime as dt

import pytest

from chat.services.leave.workflow_schema import LeaveWorkflowSchema, get_leave_workflow_schema
from chat.services.leave_slots import (
    SLOT_DATES,
    SLOT_PAYMENT,
    SLOT_REASON,
    SLOT_SCOPE,
    get_missing_slots,
)


def test_schema_singleton():
    assert get_leave_workflow_schema() is get_leave_workflow_schema()


def test_schema_missing_payment_and_scope():
    schema = LeaveWorkflowSchema()
    missing = schema.missing_fields({"start_date": "2026-06-10"})
    assert SLOT_PAYMENT in missing
    assert SLOT_SCOPE in missing
    assert SLOT_REASON in missing
    assert SLOT_DATES not in missing


def test_schema_complete_draft():
    schema = LeaveWorkflowSchema()
    draft = {
        "start_date": "2026-06-10",
        "end_date": "2026-06-10",
        "leave_payment_category": "paid",
        "day_scope": "full",
        "reason": "family program",
    }
    assert schema.is_complete(draft)
    assert schema.missing_fields(draft) == []


def test_get_missing_slots_delegates_to_schema(monkeypatch):
    fixed = dt.date(2026, 6, 5)

    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)

    draft = {
        "start_date": "2026-06-10",
        "leave_payment_category": "paid",
        "reason": "fever",
    }
    schema_missing = get_leave_workflow_schema().missing_fields(draft)
    slots_missing = get_missing_slots(draft)

    assert slots_missing == schema_missing == [SLOT_SCOPE]


def test_schema_implied_sick_reason():
    schema = LeaveWorkflowSchema()
    draft = {
        "start_date": "2026-06-10",
        "leave_payment_category": "paid",
        "day_scope": "full",
        "leave_type": "sick",
    }
    missing = schema.missing_fields(draft)
    assert SLOT_REASON not in missing
    assert draft.get("reason")
