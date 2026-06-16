"""

Bridge ``session_turn_router`` decisions into the orchestrator pipeline.



Maps ``SessionTurnDecision`` → intent_result, workflow_turn, and side-effect

messages (duplicate leave prompt, context clarification).

"""



from __future__ import annotations



from dataclasses import dataclass

from datetime import date

from typing import Any



from chat.constants import (

    INTENT_EXPENSE_CLAIM,

    INTENT_EXPENSE_DAY_SUMMARY,

    INTENT_EXPENSE_STATUS,

    INTENT_HR_POLICY,

    INTENT_LEAVE_BALANCE,

    INTENT_LEAVE_REQUEST,

    INTENT_REQUEST_STATUS,

    INTENT_UNKNOWN,

)

from chat.services.message_context_clarity import build_context_clarification_message

from chat.services.session_snapshot import SessionSnapshot, build_session_snapshot

from chat.services.session_turn_router import SessionTurnDecision, TurnKind, route_session_turn

from chat.services.turn_classifier import (

    TURN_CANCEL,

    TURN_CHITCHAT,

    TURN_CONFIRM,

    TURN_CORRECTION,

    TURN_NEW_WORKFLOW,

    TURN_POLICY_QUERY,

    TURN_SLOT_ANSWER,

)



_LEGACY_SOURCE_SUFFIX = {
    "P05_cancel_leave": "+cancel_leave_verify",
    "P06_cancel_expense": "+cancel_expense_verify",
    "P32_defer_expense_for_leave_submit": "+confirm_defer_leave_submit",
    "P33_defer_leave_for_expense_submit": "+defer_leave_submit",
    "P33_defer_expense_submit": "+defer_expense_submit",
    "P44_pending_leave_show": "+pending_leave_show",
    "P42_leave_summary": "+leave_summary",
    "P43_leave_meta": "+leave_meta",
    "P43_expense_meta": "+expense_meta",
    "P54_dual_workflow_submit": "+dual_workflow_submit",
    "P49_post_submit_leave_nav": "+post_submit_leave_nav",
    "P54_leave_nav_no_session": "+leave_nav_no_session",
}





@dataclass

class RouterPipelineEffects:

    duplicate_leave_message: str | None = None

    context_clarification_message: str | None = None

    force_general_out_of_scope: bool = False

    workflow_turn: str | None = None

    leave_nav_no_session: bool = False





def run_session_turn_router(

    message: str,

    *,

    workflow_state: dict[str, Any] | None,

    context_lines: list[str] | None = None,

    balance_probe: bool | None = None,

    is_greeting: bool | None = None,

    is_cancel: bool | None = None,

    provisional_intent: str | None = None,

    workflow_continuation: bool = False,

    today: date | None = None,

    trace_id: str = "",

) -> tuple[SessionSnapshot, SessionTurnDecision]:

    snapshot = build_session_snapshot(

        message,

        workflow_state=workflow_state,

        context_lines=context_lines,

        balance_probe=balance_probe,

        is_greeting=is_greeting,

        is_cancel=is_cancel,

        provisional_intent=provisional_intent,

        workflow_continuation=workflow_continuation,

        today=today,

    )

    from chat.services.turn_understanding import resolve_utterance

    last_question = ""
    for line in reversed(list(context_lines or [])):
        text = str(line or "").strip()
        if text.lower().startswith(("assistant:", "bot:")):
            last_question = text.split(":", 1)[-1].strip()
            break

    utterance = resolve_utterance(
        message,
        snapshot,
        last_question=last_question,
        trace_id=trace_id,
    )

    return snapshot, route_session_turn(

        snapshot,
        workflow_state=workflow_state,
        trace_id=trace_id,
        utterance=utterance,

    )





def router_is_fallback(decision: SessionTurnDecision) -> bool:

    return decision.reason.startswith("P99")




