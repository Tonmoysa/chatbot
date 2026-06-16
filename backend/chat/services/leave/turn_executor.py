"""
Execute router-locked leave turns without re-classifying the user message.

When ``session_turn_router`` returns a non-P99 decision, leave handlers must
trust that classification and only apply slot/confirm/correction mechanics.
"""

from __future__ import annotations

from typing import Any

from chat.services.leave_fsm import read_leave_state
from chat.services.leave.turn_schema import (
    TURN_CONFIRM,
    TURN_DENY,
    TURN_EDIT_FIELD,
    TURN_FILL_SLOT,
    LeaveTurnDecision,
)
from chat.services.session_router_execution import router_execution_locked
from chat.services.session_turn_router import SessionTurnDecision, TurnKind

KEY_ROUTER_EXECUTION = "router_execution_hint"


def store_router_execution_hint(
    workflow_state: dict[str, Any],
    decision: SessionTurnDecision | None,
) -> dict[str, Any]:
    from chat.services.session_router_execution import leave_execution_hint_from_router

    wf = dict(workflow_state or {})
    hint = leave_execution_hint_from_router(decision)
    if hint:
        wf[KEY_ROUTER_EXECUTION] = hint
    else:
        wf.pop(KEY_ROUTER_EXECUTION, None)
    return wf


def read_router_execution_hint(workflow_state: dict[str, Any] | None) -> dict[str, Any]:
    raw = (workflow_state or {}).get(KEY_ROUTER_EXECUTION) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def leave_turn_decision_from_router(
    decision: SessionTurnDecision | None,
) -> LeaveTurnDecision | None:
    if not router_execution_locked(decision):
        return None
    assert decision is not None

    kind_map: dict[TurnKind, str] = {
        TurnKind.CONFIRM_YES: TURN_CONFIRM,
        TurnKind.CONFIRM_NO: TURN_DENY,
        TurnKind.CORRECTION: TURN_EDIT_FIELD,
        TurnKind.SLOT_ANSWER: TURN_FILL_SLOT,
        TurnKind.CONTINUE_WIZARD: TURN_FILL_SLOT,
    }
    turn_type = kind_map.get(decision.turn_kind)
    if not turn_type:
        return None
    return LeaveTurnDecision(
        turn_type=turn_type,
        confidence=decision.confidence,
        source=f"session_router:{decision.reason}",
    )


def router_forces_slot_answer(workflow_state: dict[str, Any] | None) -> bool:
    hint = read_router_execution_hint(workflow_state)
    return hint.get("turn_kind") in ("slot_answer", "continue_wizard")


def router_forces_review_correction(workflow_state: dict[str, Any] | None) -> bool:
    hint = read_router_execution_hint(workflow_state)
    return hint.get("turn_kind") == "correction"


def router_skips_overlap_check(workflow_state: dict[str, Any] | None) -> bool:
    hint = read_router_execution_hint(workflow_state)
    return hint.get("turn_kind") in (
        "duplicate_leave",
        "new_leave",
        "workflow_switch",
        "meta_question",
        "summary",
    )


def leave_turn_from_execution_hint(
    workflow_state: dict[str, Any] | None,
) -> LeaveTurnDecision | None:
    hint = read_router_execution_hint(workflow_state)
    if not hint:
        return None
    try:
        kind = TurnKind(str(hint.get("turn_kind") or ""))
    except ValueError:
        return None
    fake = SessionTurnDecision(
        turn_kind=kind,
        intent=None,
        target_workflow=hint.get("target_workflow"),
        handler_id=str(hint.get("handler_id") or ""),
        confidence=0.99,
        reason=str(hint.get("reason") or "router_hint"),
    )
    return leave_turn_decision_from_router(fake)


def try_execute_router_locked_leave_turn(
    *,
    workflow_state: dict[str, Any],
    message: str,
    entities: dict[str, Any] | None,
    router_decision: SessionTurnDecision | None,
    company_id: str = "",
    trace_id: str = "",
) -> dict[str, Any] | None:
    """Handle confirm/cancel turns that must not pass through collecting re-parse."""
    if not router_execution_locked(router_decision):
        return None
    assert router_decision is not None

    from chat.services.leave_confirm import (
        is_awaiting_leave_confirmation,
        process_confirmation_turn,
    )
    from chat.services.leave_workflow import build_merged_entities_for_engine, clear_leave_flow

    wf = dict(workflow_state or {})
    st = read_leave_state(wf)
    draft = dict(st.get("draft") or {})

    if router_decision.turn_kind == TurnKind.CANCEL:
        wf = clear_leave_flow(wf)
        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(draft),
            "complete": False,
            "confirmed_submit": False,
            "question": None,
            "cancelled": True,
        }

    if router_decision.turn_kind in (
        TurnKind.CONFIRM_YES,
        TurnKind.CONFIRM_NO,
        TurnKind.CONTINUE_WIZARD,
        TurnKind.SUBMIT_COMMAND,
    ) and is_awaiting_leave_confirmation(wf):
        return process_confirmation_turn(
            workflow_state=wf,
            message=message,
            draft=draft,
            entities=entities,
            trace_id=trace_id,
        )

    return None
