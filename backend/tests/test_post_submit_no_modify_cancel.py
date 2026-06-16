"""Submitted leave/expense cannot be edited or cancelled in chat."""

from __future__ import annotations

from chat.constants import INTENT_EXPENSE_STATUS
from chat.services.expense.session_action_memory import has_expense_submission_lock
from chat.services.expense.wizard_commands import wants_cancel_expense_command
from chat.services.leave_fsm import mark_submitted
from chat.services.leave_meta_queries import (
    build_leave_session_summary_message,
    wants_cancel_leave_command,
)
from chat.services.session_snapshot import build_session_snapshot
from chat.services.session_turn_router import route_session_turn


def _submitted_expense_wf() -> dict:
    return {
        "expense_last_submission": {
            "reference_id": "EXP-2026-TEST",
            "items": [
                {"category": "Bus", "amount": 120.0, "from_location": "mirpur", "to_location": "badda"},
                {"category": "Lunch", "amount": 150.0},
            ],
            "incurred_date_iso": "2026-06-14",
        },
        "bot_action_log": [
            {
                "action_type": "expense_submitted",
                "reference_id": "EXP-2026-TEST",
            }
        ],
    }


def _submitted_leave_wf() -> dict:
    return mark_submitted(
        {},
        draft={
            "leave_type": "annual",
            "day_scope": "half",
            "start_date": "2026-06-16",
            "end_date": "2026-06-16",
            "reason": "family program",
        },
        submission_id="PHP-LEAVE-TEST",
    )


def test_cancel_the_expense_matches_after_submit() -> None:
    assert wants_cancel_expense_command("cancel the expense")
    assert wants_cancel_expense_command("cancel expense")
    assert wants_cancel_leave_command("cancel the leave")
    assert wants_cancel_leave_command("cancel the leave request")
    assert wants_cancel_leave_command("okay now cancel the leave request")


def test_router_blocks_post_submit_leave_summary() -> None:
    wf = _submitted_leave_wf()
    snap = build_session_snapshot("leave summery ta daw", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason == "P42_leave_summary"
    assert decision.handler_id == "leave_meta_queries"


def test_router_blocks_post_submit_leave_cancel_request_phrase() -> None:
    wf = _submitted_leave_wf()
    snap = build_session_snapshot("cancel the leave request", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason == "P47_post_submit_leave_cancel_blocked"
    assert "block_message" in (decision.flags or {})


def test_router_blocks_post_submit_expense_cancel() -> None:
    wf = _submitted_expense_wf()
    snap = build_session_snapshot("cancel the expense", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason in (
        "P48b_post_submit_expense_cancel_blocked",
        "P48_post_submit_edit_blocked",
    )
    assert decision.intent == INTENT_EXPENSE_STATUS
    assert "block_message" in (decision.flags or {})


def test_router_blocks_post_submit_leave_cancel() -> None:
    wf = _submitted_leave_wf()
    snap = build_session_snapshot("cancel the leave", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason in (
        "P47_post_submit_leave_cancel_blocked",
        "P47_post_submit_leave_edit_blocked",
    )
    assert "block_message" in (decision.flags or {})


def test_router_blocks_post_submit_expense_modify() -> None:
    wf = _submitted_expense_wf()
    snap = build_session_snapshot("bus modify kore 120 koro", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason == "P48_post_submit_edit_blocked"


def test_router_blocks_post_submit_leave_modify() -> None:
    wf = _submitted_leave_wf()
    snap = build_session_snapshot("date change koro", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason == "P47_post_submit_leave_edit_blocked"


def test_submitted_leave_summary_is_rich() -> None:
    wf = _submitted_leave_wf()
    msg = build_leave_session_summary_message(wf)
    assert "জমা দেওয়া ছুটির সারাংশ" in msg
    assert "PHP-LEAVE-TEST" in msg
    assert "পর্যালোচনা" in msg or "Select Leave" in msg
    assert "স্ট্যাটাস" in msg
