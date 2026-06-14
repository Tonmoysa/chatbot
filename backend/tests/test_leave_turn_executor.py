"""Tests for router-locked leave execution (Phase 6)."""

from chat.services.leave.turn_executor import (
    leave_turn_decision_from_router,
    leave_turn_from_execution_hint,
    router_forces_slot_answer,
    store_router_execution_hint,
)
from chat.services.leave.turn_schema import TURN_CONFIRM, TURN_FILL_SLOT
from chat.services.session_turn_router import SessionTurnDecision, TurnKind


def _decision(kind: TurnKind, *, reason: str = "P30_confirm_yes") -> SessionTurnDecision:
    return SessionTurnDecision(
        turn_kind=kind,
        intent="LEAVE_REQUEST",
        target_workflow="leave",
        handler_id="leave_workflow",
        confidence=0.99,
        reason=reason,
    )


def test_leave_turn_decision_confirm_yes():
    dec = leave_turn_decision_from_router(_decision(TurnKind.CONFIRM_YES))
    assert dec is not None
    assert dec.turn_type == TURN_CONFIRM
    assert dec.source.startswith("session_router:")


def test_leave_turn_decision_p99_returns_none():
    dec = leave_turn_decision_from_router(
        _decision(TurnKind.UNKNOWN, reason="P99_no_match")
    )
    assert dec is None


def test_store_hint_forces_slot_answer():
    wf = store_router_execution_hint({}, _decision(TurnKind.SLOT_ANSWER, reason="P80_leave_slot_token"))
    assert router_forces_slot_answer(wf)
    turn = leave_turn_from_execution_hint(wf)
    assert turn is not None
    assert turn.turn_type == TURN_FILL_SLOT
