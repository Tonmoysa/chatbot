"""Cross-workflow submit + expense resume after leave switch."""

from __future__ import annotations

from chat.constants import INTENT_EXPENSE_CLAIM, INTENT_LEAVE_REQUEST
from chat.services.expense_workflow import process_expense_turn
from chat.services.leave_workflow import process_leave_turn
from chat.services.session_snapshot import build_session_snapshot
from chat.services.session_turn_router import TurnKind, route_session_turn
from chat.services.workflow_suspend import (
    suspend_expense_for_workflow_switch,
    suspend_leave_for_workflow_switch,
)


def _leave_review_draft() -> dict:
    return {
        "leave_type": "annual",
        "day_scope": "full",
        "start_date": "2026-06-16",
        "end_date": "2026-06-18",
        "reason": "family program",
        "leave_payment_category": "paid",
    }


def _expense_review_block() -> dict:
    return {
        "active": True,
        "stage": "review",
        "incurred_date_iso": "2026-06-15",
        "items": [
            {
                "category": "Bus",
                "amount": 100.0,
                "from_location": "mirpur",
                "to_location": "badda",
            },
            {"category": "Lunch", "amount": 100.0},
        ],
    }


def test_submit_koro_after_expense_summary_routes_to_parked_leave_review() -> None:
    """Leave at review, expense summary parks leave — submit still confirms leave."""
    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": _leave_review_draft(),
        "review_pending": True,
        "expense_request": _expense_review_block(),
    }
    wf = suspend_expense_for_workflow_switch(wf)
    wf = suspend_leave_for_workflow_switch(wf)

    snap = build_session_snapshot("submit koro", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)

    assert decision.reason == "P03_leave_submit_command"
    assert decision.intent == INTENT_LEAVE_REQUEST
    assert decision.turn_kind == TurnKind.SUBMIT_COMMAND

    pack = process_leave_turn(
        workflow_state=wf,
        message="submit koro",
        entities={},
        company_id="default",
        trace_id="cross-submit-leave",
        router_decision=decision,
    )
    assert pack.get("confirmed_submit") is True


def test_submit_koro_routes_to_foreground_expense_when_resumed() -> None:
    """After expense e jao, submit applies to expense — not parked leave."""
    wf = {
        "suspended_leave": {
            "draft": _leave_review_draft(),
            "step": None,
            "status": "active",
            "review_pending": True,
        },
        "expense_request": _expense_review_block(),
    }

    snap = build_session_snapshot("submit koro", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)

    assert decision.reason == "P04_expense_submit_command"
    assert decision.intent == INTENT_EXPENSE_CLAIM


def test_expense_e_jao_restores_suspended_review_draft_with_bus_route() -> None:
    """Resume must restore suspended snapshot — not start a blank collecting wizard."""
    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": _leave_review_draft(),
        "review_pending": True,
    }
    wf = suspend_expense_for_workflow_switch(
        {
            **wf,
            "expense_request": _expense_review_block(),
        }
    )
    wf = suspend_leave_for_workflow_switch(wf)

    from chat.services.session_turn_router import SessionTurnDecision

    resume_decision = SessionTurnDecision(
        turn_kind=TurnKind.RESUME_SUSPENDED,
        intent=INTENT_EXPENSE_CLAIM,
        target_workflow="expense",
        handler_id="expense_workflow",
        confidence=0.99,
        reason="P53_resume_or_show_expense",
    )

    pack = process_expense_turn(
        workflow_state=wf,
        message="expense e jao",
        router_decision=resume_decision,
    )
    block = (pack.get("workflow_state") or {}).get("expense_request") or {}
    items = block.get("items") or []
    assert block.get("stage") == "review"
    assert len(items) == 2
    bus = next(i for i in items if str(i.get("category")).lower() == "bus")
    assert bus.get("from_location") == "mirpur"
    assert bus.get("to_location") == "badda"
    question = str(pack.get("question") or "")
    assert "from" not in question.lower() or "mirpur" in question.lower()