def apply_hr_query_router_fallback(
    decision: SessionTurnDecision,
    hr_query: Any,
) -> SessionTurnDecision:
    """
  When the P00–P99 matrix did not match (P99), promote a confident
    ``hr_query_classifier`` result into a concrete router decision.
    """
    from chat.constants import (
        INTENT_APPROVAL_ESCALATION,
        INTENT_EXPENSE_CLAIM,
        INTENT_EXPENSE_DAY_SUMMARY,
        INTENT_EXPENSE_STATUS,
        INTENT_HR_POLICY,
        INTENT_LEAVE_BALANCE,
        INTENT_LEAVE_REQUEST,
        INTENT_REQUEST_STATUS,
    )
    from chat.services.hr_query_classifier import (
        CONFIDENCE_LLM_FALLBACK,
        QUERY_CHITCHAT,
        QUERY_UNKNOWN,
    )
    from chat.services.session_turn_router import _decision

    if not router_is_fallback(decision):
        return decision

    intent = hr_query.maps_to_intent
    if not intent:
        return decision
    if float(hr_query.confidence or 0) < CONFIDENCE_LLM_FALLBACK:
        return decision
    if hr_query.query_kind in (QUERY_CHITCHAT, QUERY_UNKNOWN) and not hr_query.in_hr_scope:
        return decision

    mapping: dict[str, tuple[TurnKind, str | None, str, str]] = {
        INTENT_LEAVE_BALANCE: (
            TurnKind.BALANCE_QUERY,
            None,
            "leave_balance",
            "hr_query_fallback_balance",
        ),
        INTENT_HR_POLICY: (
            TurnKind.POLICY_QUERY,
            None,
            "policy_kb",
            "hr_query_fallback_policy",
        ),
        INTENT_EXPENSE_STATUS: (
            TurnKind.META_QUESTION,
            "expense",
            "expense.session_action_memory",
            "hr_query_fallback_expense_meta",
        ),
        INTENT_REQUEST_STATUS: (
            TurnKind.META_QUESTION,
            "leave",
            "leave.session_action_memory",
            "hr_query_fallback_request_status",
        ),
        INTENT_EXPENSE_DAY_SUMMARY: (
            TurnKind.SUMMARY,
            "expense",
            "expense_workflow",
            "hr_query_fallback_expense_summary",
        ),
        INTENT_EXPENSE_CLAIM: (
            TurnKind.NEW_EXPENSE,
            "expense",
            "expense_workflow",
            "hr_query_fallback_expense_claim",
        ),
        INTENT_LEAVE_REQUEST: (
            TurnKind.NEW_LEAVE,
            "leave",
            "leave_workflow",
            "hr_query_fallback_leave",
        ),
        INTENT_APPROVAL_ESCALATION: (
            TurnKind.META_QUESTION,
            None,
            "global_intent",
            "hr_query_fallback_approval",
        ),
    }
    row = mapping.get(intent)
    if not row:
        return decision

    kind, target, handler, reason = row
    return _decision(
        turn_kind=kind,
        intent=intent,
        target_workflow=target,
        handler_id=handler,
        reason=reason,
        matched_predicate="hr_query_classifier",
        confidence=float(hr_query.confidence or CONFIDENCE_LLM_FALLBACK),
    )




def should_override_wizard_intent(

    decision: SessionTurnDecision,

    *,

    wizard_active: bool,

) -> bool:

    if router_is_fallback(decision):

        return False

    if wizard_active:

        return True

    return router_overrides_cold_start_intent(decision)





