"""Regression tests for transcript session bugs (leave/expense post-submit)."""

from __future__ import annotations

from datetime import date

from chat.services.expense.command_executor import execute_correction_plan
from chat.services.expense.command_parser import parse_correction_plan
from chat.services.leave.date_correction import try_apply_leave_end_date_only
from chat.services.leave_fsm import mark_submitted
from chat.services.session_snapshot import build_session_snapshot
from chat.services.session_turn_router import TurnKind, route_session_turn

FIXED_TODAY = date(2026, 6, 15)


def _route(message: str, wf: dict | None = None):
    state = wf or {}
    snap = build_session_snapshot(
        message,
        workflow_state=state,
        today=FIXED_TODAY,
    )
    return route_session_turn(snap, workflow_state=state)


def test_leave_end_date_only_correction_keeps_start():
    draft = {"start_date": "2026-08-05", "end_date": "2026-08-07", "days": 3}
    ok = try_apply_leave_end_date_only(
        draft,
        "leave er sesh date 7 august na 8 august hobe",
        today=FIXED_TODAY,
    )
    assert ok is True
    assert draft["start_date"] == "2026-08-05"
    assert draft["end_date"] == "2026-08-08"


def test_leave_end_date_correction_during_suspended_expense(monkeypatch):
    """Regression: end-date fix must not be stomped by compound slot extraction."""
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    from chat.services.leave_workflow import process_leave_turn
    from chat.services.leave_fsm import read_leave_state
    from chat.services.workflow_suspend import suspend_leave_for_workflow_switch

    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {
            "start_date": "2026-08-05",
            "end_date": "2026-08-07",
            "reason": "gram e jawar jonno",
            "day_scope": "full",
            "days": 3,
        },
        "step": "leave_type",
        "review_pending": False,
    }
    wf = suspend_leave_for_workflow_switch(wf)
    wf["expense_request"] = {
        "active": True,
        "stage": "collecting",
        "items": [{"category": "Lunch", "amount": 180}],
    }
    decision = _route("leave er sesh date 7 august na, 8 august hobe", wf)
    assert decision.reason == "P11_suspended_leave_correction"
    pack = process_leave_turn(
        workflow_state=wf,
        message="leave er sesh date 7 august na, 8 august hobe",
        entities={},
        company_id="default",
        router_decision=decision,
    )
    draft = read_leave_state(pack["workflow_state"]).get("draft") or {}
    assert draft["start_date"] == "2026-08-05"
    assert draft["end_date"] == "2026-08-08"
    assert float(draft.get("days") or 0) == 4.0


def test_new_leave_after_submit_routes_p49b():
    draft = {
        "start_date": "2026-08-05",
        "end_date": "2026-08-07",
        "leave_type": "annual",
        "days": 3.0,
    }
    wf = mark_submitted(
        {},
        draft=draft,
        submission_id="PHP-LEAVE-AUG",
    )
    decision = _route("abar 5 august theke 8 august leave chai", wf)
    assert decision.turn_kind == TurnKind.NEW_LEAVE
    assert decision.reason == "P50c_new_leave_cold_start"


def test_post_submit_expense_resume_blocked_p48c():
    wf = {
        "expense_last_submission": {"reference_id": "EXP-2026-001", "items": []},
        "expense_request": {"active": False, "items": []},
    }
    decision = _route("expense e back koro", wf)
    assert decision.reason == "P48c_post_submit_expense_resume_blocked"
    assert decision.flags.get("block_message")


def test_expense_claim_during_leave_type_step_switches_p51():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {
            "start_date": "2026-08-05",
            "end_date": "2026-08-07",
            "reason": "family function",
            "day_scope": "full",
        },
        "step": "leave_type",
        "review_pending": False,
    }
    decision = _route(
        "ajke sokale mirpur theke motijheel bus e 120 taka khoroch hoise",
        wf,
    )
    assert decision.turn_kind == TurnKind.WORKFLOW_SWITCH
    assert decision.reason == "P51_switch_to_expense"
    assert decision.target_workflow == "expense"


def test_lunch_na_amount_routes_to_edit_not_add_lines():
    from chat.services.expense.turn_parser import parse_turn_rules
    from chat.services.expense.turn_schema import TURN_EDIT_DRAFT

    items = [
        {"category": "Bus", "amount": 120.0},
        {"category": "Lunch", "amount": 180.0},
    ]
    decision = parse_turn_rules(
        "lunch 180 na 220 taka hobe",
        items=items,
        stage="submit_confirm",
        pending_step="",
        has_pending_line=False,
        block={"stage": "submit_confirm"},
    )
    assert decision.turn_type == TURN_EDIT_DRAFT
    assert decision.plan.amount_replacements == [(220.0, 180.0)]


def test_ordinal_na_pattern_picks_new_amount():
    from chat.services.expense.command_parser import parse_ordinal_amount_correction

    parsed = parse_ordinal_amount_correction(
        "prothom expense 120 na, 140 taka hobe",
        item_count=4,
    )
    assert parsed == (0, 140.0)


def test_ordinal_amount_correction_does_not_duplicate_lines():
    items = [
        {"category": "snack", "amount": 120.0, "line_id": "l1"},
        {"category": "bus", "amount": 50.0, "line_id": "l2"},
    ]
    block: dict = {"items": items}
    plan = parse_correction_plan(
        "prothom expense 120 na 140",
        item_count=len(items),
    )
    assert plan.update_amount_by_index == (0, 140.0)
    assert not plan.amount_replacements
    result = execute_correction_plan(items, plan, block=block)
    assert result.changed
    assert len(result.items) == 2
    assert result.items[0]["amount"] == 140.0
    assert result.items[1]["amount"] == 50.0
