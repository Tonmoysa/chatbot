"""Leave conversation manager — acknowledgment + batched prompts."""

import datetime as dt

import pytest

from chat.services.leave.conversation_manager import LeaveConversationManager
from chat.services.leave_slots import (
    SLOT_DATES,
    SLOT_PAYMENT,
    SLOT_REASON,
    SLOT_SCOPE,
    generate_question,
)
from chat.services.leave_workflow import process_leave_turn


def test_acknowledges_date_and_asks_payment_scope_batched():
    mgr = LeaveConversationManager()
    draft = {
        "start_date": "2026-06-10",
        "end_date": "2026-06-10",
        "reason": "family program",
    }
    missing = [SLOT_PAYMENT, SLOT_SCOPE]
    q = mgr.build_follow_up(
        draft,
        primary_slot=SLOT_PAYMENT,
        missing=missing,
    )
    assert "২০২৬-০৬-১০" in q or "2026-06-10" in q
    assert "family program" in q
    assert "Paid" in q
    assert "Full Day" in q
    assert "Half Day" in q
    assert "কোন তারিখ" not in q
    assert "Reason টা" not in q


def test_scope_only_after_payment_ack():
    mgr = LeaveConversationManager()
    draft = {
        "start_date": "2026-06-10",
        "leave_payment_category": "paid",
    }
    missing = [SLOT_SCOPE]
    q = mgr.build_follow_up(
        draft,
        primary_slot=SLOT_SCOPE,
        missing=missing,
    )
    assert "Paid" in q
    assert "Full Day" in q
    assert "Half Day" in q
    assert "Select Leave" not in q


def test_reason_ask_when_only_reason_missing():
    mgr = LeaveConversationManager()
    draft = {
        "start_date": "2026-06-10",
        "leave_payment_category": "paid",
        "day_scope": "full",
    }
    missing = [SLOT_REASON]
    q = mgr.build_follow_up(
        draft,
        primary_slot=SLOT_REASON,
        missing=missing,
    )
    assert "Reason টা" in q
    assert "২০২৬-০৬-১০" in q or "2026-06-10" in q


def test_generate_question_delegates_with_missing_param():
    draft = {"start_date": "2026-06-11", "reason": "fever"}
    q = generate_question(
        SLOT_PAYMENT,
        draft,
        remaining=2,
        missing=[SLOT_PAYMENT, SLOT_SCOPE],
    )
    assert "fever" in q
    assert "Paid" in q
    assert "Full Day" in q


@pytest.mark.django_db
def test_wizard_after_sick_date_asks_payment_not_date(monkeypatch):
    fixed = dt.date(2026, 5, 7)

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)
    monkeypatch.setattr("chat.services.entity_extractor.date", FixedDate)

    wf: dict = {}
    r1 = process_leave_turn(
        workflow_state=wf,
        message="ami kalke sick leave nite chai",
        entities={},
        company_id="company-a",
    )
    q1 = r1.get("question") or ""
    assert "কোন তারিখ" not in q1
    assert "বেতন" in q1 or "paid" in q1.lower() or "Paid" in q1
    assert "2026-05-08" in q1 or "ছুটির তারিখ" in q1
