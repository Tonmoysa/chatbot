"""Structured prompt context + P79 slot-first routing."""

from __future__ import annotations

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM, INTENT_LEAVE_REQUEST, INTENT_UNKNOWN
from chat.services.session_expected_answer import (
    KIND_DATE,
    KIND_NONE,
    derive_prompt_context_fields,
    message_plausibly_answers_prompt,
)
from chat.services.session_snapshot import build_session_snapshot
from chat.services.session_turn_router import TurnKind, route_session_turn


def _leave_wf(*, step: str, draft: dict | None = None) -> dict:
    return {
        "active_flow": "leave",
        "status": "active",
        "draft": dict(draft or {"reason": "family program", "days": 3.0}),
        "step": step,
        "review_pending": False,
    }


def _route(message: str, wf: dict):
    snap = build_session_snapshot(message, workflow_state=wf)
    return route_session_turn(snap, workflow_state=wf), snap


def test_snapshot_derives_leave_dates_prompt():
    snap = build_session_snapshot("3 din", workflow_state=_leave_wf(step="leave_dates"))
    assert snap.active_prompt_domain == "leave"
    assert snap.active_prompt_slot == "leave_dates"
    assert snap.expected_answer_kind == KIND_DATE
    assert snap.has_pending_prompt


def test_duration_answers_pending_leave_dates():
    snap = build_session_snapshot("7 days", workflow_state=_leave_wf(step="leave_dates"))
    assert message_plausibly_answers_prompt("7 days", snap)
    assert message_plausibly_answers_prompt("3 din", snap)


def test_p79_slot_first_beats_balance_heuristic():
    decision, snap = _route("7 days", _leave_wf(step="leave_dates"))
    assert decision.turn_kind == TurnKind.SLOT_ANSWER
    assert decision.intent == INTENT_LEAVE_REQUEST
    assert decision.reason.startswith("P79_slot_first_leave")
    assert snap.expected_answer_kind == KIND_DATE


def test_cold_start_seven_days_still_clarifies_not_slot():
    decision, snap = _route("7 days", {})
    assert snap.expected_answer_kind == KIND_NONE
    assert decision.turn_kind == TurnKind.CONTEXT_CLARIFICATION
    assert decision.reason.startswith("P21")


def test_reason_slot_accepts_free_text():
    ctx = derive_prompt_context_fields(
        leave_active=True,
        pending_leave_step="reason",
    )
    assert ctx.kind == "free_text"
    snap = build_session_snapshot(
        "family program e jabo",
        workflow_state=_leave_wf(step="reason"),
    )
    assert message_plausibly_answers_prompt("family program e jabo", snap)


def test_leave_application_during_expense_from_to_is_not_slot_answer():
    """Regression: leave date must not bind as Bus from/to (P79 broke P50)."""
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "items": [
                {"amount": 150, "category": "Lunch"},
                {"amount": 50, "category": "Snack"},
                {"amount": 200, "category": "Bus"},
            ],
            "pending_step": "from_to",
            "pending_line": {
                "amount": 200,
                "category": "Bus",
                "from_location": "",
                "to_location": "",
            },
        }
    }
    msg = "agami 15 august leave chai"
    decision, snap = _route(msg, wf)
    assert not message_plausibly_answers_prompt(msg, snap)
    from chat.services.session_turn_router import TurnKind

    assert decision.turn_kind == TurnKind.WORKFLOW_SWITCH
    assert decision.reason.startswith("P50")


def test_expense_clarify_pending_prompt():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "items": [{"category": "Bus", "amount": 100.0}],
            "pending_step": "clarify",
        }
    }
    snap = build_session_snapshot("bus hobe nah bike hobe", workflow_state=wf)
    assert snap.active_prompt_domain == "expense"
    assert snap.active_prompt_slot == "clarify"


@pytest.mark.parametrize(
    "msg,step",
    [
        ("paid", "leave_payment_category"),
        ("full day", "day_scope"),
        ("annual leave", "leave_type"),
    ],
)
def test_enum_slots_match_wizard_tokens(msg: str, step: str):
    snap = build_session_snapshot(msg, workflow_state=_leave_wf(step=step))
    assert message_plausibly_answers_prompt(msg, snap)
