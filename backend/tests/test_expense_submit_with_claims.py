"""Submit command mixed with new expense claims in one message."""

from __future__ import annotations

from chat.services.expense.draft_view import ExpenseDraftView
from chat.services.expense.wizard_commands import (
    message_has_ingestible_claim_body,
    strip_expense_submit_tail_for_parse,
    wants_expense_submit_command,
)
from chat.services.expense_workflow import process_expense_turn
from chat.services.session_snapshot import build_session_snapshot
from chat.services.session_turn_router import route_session_turn


def _draft_with_pending_buses() -> dict:
    return {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "incurred_date_iso": "2026-06-12",
            "reply_language": "banglish",
            "items": [
                {"category": "snack", "amount": 50.0},
                {"category": "lunch", "amount": 120.0},
            ],
            "pending_line": {
                "category": "bus",
                "amount": 200.0,
            },
            "pending_step": "from_to",
            "pending_queue": [
                {"category": "bus", "amount": 100.0},
            ],
        }
    }


def test_strip_submit_tail_for_compound_claim():
    msg = "ajke amr expense hoyeche bus 50,bike 120,train 30 eta submit kore daw"
    body = strip_expense_submit_tail_for_parse(msg)
    assert wants_expense_submit_command(msg)
    assert "submit" not in body.lower()
    assert message_has_ingestible_claim_body(body, original=msg)
    assert "bike" in body.lower()


def test_submit_with_claims_routes_add_lines_not_bare_navigate():
    wf = _draft_with_pending_buses()
    msg = "ajke amr expense hoyeche bus 50,bike 120,train 30 eta submit kore daw"
    snap = build_session_snapshot(msg, workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.target_workflow == "expense"
    assert decision.reason != "P04_expense_submit_command"


def test_submit_with_claims_ingests_bike_before_blocked_summary():
    wf = _draft_with_pending_buses()
    msg = "ajke amr expense hoyeche bus 50,bike 120,train 30 eta submit kore daw"
    pack = process_expense_turn(workflow_state=wf, message=msg)
    items = pack["items"]
    block = pack["workflow_state"]["expense_request"]
    view = ExpenseDraftView(items, block)
    cats = {ln.category.lower() for ln in view.lines}
    assert "bike" in cats or any(
        str(r.get("category") or "").lower() == "bike" for r in items
    )
    q = pack.get("question") or ""
    assert "জমা দেওয়া যাবে না" in q or "Cannot submit" in q or "বাকি" in q
    assert "Save korechi" in q or "Saved" in q
