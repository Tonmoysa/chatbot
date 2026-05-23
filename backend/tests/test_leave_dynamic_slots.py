"""Dynamic leave slot-filling — only missing fields are asked."""

import datetime as dt

import pytest

from chat.services.leave_slot_extraction import extract_leave_slots
from chat.services.leave_slots import get_missing_slots, prefill_draft_from_extraction
from chat.services.leave_workflow import process_leave_turn


def _draft_from_message(msg: str) -> dict:
    ex = extract_leave_slots(msg)
    draft: dict = {}
    prefill_draft_from_extraction(draft, ex)
    return draft


def test_tomarrow_typo_maps_to_tomorrow(monkeypatch):
    fixed = dt.date(2026, 5, 20)

    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    ex = extract_leave_slots("i need a leave tomarrow for sickness")
    assert ex.start_date.confidence == "high"
    assert ex.start_date.value == "2026-05-21"
    assert ex.leave_type.value == "sick"


def test_kalke_sick_leave_prefills_type_and_date():
    draft = _draft_from_message("ami kalke sick leave nite chai")
    assert draft.get("leave_type") == "sick"
    assert draft.get("start_date")
    assert draft.get("reason")  # implied
    missing = get_missing_slots(draft)
    assert "leave_dates" not in missing
    assert "leave_type" not in missing
    assert "reason" not in missing
    assert "leave_payment_category" in missing
    assert "day_scope" in missing


def test_agamikal_paid_leave_skips_date_and_reason():
    draft = _draft_from_message("আগামীকাল paid leave চাই")
    assert draft.get("start_date")
    assert draft.get("leave_payment_category") == "paid"
    missing = get_missing_slots(draft)
    assert "leave_dates" not in missing
    assert "leave_payment_category" not in missing
    assert "leave_type" in missing
    assert "day_scope" in missing


def test_next_week_three_days_duration():
    draft = _draft_from_message("next week 3 diner chuti lagbe")
    assert draft.get("days") == 3.0 or draft.get("start_date")
    missing = get_missing_slots(draft)
    assert "leave_dates" not in missing or draft.get("end_date")


def test_today_half_day_casual():
    draft = _draft_from_message("ajke half day casual leave")
    assert draft.get("leave_type") == "casual"
    assert draft.get("day_scope") == "half"
    missing = get_missing_slots(draft)
    assert "day_scope" not in missing
    assert "leave_dates" not in missing
    assert "leave_payment_category" in missing


def test_full_paid_sick_single_turn_almost_complete():
    draft = _draft_from_message("agamikal full day paid sick leave chai")
    assert draft.get("leave_type") == "sick"
    assert draft.get("leave_payment_category") == "paid"
    assert draft.get("day_scope") == "full"
    assert draft.get("start_date")
    missing = get_missing_slots(draft)
    assert missing == [] or missing == ["supporting_document"]


def test_vague_date_triggers_clarification():
    ex = extract_leave_slots("next week maybe leave lagbe")
    assert ex.vague_date
    draft: dict = {}
    prefill_draft_from_extraction(draft, ex)
    missing = get_missing_slots(draft, extraction=ex)
    assert "date_clarification" in missing or "leave_dates" in missing


@pytest.mark.django_db
def test_wizard_asks_payment_then_scope_after_sick_phrase(monkeypatch):
    """Explicit sick + date + implied reason still requires paid/unpaid and scope."""
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
    assert not r1["complete"]
    q1 = r1.get("question") or ""
    assert "বেতন" in q1 or "paid" in q1.lower()
    assert "প্রথম প্রশ্ন" not in q1
    assert "১/৫" not in q1

    r2 = process_leave_turn(
        workflow_state=r1["workflow_state"],
        message="paid",
        entities={},
        company_id="company-a",
    )
    assert not r2["complete"]
    q2 = r2.get("question") or ""
    assert "হাফ" in q2 or "পুরো" in q2 or "half" in q2.lower() or "full" in q2.lower()

    r3 = process_leave_turn(
        workflow_state=r2["workflow_state"],
        message="full",
        entities={},
        company_id="company-a",
    )
    assert not r3["complete"]
    assert not r3.get("confirmed_submit")
    assert "জমা দেবেন" in (r3.get("question") or "") or "yes" in (r3.get("question") or "").lower()

    r4 = process_leave_turn(
        workflow_state=r3["workflow_state"],
        message="yes",
        entities={},
        company_id="company-a",
    )
    assert r4["complete"]
    assert r4.get("confirmed_submit")
    assert r4["merged_entities"].get("leave_type") == "sick"
    assert r4["merged_entities"].get("reason")


@pytest.mark.django_db
def test_wizard_partial_message_collects_type_payment_scope_reason(monkeypatch):
    """Vague chuti message: ask leave type, paid/unpaid, scope, then reason."""
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
        message="ami kalke chuti lagbe",
        entities={},
        company_id="company-a",
    )
    assert not r1["complete"]
    q1 = r1["question"] or ""
    assert "প্রথম প্রশ্ন" not in q1
    assert "১/৫" not in q1

    r2 = process_leave_turn(
        workflow_state=r1["workflow_state"],
        message="casual",
        entities={},
        company_id="company-a",
    )
    assert not r2["complete"]

    r3 = process_leave_turn(
        workflow_state=r2["workflow_state"],
        message="paid",
        entities={},
        company_id="company-a",
    )
    assert not r3["complete"]

    r4 = process_leave_turn(
        workflow_state=r3["workflow_state"],
        message="full",
        entities={},
        company_id="company-a",
    )
    assert not r4["complete"]

    r5 = process_leave_turn(
        workflow_state=r4["workflow_state"],
        message="family kajer jonno",
        entities={},
        company_id="company-a",
    )
    assert not r5["complete"]
    assert "জমা দেবেন" in (r5.get("question") or "")

    r6 = process_leave_turn(
        workflow_state=r5["workflow_state"],
        message="yes",
        entities={},
        company_id="company-a",
    )
    assert r6["complete"]
    assert r6.get("confirmed_submit")
    assert r6["merged_entities"].get("reason")
