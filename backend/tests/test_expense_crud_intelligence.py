"""Expense CRUD intelligence — prompt supremacy, summary during slots, delete flow."""

from __future__ import annotations

import pytest

from chat.services.expense.active_prompt import KIND_DELETE_CONFIRM, KIND_DELETE_PICK, read_active_prompt
from chat.services.expense.delete_flow import (
    apply_delete_line,
    parse_delete_pick_number,
    start_delete_confirm,
    start_numbered_delete,
)
from chat.services.expense.draft_view import ExpenseDraftView
from chat.services.expense_workflow import process_expense_turn, wants_expense_summary
from chat.services.message_context_clarity import should_ask_context_clarification
from chat.services.session_snapshot import build_session_snapshot
from chat.services.session_turn_router import TurnKind, route_session_turn
from chat.services.turn_understanding import resolve_utterance


def _collecting_with_routes_pending() -> dict:
    """Lunch, snack, three buses pending From/To (user-style draft)."""
    items = [
        {"category": "Lunch", "amount": 150.0},
        {"category": "Snack", "amount": 50.0},
        {"category": "Lunch", "amount": 120.0},
    ]
    block = {
        "active": True,
        "stage": "collecting",
        "incurred_date_iso": "2026-06-13",
        "reply_language": "banglish",
        "items": items,
        "pending_step": "from_to",
        "pending_line": {
            "amount": 100.0,
            "category": "Bus",
            "from_location": "",
            "to_location": "",
        },
        "pending_queue": [
            {
                "amount": 100.0,
                "category": "Bus",
                "from_location": "",
                "to_location": "",
            },
            {
                "amount": 100.0,
                "category": "Bus",
                "from_location": "",
                "to_location": "",
            },
            {
                "amount": 150.0,
                "category": "Bike",
                "from_location": "",
                "to_location": "",
            },
        ],
    }
    return {"expense_request": block}


def test_wants_expense_summary_typo_smmery():
    assert wants_expense_summary("expense er smmery ta daw")
    assert wants_expense_summary("expense er summery ta daw")


def test_summary_during_from_to_pending_not_route_prompt():
    wf = _collecting_with_routes_pending()
    pack = process_expense_turn(workflow_state=wf, message="expense er smmery ta daw")
    q = pack.get("question") or ""
    assert "From and To" not in q
    assert "From/To" not in q or "Pending" in q or "pending" in q.lower() or "Saved" in q
    assert "Lunch" in q or "150" in q


def test_delete_pick_six_no_not_context_clarify():
    wf = _collecting_with_routes_pending()
    block = wf["expense_request"]
    start_numbered_delete(block)
    assert read_active_prompt(block).get("kind") == KIND_DELETE_PICK
    assert not should_ask_context_clarification(
        "6 no",
        [],
        intent="unknown",
        balance_probe=False,
        leave_active=False,
        expense_active=True,
        workflow_continuation=True,
        pending_prompt_snapshot=build_session_snapshot("6 no", workflow_state=wf),
        workflow_state=wf,
    )


def test_delete_pick_six_no_routes_to_expense():
    wf = _collecting_with_routes_pending()
    start_numbered_delete(wf["expense_request"])
    snap = build_session_snapshot("6 no", workflow_state=wf)
    utterance = resolve_utterance("6 no", snap)
    decision = route_session_turn(snap, workflow_state=wf, utterance=utterance)
    assert decision.turn_kind in (TurnKind.SLOT_ANSWER, TurnKind.DELETE_CONFIRM)
    assert decision.target_workflow == "expense"


def test_delete_flow_pick_confirm_and_apply():
    wf = _collecting_with_routes_pending()
    block = wf["expense_request"]
    start_numbered_delete(block)
    pack = process_expense_turn(workflow_state=wf, message="6")
    q = pack.get("question") or ""
    assert "Delete" in q or "মুছ" in q
    assert read_active_prompt(pack["workflow_state"]["expense_request"]).get("kind") == KIND_DELETE_CONFIRM

    pack2 = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="yes",
    )
    q2 = pack2.get("question") or ""
    assert "Removed" in q2 or "মুছে" in q2
    view = ExpenseDraftView(
        pack2["workflow_state"]["expense_request"]["items"],
        pack2["workflow_state"]["expense_request"],
    )
    nums = [ln.number for ln in view.lines]
    assert 6 not in nums or all(ln.amount != 100 or ln.category.lower() != "bus" for ln in view.lines if ln.number == 6)


def test_parse_delete_pick_embedded_line_reference():
    assert parse_delete_pick_number("6. Bus — 100 Tk (route pending) delete koro") == 6
    assert parse_delete_pick_number("#6") == 6


def test_apply_delete_pending_promotes_queue():
    block = {
        "pending_step": "from_to",
        "pending_line": {"amount": 100, "category": "Bus", "from_location": "", "to_location": ""},
        "pending_queue": [
            {"amount": 150, "category": "Bike", "from_location": "", "to_location": ""},
        ],
        "items": [{"category": "Lunch", "amount": 120}],
    }
    view = ExpenseDraftView(block["items"], block)
    pending_line = next(ln for ln in view.lines if ln.kind == "pending")
    items, block, changed = apply_delete_line(view, pending_line)
    assert changed
    assert block.get("pending_line", {}).get("category") == "Bike"
    assert not block.get("pending_queue")


def test_policy_query_during_expense_routes_to_policy():
    wf = _collecting_with_routes_pending()
    snap = build_session_snapshot("reimbursement policy ki?", workflow_state=wf)
    utterance = resolve_utterance("reimbursement policy ki?", snap)
    decision = route_session_turn(snap, workflow_state=wf, utterance=utterance)
    assert decision.turn_kind == TurnKind.POLICY_QUERY


def test_multi_delete_pick():
    wf = _collecting_with_routes_pending()
    start_numbered_delete(wf["expense_request"])
    pack = process_expense_turn(
        workflow_state=wf,
        message="#4 #6 delete koro",
    )
    q = pack.get("question") or ""
    assert "Removed" in q or "মুছে" in q
    view = ExpenseDraftView(
        pack["workflow_state"]["expense_request"]["items"],
        pack["workflow_state"]["expense_request"],
    )
    assert len(view.lines) == 5


@pytest.mark.parametrize(
    "message",
    [
        "reimbursement policy",
        "leave balance",
        "ami leave nite chai",
    ],
)
def test_side_questions_during_expense_not_delete_pick(message):
    wf = _collecting_with_routes_pending()
    start_numbered_delete(wf["expense_request"])
    snap = build_session_snapshot(message, workflow_state=wf)
    utterance = resolve_utterance(message, snap)
    decision = route_session_turn(snap, workflow_state=wf, utterance=utterance)
    assert decision.turn_kind != TurnKind.SLOT_ANSWER or decision.matched_predicate != "expense_active_prompt_delete_pick"
