"""Leave confirmation gate — no submit without explicit yes."""

import datetime as dt

import pytest

from chat.services.leave_confirm import (
    build_confirmation_prompt,
    is_confirmation_yes,
    process_confirmation_turn,
)
from chat.services.leave_fsm import is_leave_submission_locked, read_leave_state
from chat.services.leave_fsm import is_awaiting_leave_confirmation
from chat.services.leave_workflow import process_leave_turn


@pytest.mark.django_db
def test_slots_complete_does_not_auto_submit(monkeypatch):
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
    r2 = process_leave_turn(
        workflow_state=r1["workflow_state"],
        message="paid",
        entities={},
        company_id="company-a",
    )
    r3 = process_leave_turn(
        workflow_state=r2["workflow_state"],
        message="full",
        entities={},
        company_id="company-a",
    )
    assert not r3.get("confirmed_submit")
    assert is_awaiting_leave_confirmation(r3["workflow_state"])
    assert "জমা দেবেন" in (r3.get("question") or "")


def test_confirmation_yes_submits_only_after_explicit_yes():
    draft = {
        "leave_type": "sick",
        "leave_payment_category": "paid",
        "day_scope": "full",
        "start_date": "2026-05-08",
        "end_date": "2026-05-08",
        "reason": "fever",
    }
    wf = {
        "active_flow": "leave",
        "status": "active",
        "review_pending": True,
        "draft": draft,
    }
    out = process_confirmation_turn(
        workflow_state=wf,
        message="yes",
        draft=draft,
    )
    assert out["confirmed_submit"] is True
    assert out["complete"] is True
    assert not is_leave_submission_locked(out["workflow_state"])


def test_confirmation_cancel_clears_workflow():
    draft = {"leave_type": "sick", "start_date": "2026-05-08"}
    wf = {
        "leave_request": {
            "active": True,
            "stage": "awaiting_confirmation",
            "draft": draft,
        }
    }
    out = process_confirmation_turn(
        workflow_state=wf,
        message="cancel",
        draft=draft,
    )
    assert out.get("cancelled") is True
    assert read_leave_state(out["workflow_state"]).get("active_flow") is None


def test_build_confirmation_prompt_lists_summary():
    prompt = build_confirmation_prompt(
        {
            "leave_type": "casual",
            "leave_payment_category": "paid",
            "day_scope": "half",
            "start_date": "2026-05-10",
            "reason": "family",
        }
    )
    assert "casual" in prompt
    assert "yes" in prompt.lower()
    assert "edit" in prompt.lower()


def test_is_confirmation_yes():
    assert is_confirmation_yes("yes")
    assert is_confirmation_yes("হ্যাঁ")


def test_compound_paid_sick_full_day_fills_all_slots(monkeypatch):
    fixed = dt.date(2026, 5, 21)

    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)

    wf: dict = {
        "leave_request": {
            "active": True,
            "stage": "collecting",
            "pending_slot": "leave_payment_category",
            "draft": {
                "start_date": "2026-05-22",
                "end_date": "2026-05-22",
            },
        }
    }
    out = process_leave_turn(
        workflow_state=wf,
        message="paid,sick,full day",
        entities={},
        company_id="company-a",
    )
    draft = read_leave_state(out["workflow_state"]).get("draft") or {}
    assert draft.get("leave_payment_category") == "paid"
    assert draft.get("leave_type") == "sick"
    assert draft.get("day_scope") == "full"
    assert "হাফ" not in (out.get("question") or "") or out.get("complete") is False


def test_review_half_day_correction_updates_scope():
    draft = {
        "leave_type": "sick",
        "leave_payment_category": "paid",
        "day_scope": "full",
        "start_date": "2026-05-22",
        "end_date": "2026-05-22",
        "reason": "fever",
    }
    wf = {
        "leave_request": {
            "active": True,
            "stage": "awaiting_confirmation",
            "draft": dict(draft),
        }
    }
    out = process_confirmation_turn(
        workflow_state=wf,
        message="half day hobe",
        draft=dict(draft),
    )
    d = read_leave_state(out["workflow_state"]).get("draft") or {}
    assert d.get("day_scope") == "half"
    assert "হাফ দিন" in (out.get("question") or "")