def is_decisive_router_decision(

    decision: SessionTurnDecision,

    *,

    wizard_active: bool = False,

) -> bool:

    if router_is_fallback(decision):

        return False

    if wizard_active:

        return workflow_turn_from_router_decision(decision) is not None

    return workflow_turn_from_router_decision(decision) is not None and (

        decision.turn_kind

        in {

            TurnKind.CORRECTION,

            TurnKind.SUMMARY,

            TurnKind.PRE_SUBMIT_REVIEW,

            TurnKind.DONE_COLLECTING,

            TurnKind.DELETE_CONFIRM,

            TurnKind.WORKFLOW_SWITCH,

            TurnKind.NEW_LEAVE,

            TurnKind.DEFER_SUBMIT,

            TurnKind.SLOT_ANSWER,

            TurnKind.SUBMIT_COMMAND,

            TurnKind.CONTINUE_WIZARD,

            TurnKind.CANCEL,

            TurnKind.CONFIRM_YES,

            TurnKind.CONFIRM_NO,

            TurnKind.META_QUESTION,

            TurnKind.POLICY_QUERY,

            TurnKind.BALANCE_QUERY,

            TurnKind.CHITCHAT,

            TurnKind.OUT_OF_SCOPE,

        }

    )





def legacy_wizard_intent_fallback(

    message: str,

    workflow_state: dict[str, Any],

    *,

    balance_probe: bool,

    trace_id: str = "",

) -> dict[str, Any]:

    """P99 fallback — legacy deterministic gates until fully retired."""

    from chat.services.expense_workflow import is_expense_in_progress

    from chat.services.leave_workflow import is_leave_in_progress
    from chat.services.workflow_priority import expense_workflow_is_foreground



    if expense_workflow_is_foreground(workflow_state):

        from chat.services.legacy_wizard_intent import detect_intent_during_expense_workflow



        return detect_intent_during_expense_workflow(

            message, workflow_state, balance_probe=balance_probe, trace_id=trace_id

        )

    if is_leave_in_progress(workflow_state):

        from chat.services.legacy_wizard_intent import detect_intent_during_leave_workflow



        return detect_intent_during_leave_workflow(

            message, workflow_state, balance_probe=balance_probe, trace_id=trace_id

        )

    if is_expense_in_progress(workflow_state):

        from chat.services.legacy_wizard_intent import detect_intent_during_expense_workflow



        return detect_intent_during_expense_workflow(

            message, workflow_state, balance_probe=balance_probe, trace_id=trace_id

        )

    return {

        "intent": INTENT_UNKNOWN,

        "confidence": 0.0,

        "source": "session_turn_router+P99_no_match",

    }





def intent_result_from_router_decision(decision: SessionTurnDecision) -> dict[str, Any]:

    """Map router output to orchestrator intent_result shape."""

    intent = decision.intent

    if intent is None:

        intent = INTENT_UNKNOWN



    if decision.turn_kind == TurnKind.META_QUESTION:

        if decision.intent == INTENT_LEAVE_REQUEST:

            intent = INTENT_LEAVE_REQUEST

        elif decision.intent == INTENT_REQUEST_STATUS:

            intent = INTENT_REQUEST_STATUS

        else:

            intent = INTENT_EXPENSE_STATUS

    elif decision.turn_kind == TurnKind.PRE_SUBMIT_REVIEW:

        intent = INTENT_EXPENSE_STATUS

    elif decision.turn_kind == TurnKind.OUT_OF_SCOPE:

        intent = INTENT_UNKNOWN

    elif decision.turn_kind == TurnKind.CHITCHAT:

        intent = INTENT_UNKNOWN



    source = f"session_turn_router+{decision.reason}"

    suffix = _LEGACY_SOURCE_SUFFIX.get(decision.reason)

    if suffix:

        source += suffix



    result: dict[str, Any] = {

        "intent": intent,

        "confidence": decision.confidence,

        "source": source,

        "router_turn_kind": decision.turn_kind.value,

        "router_handler": decision.handler_id,

        "router_reason": decision.reason,

    }

    if decision.flags.get("duplicate_prompt"):

        result["_block_message"] = decision.flags["duplicate_prompt"]

    if decision.flags.get("block_message"):

        result["_block_message"] = decision.flags["block_message"]

    if decision.flags.get("leave_nav_no_session"):

        result["leave_nav_no_session"] = True

    return result





