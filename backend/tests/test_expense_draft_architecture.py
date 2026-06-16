"""Expense draft view + active prompt + router clear (TURN_ROUTER_SPEC aligned)."""

from __future__ import annotations

from chat.services.expense.active_prompt import KIND_DELETE_PICK, read_active_prompt
from chat.services.expense.delete_disambiguation_pending import (
    has_delete_disambiguation_pending,
    mark_delete_disambiguation_pending,
)
from chat.services.expense.delete_flow import build_numbered_delete_prompt, start_numbered_delete
from chat.services.expense.draft_view import ExpenseDraftView
from chat.services.expense.prompt_routing import apply_expense_interactive_clear_from_router
from chat.services.expense_workflow import process_expense_turn
from chat.services.session_snapshot import build_session_snapshot
from chat.services.session_turn_router import TurnKind, route_session_turn
from chat.services.turn_understanding import resolve_utterance
from chat.services.workflow_suspend import restore_suspended_expense, suspend_expense_for_workflow_switch


def _five_item_review_with_delete_pending() -> dict:
    items = [
        {"category": "lunch", "amount": 150.0},
        {"category": "snack", "amount": 50.0},
        {"category": "lunch", "amount": 120.0},
        {
            "category": "bus",
            "amount": 200.0,
            "from_location": "office",
            "to_location": "home",
        },
        {
            "category": "bus",
            "amount": 100.0,
            "from_location": "home",
            "to_location": "office",
        },
    ]
    block = {
        "active": True,
        "stage": "review",
        "items": items,
        "incurred_date_iso": "2026-06-11",
        "reply_language": "en",
    }
    start_numbered_delete(block)
    return {"expense_request": block}


def test_numbered_delete_prompt_lists_all_lines():
    wf = _five_item_review_with_delete_pending()
    block = wf["expense_request"]
    view = ExpenseDraftView(block["items"], block)
    q = build_numbered_delete_prompt(view, lang="en")
    assert "1." in q
    assert "5." in q
    assert read_active_prompt(block).get("kind") == KIND_DELETE_PICK


def test_router_p04_clears_expense_interactive_flag():
    wf = _five_item_review_with_delete_pending()
    snap = build_session_snapshot("expense submit koro", workflow_state=wf)
    utterance = resolve_utterance("expense submit koro", snap)
    decision = route_session_turn(snap, workflow_state=wf, utterance=utterance)
    assert decision.turn_kind == TurnKind.SUBMIT_COMMAND
    assert decision.flags.get("clear_expense_interactive")
    wf2 = apply_expense_interactive_clear_from_router(wf, decision, "expense submit koro")
    assert wf2 is not wf
    assert not has_delete_disambiguation_pending(wf2["expense_request"])


def test_snack_collision_auto_adds_duplicate_category():
    wf = _five_item_review_with_delete_pending()
    mark_delete_disambiguation_pending(wf["expense_request"])
    pack = process_expense_turn(workflow_state=wf, message="ajke nasta 50 taka")
    q = pack.get("question") or ""
    assert "Which entry should I delete" not in q
    items = pack["items"]
    assert sum(1 for r in items if str(r.get("category")).lower() == "snack") >= 1


def test_suspend_restore_clears_active_prompt():
    wf = _five_item_review_with_delete_pending()
    assert has_delete_disambiguation_pending(wf["expense_request"])
    wf = suspend_expense_for_workflow_switch(wf)
    wf = restore_suspended_expense(wf)
    assert not has_delete_disambiguation_pending(wf["expense_request"])
    assert read_active_prompt(wf["expense_request"]) is None
