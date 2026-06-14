"""
Map ``SessionTurnDecision`` → workflow execution hints (Phase A).

When the session router locks a turn (non-P99), execution modules must not
re-classify the same message — they only apply the router's decision.
"""

from __future__ import annotations

from typing import Any

from chat.services.expense.turn_schema import (
    TURN_ADD_LINES,
    TURN_CONFIRM,
    TURN_DENY,
    TURN_EDIT_DRAFT,
    TURN_FILL_SLOT,
    TURN_NAVIGATE,
    TurnDecision,
)
from chat.services.session_turn_bridge import router_is_fallback
from chat.services.session_turn_router import SessionTurnDecision, TurnKind


def router_execution_locked(decision: SessionTurnDecision | None) -> bool:
    return decision is not None and not router_is_fallback(decision)


def expense_turn_decision_from_router(
    decision: SessionTurnDecision | None,
) -> TurnDecision | None:
    """Deterministic expense turn type from a locked router decision."""
    if not router_execution_locked(decision):
        return None
    assert decision is not None

    kind_map: dict[TurnKind, str] = {
        TurnKind.CONFIRM_YES: TURN_CONFIRM,
        TurnKind.CONFIRM_NO: TURN_DENY,
        TurnKind.CORRECTION: TURN_EDIT_DRAFT,
        TurnKind.SLOT_ANSWER: TURN_FILL_SLOT,
        TurnKind.DONE_COLLECTING: TURN_NAVIGATE,
        TurnKind.SUBMIT_COMMAND: TURN_NAVIGATE,
        TurnKind.WORKFLOW_SWITCH: TURN_ADD_LINES,
        TurnKind.NEW_EXPENSE: TURN_ADD_LINES,
        TurnKind.DELETE_CONFIRM: TURN_CONFIRM,
        TurnKind.PRE_SUBMIT_REVIEW: TURN_NAVIGATE,
        TurnKind.SUMMARY: TURN_NAVIGATE,
        TurnKind.RESUME_SUSPENDED: TURN_NAVIGATE,
        TurnKind.DEFER_SUBMIT: TURN_CONFIRM,
    }
    turn_type = kind_map.get(decision.turn_kind)
    if not turn_type:
        return None
    td = TurnDecision(
        turn_type=turn_type,
        confidence=decision.confidence,
        source=f"session_router:{decision.reason}",
    )
    if decision.turn_kind == TurnKind.SUBMIT_COMMAND:
        td.submit_draft = True
    return td


def leave_execution_hint_from_router(
    decision: SessionTurnDecision | None,
) -> dict[str, Any] | None:
    """Lightweight hint bundle for ``process_leave_turn`` when router is locked."""
    if not router_execution_locked(decision):
        return None
    assert decision is not None
    return {
        "turn_kind": decision.turn_kind.value,
        "handler_id": decision.handler_id,
        "reason": decision.reason,
        "target_workflow": decision.target_workflow,
        "flags": dict(decision.flags or {}),
    }
