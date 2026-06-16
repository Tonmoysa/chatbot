"""Bus route must not be re-requested after user already supplied From/To."""

from __future__ import annotations

from chat.constants import INTENT_EXPENSE_CLAIM
from chat.services.expense.pending_routes import (
    consolidate_incomplete_travel_duplicates,
    prepare_draft_items_for_submit,
    try_apply_pending_routes,
)
from chat.services.expense_workflow import _try_enter_submit_confirm, process_expense_turn
from chat.services.session_turn_router import SessionTurnDecision, TurnKind


def test_try_apply_route_updates_committed_bus_line() -> None:
    block = {
        "active": True,
        "stage": "collecting",
        "pending_step": "from_to",
        "pending_line": {
            "category": "Bus",
            "amount": 100.0,
        },
        "incurred_date_iso": "2026-06-15",
    }
    items = [
        {"category": "Lunch", "amount": 100.0},
        {"category": "Bus", "amount": 100.0},
    ]
    result = try_apply_pending_routes(block, items, "mirpur to badda")
    bus_rows = [r for r in result.items if str(r.get("category")).lower() == "bus"]
    assert len(bus_rows) == 1
    assert bus_rows[0].get("from_location") == "mirpur"
    assert bus_rows[0].get("to_location") == "badda"
    assert "pending_line" not in block


def test_consolidate_drops_route_less_bus_when_routed_bus_exists() -> None:
    items = [
        {"category": "Lunch", "amount": 100.0},
        {"category": "Bus", "amount": 100.0},
        {
            "category": "Bus",
            "amount": 100.0,
            "from_location": "mirpur",
            "to_location": "badda",
        },
    ]
    cleaned = consolidate_incomplete_travel_duplicates(items)
    bus_rows = [r for r in cleaned if str(r.get("category")).lower() == "bus"]
    assert len(bus_rows) == 1
    assert bus_rows[0].get("from_location") == "mirpur"


def test_expense_submit_after_route_not_blocked() -> None:
    block = {
        "active": True,
        "stage": "collecting",
        "incurred_date_iso": "2026-06-15",
        "reply_language": "en",
    }
    items = [
        {"category": "Lunch", "amount": 100.0},
        {"category": "Bus", "amount": 100.0},
        {
            "category": "Bus",
            "amount": 100.0,
            "from_location": "mirpur",
            "to_location": "badda",
        },
    ]
    items = prepare_draft_items_for_submit(block, items)
    pack = _try_enter_submit_confirm(
        {},
        block,
        items,
        message="expense submit",
        inc_iso="2026-06-15",
        day_logged_total=0.0,
        daily_cap=300.0,
        lang="en",
    )
    assert pack is not None
    assert block.get("stage") == "submit_confirm"
    assert "From" not in (pack.get("question") or "")


def test_expense_submit_turn_after_cross_workflow_suspend() -> None:
    wf = {
        "suspended_leave": {
            "draft": {
                "leave_type": "annual",
                "start_date": "2026-06-16",
                "end_date": "2026-06-18",
                "reason": "family program",
            },
            "review_pending": True,
            "status": "active",
        },
        "suspended_expense": {
            "expense_request": {
                "active": True,
                "stage": "collecting",
                "incurred_date_iso": "2026-06-15",
                "items": [
                    {"category": "Lunch", "amount": 100.0},
                    {"category": "Bus", "amount": 100.0},
                    {
                        "category": "Bus",
                        "amount": 100.0,
                        "from_location": "mirpur",
                        "to_location": "badda",
                    },
                ],
            }
        },
    }
    decision = SessionTurnDecision(
        turn_kind=TurnKind.SUBMIT_COMMAND,
        intent=INTENT_EXPENSE_CLAIM,
        target_workflow="expense",
        handler_id="expense.turn_router",
        confidence=0.99,
        reason="P04_expense_submit_command",
    )
    pack = process_expense_turn(
        workflow_state=wf,
        message="expense submit",
        router_decision=decision,
    )
    block = (pack.get("workflow_state") or {}).get("expense_request") or {}
    assert block.get("stage") == "submit_confirm"
    question = str(pack.get("question") or "")
    assert "From and To" not in question
