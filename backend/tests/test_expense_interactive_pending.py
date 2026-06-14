"""Stale delete/confirm prompts must not trap submit or new expense lines."""

from __future__ import annotations

from chat.services.expense.delete_disambiguation_pending import (
    has_delete_disambiguation_pending,
    mark_delete_disambiguation_pending,
)
from chat.services.expense_workflow import process_expense_turn
from chat.services.workflow_suspend import (
    restore_suspended_expense,
    suspend_expense_for_workflow_switch,
)


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
    mark_delete_disambiguation_pending(block)
    return {"expense_request": block}


def test_suspend_and_restore_clears_delete_disambiguation():
    wf = _five_item_review_with_delete_pending()
    assert has_delete_disambiguation_pending(wf["expense_request"])

    wf = suspend_expense_for_workflow_switch(wf)
    assert "expense_request" not in wf
    suspended = (wf.get("suspended_expense") or {}).get("expense_request") or {}
    assert not has_delete_disambiguation_pending(suspended)

    wf = restore_suspended_expense(wf)
    block = wf["expense_request"]
    assert block.get("active")
    assert not has_delete_disambiguation_pending(block)


def test_expense_submit_escapes_stale_delete_disambiguation():
    wf = _five_item_review_with_delete_pending()
    pack = process_expense_turn(workflow_state=wf, message="expense submit koro")
    q = pack.get("question") or ""
    assert "Which entry should I delete" not in q
    assert "Kon entry delete" not in q
    block = pack["workflow_state"]["expense_request"]
    assert not has_delete_disambiguation_pending(block)


def test_new_expense_line_escapes_stale_delete_disambiguation():
    wf = _five_item_review_with_delete_pending()
    pack = process_expense_turn(workflow_state=wf, message="ajke nasta 50 taka")
    q = pack.get("question") or ""
    assert "Which entry should I delete" not in q
    assert "Kon entry delete" not in q
    block = pack["workflow_state"]["expense_request"]
    assert not has_delete_disambiguation_pending(block)