def workflow_turn_from_router_decision(decision: SessionTurnDecision) -> str | None:

    if decision.turn_kind == TurnKind.CONTINUE_WIZARD:

        if decision.target_workflow == "leave":

            return TURN_SLOT_ANSWER

        return TURN_CHITCHAT



    mapping = {

        TurnKind.CANCEL: TURN_CANCEL,

        TurnKind.CORRECTION: TURN_CORRECTION,

        TurnKind.CONFIRM_YES: TURN_CONFIRM,

        TurnKind.CONFIRM_NO: TURN_CONFIRM,

        TurnKind.SLOT_ANSWER: TURN_SLOT_ANSWER,

        TurnKind.CHITCHAT: TURN_CHITCHAT,

        TurnKind.POLICY_QUERY: TURN_POLICY_QUERY,

        TurnKind.BALANCE_QUERY: TURN_POLICY_QUERY,

        TurnKind.DONE_COLLECTING: TURN_SLOT_ANSWER,

        TurnKind.SUBMIT_COMMAND: TURN_SLOT_ANSWER,

        TurnKind.SUMMARY: TURN_CHITCHAT,

        TurnKind.PRE_SUBMIT_REVIEW: TURN_SLOT_ANSWER,

        TurnKind.DUPLICATE_LEAVE: TURN_SLOT_ANSWER,

        TurnKind.WORKFLOW_SWITCH: TURN_NEW_WORKFLOW,

        TurnKind.NEW_LEAVE: TURN_NEW_WORKFLOW,

        TurnKind.NEW_EXPENSE: TURN_NEW_WORKFLOW,

        TurnKind.RESUME_SUSPENDED: TURN_SLOT_ANSWER,

        TurnKind.DEFER_SUBMIT: TURN_CONFIRM,

        TurnKind.DELETE_CONFIRM: TURN_CONFIRM,

        TurnKind.META_QUESTION: TURN_POLICY_QUERY,

        TurnKind.OUT_OF_SCOPE: TURN_CHITCHAT,

        TurnKind.CONTEXT_CLARIFICATION: TURN_CHITCHAT,

        TurnKind.DELETE_REQUEST: TURN_CANCEL,

    }

    return mapping.get(decision.turn_kind)





def pipeline_effects_from_router_decision(

    decision: SessionTurnDecision,

    *,

    message: str,

    context_lines: list[str] | None,

    snapshot: SessionSnapshot | None = None,

) -> RouterPipelineEffects:

    effects = RouterPipelineEffects(

        workflow_turn=workflow_turn_from_router_decision(decision),

        leave_nav_no_session=bool(decision.flags.get("leave_nav_no_session")),

    )

    if decision.turn_kind == TurnKind.CONTEXT_CLARIFICATION:
        prompt = decision.flags.get("clarification_prompt")
        if prompt:
            effects.context_clarification_message = str(prompt)
        elif snapshot is not None:
            from chat.services.session_expected_answer import build_slot_aware_clarification

            tailored = build_slot_aware_clarification(message, snapshot)
            if tailored:
                effects.context_clarification_message = tailored

  # Duplicate overlap and context clarification are owned by workflows only.

    if decision.turn_kind == TurnKind.OUT_OF_SCOPE:

        effects.force_general_out_of_scope = True

    return effects





def router_overrides_cold_start_intent(decision: SessionTurnDecision) -> bool:

    if router_is_fallback(decision):

        return False

    return decision.turn_kind in {

        TurnKind.DUPLICATE_LEAVE,

        TurnKind.CONTEXT_CLARIFICATION,

        TurnKind.OUT_OF_SCOPE,

        TurnKind.POLICY_QUERY,

        TurnKind.BALANCE_QUERY,

        TurnKind.META_QUESTION,

        TurnKind.NEW_LEAVE,

        TurnKind.NEW_EXPENSE,

    }





def router_locked_intent(intent_result: dict[str, Any]) -> bool:

    """Router intent is final — orchestrator must not override."""

    return str(intent_result.get("source") or "").startswith("session_turn_router+")


