"""
Single priority matrix for cross-workflow turn routing (Phase 2).

Classifies a user message against ``SessionSnapshot`` and returns a deterministic
``SessionTurnDecision``. Execution stays in workflow modules — this module only
decides *what* should handle the turn.

See ``docs/TURN_ROUTER_SPEC.md`` for the full matrix (P00–P99).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
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
from chat.services.session_snapshot import SessionSnapshot


class TurnKind(str, Enum):
    CANCEL = "cancel"
    CHITCHAT = "chitchat"
    OUT_OF_SCOPE = "out_of_scope"
    POLICY_QUERY = "policy_query"
    BALANCE_QUERY = "balance_query"
    CONTEXT_CLARIFICATION = "context_clarification"
    NEW_LEAVE = "new_leave"
    NEW_EXPENSE = "new_expense"
    WORKFLOW_SWITCH = "workflow_switch"
    RESUME_SUSPENDED = "resume_suspended"
    DEFER_SUBMIT = "defer_submit"
    SLOT_ANSWER = "slot_answer"
    CONFIRM_YES = "confirm_yes"
    CONFIRM_NO = "confirm_no"
    CORRECTION = "correction"
    SUMMARY = "summary"
    SUBMIT_COMMAND = "submit_command"
    DONE_COLLECTING = "done_collecting"
    DELETE_REQUEST = "delete_request"
    DELETE_CONFIRM = "delete_confirm"
    DUPLICATE_LEAVE = "duplicate_leave"
    META_QUESTION = "meta_question"
    PRE_SUBMIT_REVIEW = "pre_submit_review"
    CONTINUE_WIZARD = "continue_wizard"
    UNKNOWN = "unknown"


@dataclass
class SessionTurnDecision:
    turn_kind: TurnKind
    intent: str | None
    target_workflow: str | None
    handler_id: str
    confidence: float
    reason: str
    matched_predicate: str = ""
    flags: dict[str, Any] = field(default_factory=dict)

    def skip_llm_intent(self) -> bool:
        return self.intent is not None and self.turn_kind != TurnKind.UNKNOWN


def _decision(
    *,
    turn_kind: TurnKind,
    intent: str | None,
    target_workflow: str | None,
    handler_id: str,
    reason: str,
    matched_predicate: str = "",
    confidence: float = 0.99,
    flags: dict[str, Any] | None = None,
) -> SessionTurnDecision:
    return SessionTurnDecision(
        turn_kind=turn_kind,
        intent=intent,
        target_workflow=target_workflow,
        handler_id=handler_id,
        confidence=confidence,
        reason=reason,
        matched_predicate=matched_predicate,
        flags=dict(flags or {}),
    )


def _is_policy_query(message: str) -> bool:
    from chat.services.policy_intent_helpers import is_expense_entitlement_query, is_rules_query
    from chat.services.intent_detector import _strong_hr_policy

    import re

    raw = message or ""
    if re.search(
        r"annual\s+leave.{0,30}(?:বছরে|per\s*year|yearly|পাওয়া\s*যায়|কত\s*দিন)",
        raw,
        re.I | re.UNICODE,
    ):
        return True
    if is_expense_entitlement_query(message):
        return True
    if _strong_hr_policy(message) and is_rules_query(message):
        return True
    low = (message or "").lower()
    if re.search(
        r"\b(payslip|pay\s*slip|salary\s*slip|payroll|payslips)\b",
        low,
    ):
        return True
    return bool(
        re.search(r"(পেস্লিপ|বেতন\s*স্লিপ|বেতনের\s*স্লিপ)", message or "", re.I)
    )


def _leave_wizard_token(message: str) -> bool:
    import re

    from chat.services.leave.user_goal import (
        has_leave_question_marker,
        is_informational_leave_goal,
        classify_leave_user_goal,
    )
    from chat.services.leave_balance_intent import is_leave_balance_query

    if is_leave_balance_query(message):
        return False
    goal = classify_leave_user_goal(message, leave_active=True)
    if is_informational_leave_goal(goal):
        return False
    if has_leave_question_marker(message) and re.search(
        r"\b(sick|annual|casual|paid|unpaid|full|half|leave|chuti|chhuti)\b",
        (message or "").lower(),
    ):
        return False

    t = (message or "").strip().lower()
    if not t or len(t) > 48:
        return False
    try:
        from chat.services.leave.normalization import (
            looks_like_wizard_leave_type_answer,
            parse_day_scope_answer,
        )

        if looks_like_wizard_leave_type_answer(message):
            return True
        if parse_day_scope_answer(message):
            return True
    except Exception:
        pass
    if re.match(
        r"^(paid|unpaid|lwop|full|half|sick|casual|annual|anual|anul|emergency|maternity|paternity)"
        r"(?:\s+leave)?(?:\s+day)?s?$",
        t,
    ):
        return True
    if re.match(r"^(full|half)\s*day$", t):
        return True
    return bool(re.search(r"(বেতনসহ|বেতন\s*ছাড়া)", message or ""))


def _explicit_correction_marker(message: str) -> bool:
    """Negation / change phrasing that marks a *correction*, not a plain answer."""
    import re

    return bool(
        re.search(
            r"(\b(?:na|nah|not|change|badl[ae]?|poriborto[n]?|hobe|habe)\b|না|নাহ|হবে|বদল|পরিবর্তন|ঠিক\s*কর)",
            message or "",
            re.I | re.UNICODE,
        )
    )


def _is_confirmation_yes(message: str) -> bool:
    from chat.services.expense_workflow import wants_expense_summary, wants_resume_or_show_expense
    from chat.services.leave_meta_queries import wants_leave_session_summary
    from chat.services.leave_confirm import is_confirmation_yes as leave_yes
    from chat.services.expense.expense_confirm import is_confirmation_yes as expense_yes
    from chat.services.workflow_suspend import wants_resume_suspended_leave

    if wants_expense_summary(message) or wants_leave_session_summary(message):
        return False
    if wants_resume_suspended_leave(message) or wants_resume_or_show_expense(message):
        return False
    return leave_yes(message) or expense_yes(message)


def _is_confirmation_no(message: str) -> bool:
    from chat.services.leave_confirm import is_confirmation_cancel
    from chat.services.expense.expense_confirm import is_confirmation_no as expense_no

    return is_confirmation_cancel(message) or expense_no(message)


def _confirm_workflow_target(snap: SessionSnapshot) -> tuple[str, str, str] | None:
    """Return (target_workflow, intent, handler_id) for yes/no on review gates."""
    if snap.expense_submit_confirm_pending or (
        snap.expense_active
        and (snap.expense_review_pending or snap.expense_stage == "submit_confirm")
    ):
        return ("expense", INTENT_EXPENSE_CLAIM, "expense.turn_router")
    if snap.leave_submit_confirm_pending or snap.leave_review_pending:
        return ("leave", INTENT_LEAVE_REQUEST, "leave_workflow")
    return None


def _wizard_interrupt_decision(
    snapshot: SessionSnapshot,
    msg: str,
    workflow_state: dict[str, Any] | None,
    *,
    trace_id: str = "",
) -> SessionTurnDecision | None:
    """LLM/rules interrupt classifier — last resort before continue-wizard fallback."""
    if not workflow_state:
        return None
    from chat.constants import (
        INTENT_EXPENSE_CLAIM,
        INTENT_EXPENSE_DAY_SUMMARY,
        INTENT_HR_POLICY,
        INTENT_LEAVE_BALANCE,
        INTENT_LEAVE_REQUEST,
    )
    from chat.services.wizard_interrupt_classifier import (
        build_expense_interrupt_context,
        build_leave_interrupt_context,
        classify_wizard_interrupt,
        interrupt_is_workflow_switch,
    )
    from chat.services.leave_workflow import pending_step

    CONF = 0.99

    def _from_interrupt(
        intr,
        *,
        prefix: str,
        allowed_intents: frozenset[str],
    ) -> SessionTurnDecision | None:
        if not interrupt_is_workflow_switch(intr) and intr.maps_to_intent not in allowed_intents:
            return None
        intent = intr.maps_to_intent
        if interrupt_is_workflow_switch(intr) and not intent:
            intent = INTENT_UNKNOWN
        if not intent:
            return None
        kind = TurnKind.WORKFLOW_SWITCH if interrupt_is_workflow_switch(intr) else TurnKind.CHITCHAT
        if intent in (INTENT_HR_POLICY, INTENT_LEAVE_BALANCE):
            kind = TurnKind.POLICY_QUERY
        if intent == INTENT_UNKNOWN:
            kind = TurnKind.CHITCHAT
        target = None
        if intent == INTENT_LEAVE_REQUEST:
            target = "leave"
        elif intent in (INTENT_EXPENSE_CLAIM, INTENT_EXPENSE_DAY_SUMMARY):
            target = "expense"
        return _decision(
            turn_kind=kind,
            intent=intent,
            target_workflow=target,
            handler_id="wizard_interrupt_classifier",
            reason=f"{prefix}_wizard_interrupt",
            matched_predicate="classify_wizard_interrupt",
            confidence=intr.confidence or CONF,
        )

    if snapshot.leave_active or snapshot.leave_review_pending:
        ctx = build_leave_interrupt_context(
            workflow_state,
            leave_review_pending=snapshot.leave_review_pending,
            pending_leave_step=snapshot.pending_leave_step or pending_step(workflow_state),
        )
        intr = classify_wizard_interrupt(msg, context=ctx, trace_id=trace_id, use_llm=True)
        allowed = (
            frozenset({INTENT_EXPENSE_DAY_SUMMARY, INTENT_LEAVE_REQUEST})
            if snapshot.leave_review_pending
            else frozenset(
                {INTENT_EXPENSE_CLAIM, INTENT_HR_POLICY, INTENT_LEAVE_BALANCE}
            )
        )
        return _from_interrupt(intr, prefix="P84", allowed_intents=allowed)

    if snapshot.expense_active:
        intr = classify_wizard_interrupt(
            msg,
            context=build_expense_interrupt_context(workflow_state),
            trace_id=trace_id,
            use_llm=True,
        )
        return _from_interrupt(
            intr,
            prefix="P85",
            allowed_intents=frozenset(
                {INTENT_LEAVE_REQUEST, INTENT_HR_POLICY, INTENT_LEAVE_BALANCE}
            ),
        )

    return None


def _expense_interactive_clear_flags(snapshot: SessionSnapshot, msg: str) -> dict[str, Any]:
    """P02e — abandon stale expense prompts when user starts a new command."""
    from chat.services.expense.prompt_routing import (
        message_abandons_expense_prompt,
        snapshot_has_expense_interactive_prompt,
    )

    if snapshot_has_expense_interactive_prompt(snapshot) and message_abandons_expense_prompt(
        msg
    ):
        return {"clear_expense_interactive": True}
    return {}


def _decision_from_in_scope_utterance(
    utterance: Any,
    snapshot: SessionSnapshot,
    msg: str,
) -> SessionTurnDecision | None:
    """Tier U02 — in-scope HR acts from TUL map directly to router decisions."""
    from chat.services.turn_understanding.utterance_router_map import (
        utterance_maps_to_router_decision,
        utterance_router_act,
    )

    if not utterance_maps_to_router_decision(utterance):
        return None

    act_kind = utterance_router_act(utterance)
    if not act_kind:
        return None

    conf = float(getattr(utterance, "confidence", 0) or 0)
    clear_flags = _expense_interactive_clear_flags(snapshot, msg) or None

    if act_kind == "policy_query":
        return _decision(
            turn_kind=TurnKind.POLICY_QUERY,
            intent=INTENT_HR_POLICY,
            target_workflow=None,
            handler_id="policy_kb",
            reason="U02_policy_from_utterance",
            matched_predicate="resolve_utterance",
            confidence=conf,
            flags=clear_flags,
        )

    if act_kind == "balance_query":
        return _decision(
            turn_kind=TurnKind.BALANCE_QUERY,
            intent=INTENT_LEAVE_BALANCE,
            target_workflow=None,
            handler_id="leave_balance",
            reason="U02_balance_from_utterance",
            matched_predicate="resolve_utterance",
            confidence=conf,
        )

    if act_kind == "summary":
        return _decision(
            turn_kind=TurnKind.SUMMARY,
            intent=INTENT_EXPENSE_DAY_SUMMARY,
            target_workflow="expense",
            handler_id="expense_workflow",
            reason="U02_expense_summary_from_utterance",
            matched_predicate="resolve_utterance",
            confidence=conf,
        )

    return None


def route_session_turn(
    snapshot: SessionSnapshot,
    *,
    workflow_state: dict[str, Any] | None = None,
    trace_id: str = "",
    utterance: Any | None = None,
) -> SessionTurnDecision:
    """
    Evaluate P00–P99 priority matrix; first match wins.
    """
    msg = snapshot.message
    if not msg:
        return _decision(
            turn_kind=TurnKind.UNKNOWN,
            intent=None,
            target_workflow=None,
            handler_id="global_intent",
            reason="P99_empty_message",
        )

    # --- Tier U: Turn Understanding Layer (before wizard traps) ---
    if utterance is not None:
        from chat.services.turn_understanding.schemas import (
            ACT_NEEDS_CLARIFY,
            ACT_OUT_OF_SCOPE,
        )
        from chat.services.session_expected_answer import (
            message_plausibly_answers_prompt,
            snapshot_has_pending_prompt,
        )

        _leave_wizard_slot_answer = (
            (snapshot.leave_active or snapshot.leave_domain_active)
            and not snapshot.leave_review_pending
            and (
                (
                    snapshot_has_pending_prompt(snapshot)
                    and message_plausibly_answers_prompt(msg, snapshot)
                )
                or (
                    (snapshot.pending_leave_step or "") == "reason"
                    and re.search(r"^(?:কারণ|reason)\b", msg.strip(), re.I | re.UNICODE)
                )
                or re.search(r"^(?:কারণ|reason)\b", msg.strip(), re.I | re.UNICODE)
            )
        )

        if (
            getattr(utterance, "primary_act", "") == ACT_OUT_OF_SCOPE
            and float(getattr(utterance, "confidence", 0) or 0) >= 0.85
            and not getattr(utterance, "in_scope", True)
            and not _leave_wizard_slot_answer
        ):
            return _decision(
                turn_kind=TurnKind.OUT_OF_SCOPE,
                intent=INTENT_UNKNOWN,
                target_workflow=None,
                handler_id="policy_intent_helpers",
                reason="U00_out_of_scope",
                matched_predicate="resolve_utterance",
                confidence=float(utterance.confidence),
            )
        if getattr(utterance, "needs_clarify", False) and float(
            getattr(utterance, "confidence", 0) or 0
        ) >= 0.65:
            from chat.services.turn_understanding.resolver import (
                resolution_clarification_message,
            )

            return _decision(
                turn_kind=TurnKind.CONTEXT_CLARIFICATION,
                intent=INTENT_UNKNOWN,
                target_workflow=None,
                handler_id="message_context_clarity",
                reason="U01_utterance_clarify",
                matched_predicate="resolve_utterance",
                confidence=float(utterance.confidence),
                flags={
                    "clarification_prompt": resolution_clarification_message(
                        msg, utterance, snapshot=snapshot
                    ),
                },
            )

        u02 = _decision_from_in_scope_utterance(utterance, snapshot, msg)
        if u02 is not None:
            return u02

    # --- Tier 0: hard guards ---
    from chat.services.leave_meta_queries import wants_cancel_leave_command
    from chat.services.expense.wizard_commands import wants_cancel_expense_command

    if wants_cancel_leave_command(msg):
        return _decision(
            turn_kind=TurnKind.CANCEL,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_workflow",
            reason="P05_cancel_leave",
            matched_predicate="wants_cancel_leave_command",
        )

    if wants_cancel_expense_command(msg) and snapshot.expense_active:
        return _decision(
            turn_kind=TurnKind.CANCEL,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="expense_workflow",
            reason="P06_cancel_expense",
            matched_predicate="wants_cancel_expense_command",
        )

    if snapshot.is_cancel:
        return _decision(
            turn_kind=TurnKind.CANCEL,
            intent=INTENT_UNKNOWN,
            target_workflow=None,
            handler_id="workflow_cancel",
            reason="P00_cancel",
            matched_predicate="is_cancel",
        )

    if snapshot.duplicate_leave_choice_pending:
        from chat.services.leave.duplicate_choice import parse_duplicate_leave_choice

        if parse_duplicate_leave_choice(msg):
            return _decision(
                turn_kind=TurnKind.SLOT_ANSWER,
                intent=INTENT_LEAVE_REQUEST,
                target_workflow="leave",
                handler_id="leave.duplicate_choice",
                reason="P01_duplicate_leave_choice",
                matched_predicate="parse_duplicate_leave_choice",
            )

    if snapshot.expense_delete_verify_pending and (
        _is_confirmation_yes(msg) or _is_confirmation_no(msg)
    ):
        return _decision(
            turn_kind=TurnKind.DELETE_CONFIRM,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="expense.turn_router",
            reason="P02_delete_confirm",
            matched_predicate="expense_delete_verify_pending",
        )

    if snapshot.expense_ordinal_amount_confirm_pending and (
        _is_confirmation_yes(msg) or _is_confirmation_no(msg)
    ):
        return _decision(
            turn_kind=TurnKind.CONFIRM_YES if _is_confirmation_yes(msg) else TurnKind.CONFIRM_NO,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="expense.turn_router",
            reason="P02b_ordinal_amount_confirm",
            matched_predicate="expense_ordinal_amount_confirm_pending",
        )

    if snapshot.expense_amount_correction_pending:
        return _decision(
            turn_kind=TurnKind.CORRECTION,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="expense.turn_router",
            reason="P02c_amount_correction_pending",
            matched_predicate="expense_amount_correction_pending",
        )

    # P02d — unified expense active_prompt (delete pick/confirm) beats from_to slot tokens
    if snapshot.expense_active_prompt_kind and workflow_state:
        from chat.services.expense.active_prompt import (
            KIND_DELETE_CONFIRM,
            KIND_DELETE_PICK,
        )
        from chat.services.expense.interactive_pending import (
            message_answers_expense_interactive_pending,
        )
        from chat.services.session_snapshot import _read_expense_block_for_routing

        exp_block = _read_expense_block_for_routing(workflow_state)
        prompt_kind = str(snapshot.expense_active_prompt_kind or "")
        if message_answers_expense_interactive_pending(msg, exp_block):
            if prompt_kind == KIND_DELETE_CONFIRM and (
                _is_confirmation_yes(msg) or _is_confirmation_no(msg)
            ):
                return _decision(
                    turn_kind=TurnKind.DELETE_CONFIRM,
                    intent=INTENT_EXPENSE_CLAIM,
                    target_workflow="expense",
                    handler_id="expense.turn_router",
                    reason="P02d_delete_confirm_prompt",
                    matched_predicate="expense_active_prompt_delete_confirm",
                )
            if prompt_kind == KIND_DELETE_PICK:
                return _decision(
                    turn_kind=TurnKind.SLOT_ANSWER,
                    intent=INTENT_EXPENSE_CLAIM,
                    target_workflow="expense",
                    handler_id="expense.turn_router",
                    reason="P02d_delete_pick_prompt",
                    matched_predicate="expense_active_prompt_delete_pick",
                )

    # P41 early — expense summary beats slot collection (from_to / category open)
    from chat.services.expense.wizard_commands import wants_expense_done_command_rules
    from chat.services.expense.slots import STAGE_COLLECTING
    from chat.services.expense_workflow import wants_expense_summary

    if wants_expense_summary(msg) and (
        snapshot.expense_domain_active
        or snapshot.leave_review_pending
        or snapshot.has_suspended_expense
    ) and not (
        snapshot.expense_active
        and snapshot.expense_stage == STAGE_COLLECTING
        and wants_expense_done_command_rules(msg)
    ):
        return _decision(
            turn_kind=TurnKind.SUMMARY,
            intent=INTENT_EXPENSE_DAY_SUMMARY,
            target_workflow="expense",
            handler_id="expense_workflow",
            reason="P41b_expense_summary_early",
            matched_predicate="wants_expense_summary",
        )

    # P49 — post-submit leave navigation (MUST beat P71 balance / LLM misroute)
    if (
        snapshot.leave_submission_locked
        and not snapshot.has_suspended_leave
        and not snapshot.leave_active
        and not snapshot.duplicate_leave_prompt
    ):
        from chat.services.workflow_navigation import is_leave_navigation_phrase

        if is_leave_navigation_phrase(msg):
            return _decision(
                turn_kind=TurnKind.META_QUESTION,
                intent=INTENT_REQUEST_STATUS,
                target_workflow="leave",
                handler_id="leave.session_action_memory",
                reason="P49_post_submit_leave_nav",
                matched_predicate="is_leave_navigation_phrase",
            )

    # P48 — post-submit expense edit block (MUST beat P10 correction tier)
    if snapshot.expense_submission_locked:
        from chat.services.expense.session_action_memory import (
            looks_like_post_submit_expense_modification,
        )

        if looks_like_post_submit_expense_modification(workflow_state, msg):
            return _decision(
                turn_kind=TurnKind.META_QUESTION,
                intent=INTENT_EXPENSE_STATUS,
                target_workflow="expense",
                handler_id="expense.session_action_memory",
                reason="P48_post_submit_edit_blocked",
                matched_predicate="looks_like_post_submit_expense_modification",
            )

    # P43 — session meta (MUST beat P03/P04 submit commands and P80 slot tokens)
    from chat.services.leave.session_action_memory import wants_leave_meta_question
    from chat.services.leave_meta_queries import (
        wants_leave_session_summary,
        wants_pending_leave_show,
    )
    from chat.services.expense.session_action_memory import (
        wants_expense_meta_question,
        wants_expense_pre_submit_review,
        wants_post_submit_edit_question,
    )

    if wants_pending_leave_show(msg) and (
        snapshot.leave_domain_active or snapshot.has_suspended_leave
    ):
        return _decision(
            turn_kind=TurnKind.META_QUESTION,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_meta_queries",
            reason="P44_pending_leave_show",
            matched_predicate="wants_pending_leave_show",
        )

    if wants_leave_session_summary(msg) and snapshot.leave_summary_available:
        return _decision(
            turn_kind=TurnKind.SUMMARY,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_meta_queries",
            reason="P42_leave_summary",
            matched_predicate="wants_leave_session_summary",
        )

    if wants_leave_meta_question(msg):
        return _decision(
            turn_kind=TurnKind.META_QUESTION,
            intent=INTENT_REQUEST_STATUS,
            target_workflow="leave",
            handler_id="leave.session_action_memory",
            reason="P43_leave_meta",
            matched_predicate="wants_leave_meta_question",
        )

    if wants_expense_meta_question(msg) or wants_post_submit_edit_question(msg):
        if not (
            wants_expense_pre_submit_review(msg)
            and snapshot.expense_submit_confirm_pending
        ):
            return _decision(
                turn_kind=TurnKind.META_QUESTION,
                intent=INTENT_EXPENSE_STATUS,
                target_workflow="expense",
                handler_id="expense.session_action_memory",
                reason="P43_expense_meta",
                matched_predicate="wants_expense_meta_question",
            )

    # P54 — dual leave+expense submit disambiguation
    from chat.services.workflow_navigation import (
        build_dual_workflow_submit_clarification,
        wants_ambiguous_workflow_submit_command,
    )

    if (
        snapshot.leave_domain_active
        and snapshot.expense_domain_active
        and wants_ambiguous_workflow_submit_command(msg)
        and not snapshot.leave_submit_confirm_pending
        and not snapshot.leave_review_pending
        and not snapshot.expense_submit_confirm_pending
        and not snapshot.expense_review_pending
    ):
        return _decision(
            turn_kind=TurnKind.CONTEXT_CLARIFICATION,
            intent=INTENT_UNKNOWN,
            target_workflow=None,
            handler_id="workflow_navigation",
            reason="P54_dual_workflow_submit",
            matched_predicate="wants_ambiguous_workflow_submit_command",
            flags={
                "clarification_prompt": build_dual_workflow_submit_clarification(),
            },
        )

    # --- Tier 1: explicit commands ---
    from chat.services.leave_confirm import wants_leave_submit_command
    from chat.services.expense.wizard_commands import (
        wants_cancel_expense_command,
        wants_expense_submit_command,
    )
    if wants_leave_submit_command(msg) and snapshot.leave_domain_active:
        return _decision(
            turn_kind=TurnKind.SUBMIT_COMMAND,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_workflow",
            reason="P03_leave_submit_command",
            matched_predicate="wants_leave_submit_command",
        )

    from chat.services.expense.wizard_commands import (
        message_has_ingestible_claim_body,
        strip_expense_submit_tail_for_parse,
    )

    _submit_claim_body = strip_expense_submit_tail_for_parse(msg)
    _submit_mixed_with_claims = wants_expense_submit_command(
        msg
    ) and message_has_ingestible_claim_body(_submit_claim_body, original=msg)

    if (
        wants_expense_submit_command(msg)
        and snapshot.expense_domain_active
        and not (snapshot.leave_active and wants_leave_submit_command(msg))
        and not _submit_mixed_with_claims
    ):
        return _decision(
            turn_kind=TurnKind.SUBMIT_COMMAND,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="expense.turn_router",
            reason="P04_expense_submit_command",
            matched_predicate="wants_expense_submit_command",
            flags=_expense_interactive_clear_flags(snapshot, msg) or None,
        )

    # --- Tier 1b: policy (before correction false-positives on ``policy`` / ``ta``) ---
    if _is_policy_query(msg) or snapshot.policy_interrupt:
        return _decision(
            turn_kind=TurnKind.POLICY_QUERY,
            intent=INTENT_HR_POLICY,
            target_workflow=None,
            handler_id="policy_kb",
            reason="P70_policy_query",
            matched_predicate="_is_policy_query",
            flags=_expense_interactive_clear_flags(snapshot, msg) or None,
        )

    # --- Tier 2: corrections (before done / suspended-leave) ---
    from chat.services.expense.expense_confirm import looks_like_expense_correction
    from chat.services.suspended_leave_correction import looks_like_suspended_leave_correction
    from chat.services.leave.date_correction import looks_like_date_only_message
    from chat.services.leave.reason_correction_parser import looks_like_reason_correction
    from chat.services.wizard_turn_gate import looks_like_leave_review_update
    from chat.services.leave_confirm import parse_edit_slot, _looks_like_slot_correction
    from chat.services.intent_detector import _strong_expense_claim
    from chat.services.workflow_suspend import wants_resume_suspended_leave

    from chat.services.wizard_turn_gate import is_leave_collecting_slot_answer

    if (
        snapshot.has_suspended_leave
        and looks_like_suspended_leave_correction(msg)
        and not looks_like_expense_correction(msg)
        and not _strong_expense_claim(msg)
        and not wants_resume_suspended_leave(msg)
        and not is_leave_collecting_slot_answer(
            msg,
            pending_leave_step=snapshot.pending_leave_step,
            leave_active=snapshot.leave_active,
            leave_review_pending=snapshot.leave_review_pending,
        )
    ):
        return _decision(
            turn_kind=TurnKind.CORRECTION,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_workflow",
            reason="P11_suspended_leave_correction",
            matched_predicate="looks_like_suspended_leave_correction",
        )

    # --- Tier 2c: resume suspended workflow (before slot-first binding) ---
    from chat.services.expense_workflow import wants_resume_or_show_expense
    from chat.services.workflow_suspend import wants_resume_suspended_leave

    if snapshot.has_suspended_leave and wants_resume_suspended_leave(msg):
        return _decision(
            turn_kind=TurnKind.RESUME_SUSPENDED,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="workflow_suspend",
            reason="P52_resume_suspended_leave",
            matched_predicate="wants_resume_suspended_leave",
        )

    if snapshot.expense_domain_active and wants_resume_or_show_expense(msg):
        return _decision(
            turn_kind=TurnKind.RESUME_SUSPENDED,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="expense_workflow",
            reason="P53_resume_or_show_expense",
            matched_predicate="wants_resume_or_show_expense",
        )

    # --- Tier 5a: slot-first binding (MUST beat balance/meta/clarify heuristics) ---
    from chat.services.session_expected_answer import (
        message_plausibly_answers_prompt,
        snapshot_has_pending_prompt,
    )
    from chat.services.expense.session_action_memory import wants_expense_meta_question
    from chat.services.leave.session_action_memory import wants_leave_meta_question

    from chat.services.workflow_navigation import is_leave_application_message

    if (
        snapshot_has_pending_prompt(snapshot)
        and message_plausibly_answers_prompt(msg, snapshot)
        and not snapshot.balance_probe
        and not _is_policy_query(msg)
        and not is_leave_application_message(msg)
        and not wants_leave_meta_question(msg)
        and not wants_expense_meta_question(msg)
        and not looks_like_expense_correction(msg)
    ):
        from chat.services.leave_balance_intent import is_leave_balance_query
        from chat.services.expense_workflow import wants_expense_summary
        from chat.services.leave_meta_queries import wants_leave_session_summary
        from chat.services.expense.session_action_memory import (
            wants_expense_pre_submit_review,
        )

        if is_leave_balance_query(msg):
            pass
        elif wants_expense_summary(msg) or wants_leave_session_summary(msg):
            pass
        elif wants_expense_pre_submit_review(msg):
            pass
        else:
            domain = snapshot.active_prompt_domain
            if domain == "leave":
                return _decision(
                    turn_kind=TurnKind.SLOT_ANSWER,
                    intent=INTENT_LEAVE_REQUEST,
                    target_workflow="leave",
                    handler_id="leave_workflow",
                    reason="P79_slot_first_leave",
                    matched_predicate="message_plausibly_answers_prompt",
                    flags={
                        "active_prompt_slot": snapshot.active_prompt_slot,
                        "expected_answer_kind": snapshot.expected_answer_kind,
                    },
                )
            if domain == "expense":
                return _decision(
                    turn_kind=TurnKind.SLOT_ANSWER,
                    intent=INTENT_EXPENSE_CLAIM,
                    target_workflow="expense",
                    handler_id="expense.turn_router",
                    reason="P79_slot_first_expense",
                    matched_predicate="message_plausibly_answers_prompt",
                    flags={
                        "active_prompt_slot": snapshot.active_prompt_slot,
                        "expected_answer_kind": snapshot.expected_answer_kind,
                    },
                )

    # --- Tier 5b: informational interrupts (MUST beat P80 slot tokens) ---
    from chat.services.leave.user_goal import (
        build_leave_goal_clarification,
        needs_leave_goal_clarification,
    )

    if needs_leave_goal_clarification(msg, leave_active=snapshot.leave_active):
        return _decision(
            turn_kind=TurnKind.CONTEXT_CLARIFICATION,
            intent=INTENT_UNKNOWN,
            target_workflow=None,
            handler_id="message_context_clarity",
            reason="P45b_leave_goal_clarify",
            matched_predicate="needs_leave_goal_clarification",
            flags={
                "clarification_prompt": build_leave_goal_clarification(
                    msg, lang=snapshot.context_lines and None
                ),
            },
        )

    if snapshot.balance_probe:
        return _decision(
            turn_kind=TurnKind.BALANCE_QUERY,
            intent=INTENT_LEAVE_BALANCE,
            target_workflow=None,
            handler_id="leave_balance",
            reason="P45_balance_query",
            matched_predicate="balance_probe",
        )

    # A canonical leave token (paid/unpaid/full/half/sick/casual…) during ACTIVE
    # collecting is a slot answer — not a correction. Must precede the P12/P13
    # correction rules so "paid" at the payment step is not mis-routed. Kept
    # deliberately narrow (canonical tokens only) so expense claims like
    # "lunch 100, bus 50" still fall through to the P51 workflow switch.
    if (
        snapshot.leave_active
        and not snapshot.leave_review_pending
        and _leave_wizard_token(msg)
        and not snapshot.balance_probe
        and not _is_policy_query(msg)
        and not _strong_expense_claim(msg)
        and not looks_like_expense_correction(msg)
    ):
        return _decision(
            turn_kind=TurnKind.SLOT_ANSWER,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_workflow",
            reason="P80_leave_slot_token",
            matched_predicate="_leave_wizard_token",
        )

    if (
        snapshot.leave_domain_active
        and looks_like_date_only_message(msg)
        and not looks_like_expense_correction(msg)
        and not _strong_expense_claim(msg)
        and not (
            snapshot.leave_active
            and not snapshot.leave_review_pending
        )
    ):
        return _decision(
            turn_kind=TurnKind.CORRECTION,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_workflow",
            reason="P12b_date_correction",
            matched_predicate="looks_like_date_only_message",
        )

    if (
        snapshot.leave_domain_active
        and looks_like_reason_correction(msg)
        and not snapshot.balance_probe
        and not looks_like_expense_correction(msg)
        and not looks_like_date_only_message(msg)
        and not _strong_expense_claim(msg)
        # Wizard is asking for the reason right now: a plain reason sentence is a
        # slot answer (Tier 9), not a correction — unless it carries explicit
        # change/negation phrasing ("reason ta X hobe, Y na").
        and not (
            snapshot.leave_active
            and (snapshot.pending_leave_step or "") == "reason"
            and not _explicit_correction_marker(msg)
        )
    ):
        return _decision(
            turn_kind=TurnKind.CORRECTION,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_workflow",
            reason="P12a_reason_correction",
            matched_predicate="looks_like_reason_correction",
        )

    if (
        snapshot.expense_domain_active
        and looks_like_expense_correction(msg)
        and not snapshot.expense_submission_locked
    ):
        from chat.services.expense.expense_confirm import looks_like_new_expense_during_pending_slot
        from chat.services.session_snapshot import _read_expense_block_for_routing

        skip_p10 = False
        if (
            workflow_state
            and snapshot.pending_expense_step in ("category", "from_to")
        ):
            exp_block = _read_expense_block_for_routing(workflow_state)
            pending = exp_block.get("pending_line")
            if isinstance(pending, dict) and pending.get("amount"):
                skip_p10 = looks_like_new_expense_during_pending_slot(
                    msg,
                    pending,
                    list(exp_block.get("items") or []),
                    exp_block,
                    pending_step=str(snapshot.pending_expense_step or ""),
                )
        if not skip_p10:
            return _decision(
                turn_kind=TurnKind.CORRECTION,
                intent=INTENT_EXPENSE_CLAIM,
                target_workflow="expense",
                handler_id="expense.turn_router",
                reason="P10_expense_correction",
                matched_predicate="looks_like_expense_correction",
                flags={"skip_llm_intent": True},
            )

    if (
        snapshot.leave_review_pending
        and (looks_like_leave_review_update(msg) or parse_edit_slot(msg))
        and not looks_like_expense_correction(msg)
        and not _is_policy_query(msg)
    ):
        return _decision(
            turn_kind=TurnKind.CORRECTION,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_workflow",
            reason="P12_leave_review_correction",
            matched_predicate="looks_like_leave_review_update",
        )

    if (
        (parse_edit_slot(msg) or _looks_like_slot_correction(msg))
        and not _is_policy_query(msg)
        and not wants_leave_session_summary(msg)
        and not looks_like_expense_correction(msg)
        and not _strong_expense_claim(msg)
        # Plain answer to the reason step the wizard just asked → Tier 9 slot
        # continuation, not a correction (same guard as P12a).
        and not (
            snapshot.leave_active
            and (snapshot.pending_leave_step or "") == "reason"
            and not _explicit_correction_marker(msg)
        )
    ):
        if snapshot.leave_domain_active:
            return _decision(
                turn_kind=TurnKind.CORRECTION,
                intent=INTENT_LEAVE_REQUEST,
                target_workflow="leave",
                handler_id="leave_workflow",
                reason="P13_leave_slot_correction",
                matched_predicate="parse_edit_slot",
            )

    # --- Tier 3: duplicate / clarification ---
    if snapshot.duplicate_leave_prompt:
        return _decision(
            turn_kind=TurnKind.DUPLICATE_LEAVE,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_meta_queries",
            reason="P20_duplicate_leave_overlap",
            matched_predicate="check_overlapping_submitted_leave",
            flags={"duplicate_prompt": snapshot.duplicate_leave_prompt},
        )

    from chat.services.message_context_clarity import should_ask_context_clarification
    from chat.services.expense_extraction import message_contains_expense_claim_lines

    # A parseable expense claim ("bus 50 lunch 100") is never an underspecified
    # fragment — at cold start the router has no detected intent yet, so without
    # this guard P21 would wrongly clarify instead of starting the expense wizard.
    wizard_active_for_clarify = snapshot.leave_active or snapshot.expense_active
    if (
        not wizard_active_for_clarify
        and not message_contains_expense_claim_lines(msg)
        and should_ask_context_clarification(
            msg,
            list(snapshot.context_lines),
            intent=snapshot.provisional_intent,
            balance_probe=snapshot.balance_probe,
            leave_active=snapshot.leave_active,
            expense_active=snapshot.expense_active,
            workflow_continuation=snapshot.workflow_continuation,
            pending_prompt_snapshot=snapshot if snapshot.has_pending_prompt else None,
            workflow_state=workflow_state,
        )
    ):
        return _decision(
            turn_kind=TurnKind.CONTEXT_CLARIFICATION,
            intent=INTENT_UNKNOWN,
            target_workflow=None,
            handler_id="message_context_clarity",
            reason="P21_context_clarification",
            matched_predicate="should_ask_context_clarification",
        )

    # --- Tier 3b: done / summary / meta (before confirm — ``okay`` / ``শেষ`` ≠ yes) ---
    from chat.services.expense.session_action_memory import (
        wants_expense_meta_question,
        wants_expense_pre_submit_review,
        wants_post_submit_edit_question,
    )
    from chat.services.expense.wizard_commands import wants_expense_done_command_rules
    from chat.services.expense.slots import STAGE_COLLECTING
    from chat.services.expense_workflow import wants_expense_summary, wants_resume_or_show_expense
    from chat.services.workflow_suspend import wants_resume_suspended_leave

    if (
        snapshot.expense_active
        and snapshot.expense_stage == STAGE_COLLECTING
        and wants_expense_done_command_rules(msg)
        and not looks_like_expense_correction(msg)
    ):
        return _decision(
            turn_kind=TurnKind.DONE_COLLECTING,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="expense.turn_router",
            reason="P60_done_collecting",
            matched_predicate="wants_expense_done_command_rules",
        )

    if wants_expense_pre_submit_review(msg) and (
        snapshot.expense_submit_confirm_pending or snapshot.expense_review_pending
    ):
        return _decision(
            turn_kind=TurnKind.PRE_SUBMIT_REVIEW,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="expense.session_action_memory",
            reason="P40_pre_submit_review",
            matched_predicate="wants_expense_pre_submit_review",
        )

    if wants_expense_summary(msg) and (
        snapshot.expense_domain_active
        or snapshot.leave_review_pending
        or snapshot.has_suspended_expense
    ) and not (
        snapshot.expense_active
        and snapshot.expense_stage == STAGE_COLLECTING
        and wants_expense_done_command_rules(msg)
    ):
        return _decision(
            turn_kind=TurnKind.SUMMARY,
            intent=INTENT_EXPENSE_DAY_SUMMARY,
            target_workflow="expense",
            handler_id="expense_workflow",
            reason="P41_expense_summary",
            matched_predicate="wants_expense_summary",
        )

    # --- Tier 4: confirm / deny / defer ---
    from chat.services.leave_confirm import (
        wants_defer_expense_for_leave_submit,
        wants_defer_leave_for_expense_submit,
    )

    if wants_defer_expense_for_leave_submit(msg) and snapshot.has_suspended_expense:
        return _decision(
            turn_kind=TurnKind.DEFER_SUBMIT,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_confirm",
            reason="P32_defer_expense_for_leave_submit",
            matched_predicate="wants_defer_expense_for_leave_submit",
        )

    if (
        wants_defer_leave_for_expense_submit(msg)
        and snapshot.leave_submit_confirm_pending
        and snapshot.has_suspended_expense
    ):
        return _decision(
            turn_kind=TurnKind.DEFER_SUBMIT,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="leave_confirm",
            reason="P33_defer_expense_submit",
            matched_predicate="wants_defer_leave_for_expense_submit",
            flags={"leave_confirm_defer_expense": True},
        )

    if wants_defer_leave_for_expense_submit(msg) and snapshot.has_suspended_leave:
        return _decision(
            turn_kind=TurnKind.DEFER_SUBMIT,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="leave_confirm",
            reason="P33_defer_leave_for_expense_submit",
            matched_predicate="wants_defer_leave_for_expense_submit",
        )

    confirm_target = _confirm_workflow_target(snapshot)
    if _is_confirmation_yes(msg) and confirm_target:
        target, intent, handler = confirm_target
        return _decision(
            turn_kind=TurnKind.CONFIRM_YES,
            intent=intent,
            target_workflow=target,
            handler_id=handler,
            reason="P30_confirm_yes",
            matched_predicate="is_confirmation_yes",
        )

    if _is_confirmation_no(msg) and confirm_target:
        target, intent, handler = confirm_target
        return _decision(
            turn_kind=TurnKind.CONFIRM_NO,
            intent=intent,
            target_workflow=target,
            handler_id=handler,
            reason="P31_confirm_no",
            matched_predicate="is_confirmation_no",
        )

    if (
        (_is_confirmation_yes(msg) or _is_confirmation_no(msg))
        and snapshot.expense_active
        and str(snapshot.pending_expense_step or "").lower() != "clarify"
        and not snapshot.expense_delete_verify_pending
        and not snapshot.expense_ordinal_amount_confirm_pending
        and not snapshot.expense_amount_correction_pending
        and not snapshot.expense_review_pending
        and snapshot.expense_stage != "review"
        and not confirm_target
    ):
        return _decision(
            turn_kind=TurnKind.CONFIRM_YES if _is_confirmation_yes(msg) else TurnKind.CONFIRM_NO,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="expense.turn_router",
            reason="P30b_expense_wizard_confirm",
            matched_predicate="is_confirmation_yes",
        )

    # --- Tier 6: workflow switch / resume ---
    from chat.services.intent_detector import _strong_expense_claim
    from chat.services.workflow_navigation import is_leave_application_message

    if snapshot.expense_active and is_leave_application_message(msg):
        p50_flags = {"suspend_expense": True}
        p50_flags.update(_expense_interactive_clear_flags(snapshot, msg))
        return _decision(
            turn_kind=TurnKind.WORKFLOW_SWITCH,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="workflow_suspend",
            reason="P50_switch_to_leave",
            matched_predicate="is_leave_application_message",
            flags=p50_flags,
        )

    if (
        snapshot.leave_active
        and _strong_expense_claim(msg)
        and not _is_policy_query(msg)
    ):
        return _decision(
            turn_kind=TurnKind.WORKFLOW_SWITCH,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="workflow_suspend",
            reason="P51_switch_to_expense",
            matched_predicate="_strong_expense_claim",
            flags={"suspend_leave": True},
        )

    # --- Tier 7: wizard-specific deterministic rules (legacy gate parity) ---
    from chat.services.workflow_navigation import is_leave_navigation_phrase
    from chat.services.intent_detector import _message_answers_wizard_step

    if (
        snapshot.expense_active
        and is_leave_navigation_phrase(msg)
        and not is_leave_application_message(msg)
        and not snapshot.has_suspended_leave
        and not snapshot.leave_active
    ):
        return _decision(
            turn_kind=TurnKind.CHITCHAT,
            intent=INTENT_UNKNOWN,
            target_workflow=None,
            handler_id="expense_workflow",
            reason="P54_leave_nav_no_session",
            matched_predicate="is_leave_navigation_phrase",
            flags={"leave_nav_no_session": True},
        )

    from chat.services.expense.expense_total_dispute import is_expense_total_check_query
    from chat.services.expense.expense_draft_snapshots import wants_restore_expense_version
    from chat.services.expense.wizard_commands import is_expense_wizard_command
    if snapshot.expense_domain_active and is_expense_total_check_query(msg):
        return _decision(
            turn_kind=TurnKind.META_QUESTION,
            intent=INTENT_EXPENSE_STATUS,
            target_workflow="expense",
            handler_id="expense.expense_total_dispute",
            reason="P45_expense_total_check",
            matched_predicate="is_expense_total_check_query",
        )

    if snapshot.expense_domain_active and wants_restore_expense_version(msg):
        return _decision(
            turn_kind=TurnKind.SLOT_ANSWER,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="expense.expense_draft_snapshots",
            reason="P46_restore_expense_version",
            matched_predicate="wants_restore_expense_version",
        )

    from chat.services.expense.expense_confirm import looks_like_expense_correction

    if (
        snapshot.expense_active
        and is_expense_wizard_command(msg)
        and not looks_like_expense_correction(msg)
    ):
        return _decision(
            turn_kind=TurnKind.SUBMIT_COMMAND,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="expense.wizard_commands",
            reason="P47_expense_wizard_command",
            matched_predicate="is_expense_wizard_command",
        )

    if (
        snapshot.leave_active
        and not _is_policy_query(msg)
        and not wants_leave_session_summary(msg)
        and not wants_leave_meta_question(msg)
        and not wants_expense_meta_question(msg)
        and (
            _message_answers_wizard_step(msg, snapshot.pending_leave_step)
            or _leave_wizard_token(msg)
        )
    ):
        return _decision(
            turn_kind=TurnKind.SLOT_ANSWER,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_workflow",
            reason="P80_leave_slot_token",
            matched_predicate="_message_answers_wizard_step",
        )

    if workflow_state and snapshot.expense_active:
        from chat.services.workflow_navigation import is_leave_application_message

        if is_leave_application_message(msg):
            from chat.services.leave_meta_queries import check_duplicate_tomorrow_leave

            dup_msg = check_duplicate_tomorrow_leave(workflow_state)
            if dup_msg and re.search(
                r"agamikal|agamikal|আগামীকাল|tomorrow|kalke|kalker",
                msg or "",
                re.I | re.UNICODE,
            ):
                return _decision(
                    turn_kind=TurnKind.DUPLICATE_LEAVE,
                    intent=INTENT_LEAVE_REQUEST,
                    target_workflow="leave",
                    handler_id="leave_meta_queries",
                    reason="P49_duplicate_tomorrow_leave",
                    matched_predicate="check_duplicate_tomorrow_leave",
                    flags={"duplicate_prompt": dup_msg},
                )

    # --- Tier 8: cold-start leave / balance / chitchat / OOS ---
    if (
        not snapshot.leave_active
        and not snapshot.expense_active
        and _leave_application_excluding_policy(msg)
    ):
        return _decision(
            turn_kind=TurnKind.NEW_LEAVE,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_workflow",
            reason="P50c_new_leave_cold_start",
            matched_predicate="is_leave_application_message",
        )

    if snapshot.balance_probe:
        return _decision(
            turn_kind=TurnKind.BALANCE_QUERY,
            intent=INTENT_LEAVE_BALANCE,
            target_workflow=None,
            handler_id="leave_balance",
            reason="P71_balance_query",
            matched_predicate="balance_probe",
        )

    from chat.services.intent_detector import _looks_like_chitchat
    from chat.services.policy_intent_helpers import (
        is_general_knowledge_out_of_scope,
        is_off_topic_for_hr_assistant,
    )

    wizard_active = snapshot.leave_active or snapshot.expense_active

    off_topic = is_general_knowledge_out_of_scope(msg) or is_off_topic_for_hr_assistant(
        msg, wizard_active=wizard_active
    )
    if off_topic:
        pred = (
            "is_general_knowledge_out_of_scope"
            if is_general_knowledge_out_of_scope(msg)
            else "is_off_topic_for_hr_assistant"
        )
        return _decision(
            turn_kind=TurnKind.OUT_OF_SCOPE,
            intent=INTENT_UNKNOWN,
            target_workflow=None,
            handler_id="global_intent",
            reason="P73_out_of_scope",
            matched_predicate=pred,
        )

    if wizard_active and _looks_like_chitchat(msg, strict=True):
        return _decision(
            turn_kind=TurnKind.CHITCHAT,
            intent=INTENT_UNKNOWN,
            target_workflow=None,
            handler_id="wizard_side_question",
            reason="P72_chitchat",
            matched_predicate="_looks_like_chitchat",
        )

    # --- Tier 9: slot continuation / fallback ---
    from chat.services.expense.routing import looks_like_expense_wizard_continuation
    from chat.services.expense.clarify import looks_like_clarify_reply_signal

    if snapshot.expense_active and str(snapshot.pending_expense_step or "").lower() == "clarify":
        bare_confirm = _is_confirmation_yes(msg) or _is_confirmation_no(msg)
        if looks_like_clarify_reply_signal(msg) and not (
            bare_confirm and snapshot.expense_review_pending
        ):
            return _decision(
                turn_kind=TurnKind.SLOT_ANSWER,
                intent=INTENT_EXPENSE_CLAIM,
                target_workflow="expense",
                handler_id="expense.turn_router",
                reason="P81_clarify_reply",
                matched_predicate="looks_like_clarify_reply_signal",
            )

    if snapshot.expense_active and looks_like_expense_wizard_continuation(msg):
        return _decision(
            turn_kind=TurnKind.CONTINUE_WIZARD,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="expense.turn_router",
            reason="P81_expense_continuation",
            matched_predicate="looks_like_expense_wizard_continuation",
        )

    intr_decision = _wizard_interrupt_decision(
        snapshot, msg, workflow_state, trace_id=trace_id
    )
    if intr_decision is not None:
        return intr_decision

    if snapshot.leave_active and not snapshot.balance_probe:
        return _decision(
            turn_kind=TurnKind.CONTINUE_WIZARD,
            intent=INTENT_LEAVE_REQUEST,
            target_workflow="leave",
            handler_id="leave_workflow",
            reason="P82_continue_leave",
            matched_predicate="leave_active_fallback",
        )

    if snapshot.expense_active:
        return _decision(
            turn_kind=TurnKind.CONTINUE_WIZARD,
            intent=INTENT_EXPENSE_CLAIM,
            target_workflow="expense",
            handler_id="expense_workflow",
            reason="P83_continue_expense",
            matched_predicate="expense_active_fallback",
        )

    return _decision(
        turn_kind=TurnKind.UNKNOWN,
        intent=None,
        target_workflow=None,
        handler_id="global_intent",
        reason="P99_no_match",
        confidence=0.0,
    )


# ---------------------------------------------------------------------------
# Pre-router navigation rows (N50–N55)
#
# Resume / restore / switch are stateful: the P00–P99 matrix must classify
# against a snapshot that already reflects the navigation the user just asked
# for. These rows therefore run as a separate router-owned phase *before*
# ``route_session_turn``. The router decides (pattern + priority, first match
# per row, state threaded between rows); the orchestrator only persists each
# step and logs it — see ``orchestrator._apply_pre_router_navigation``.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NavigationStep:
    rule: str                   # priority row id, e.g. "N53_restore_suspended_leave"
    log_step: str               # observability step name (kept from legacy phase)
    state: dict[str, Any]       # workflow state after applying this step


def _leave_application_excluding_policy(message: str) -> bool:
    """New leave request phrasing — not a question about leave *policy*."""
    from chat.services.policy_intent_helpers import is_policy_interrupt_message
    from chat.services.workflow_navigation import is_leave_application_message

    if is_policy_interrupt_message(message):
        return False
    return is_leave_application_message(message)


def _answers_suspended_leave_step(message: str, workflow_state: dict[str, Any]) -> bool:
    from chat.services.intent_detector import _message_answers_wizard_step
    from chat.services.turn_classifier import _canonical_leave_wizard_token
    from chat.services.workflow_suspend import KEY_SUSPENDED_LEAVE

    sl = (workflow_state or {}).get(KEY_SUSPENDED_LEAVE) or {}
    step = sl.get("step")
    if not step:
        return False
    if _message_answers_wizard_step(message, step):
        return True
    return bool(_canonical_leave_wizard_token(message))


def plan_pre_router_navigation(
    message: str,
    workflow_state: dict[str, Any] | None,
    *,
    is_cancel: bool,
) -> list[NavigationStep]:
    """
    Evaluate navigation rows N50–N56 in priority order and return the steps to
    apply. Each row sees the state produced by the previous matched row, so the
    plan is deterministic and the caller can persist the steps one by one.
    """
    from chat.services.expense_workflow import (
        is_expense_in_progress,
        is_expense_paused,
        resume_expense_session,
        wants_resume_or_show_expense,
    )
    from chat.services.leave_workflow import (
        deactivate_leave_session,
        is_leave_in_progress,
        is_leave_paused,
        resume_leave_session,
    )
    from chat.services.policy_intent_helpers import is_policy_interrupt_message
    from chat.services.workflow_priority import (
        expense_query_should_suspend_leave,
        should_clear_misrouted_leave,
    )
    from chat.services.workflow_suspend import (
        clear_suspended_leave,
        has_suspended_expense,
        has_suspended_leave,
        restore_suspended_expense,
        restore_suspended_leave,
        suspend_leave_for_workflow_switch,
        switch_active_expense_to_suspended_leave,
        wants_resume_suspended_leave,
    )

    steps: list[NavigationStep] = []
    state: dict[str, Any] = workflow_state or {}

    def _apply(rule: str, log_step: str, new_state: dict[str, Any]) -> None:
        nonlocal state
        state = new_state
        steps.append(NavigationStep(rule=rule, log_step=log_step, state=new_state))

    # N50 — resume paused leave on any continuation except an explicit policy lookup.
    if is_leave_paused(state) and not is_cancel:
        if wants_resume_suspended_leave(message) or not is_policy_interrupt_message(message):
            _apply(
                "N50_resume_paused_leave",
                "leave_wizard_auto_resumed",
                resume_leave_session(state),
            )

    # N51 — explicit "resume leave" while an expense wizard is active → switch.
    if (
        not is_cancel
        and has_suspended_leave(state)
        and wants_resume_suspended_leave(message)
        and is_expense_in_progress(state)
        and not is_leave_in_progress(state)
    ):
        _apply(
            "N51_switch_expense_to_suspended_leave",
            "expense_suspended_resume_leave_nav",
            switch_active_expense_to_suspended_leave(state),
        )

    # N52 — paused expense: explicit leave resume wins, else resume the expense.
    if is_expense_paused(state) and not is_cancel:
        if wants_resume_suspended_leave(message) and has_suspended_leave(state):
            _apply(
                "N52a_paused_expense_to_suspended_leave",
                "expense_paused_resume_leave_nav",
                switch_active_expense_to_suspended_leave(state),
            )
        elif wants_resume_or_show_expense(message):
            _apply(
                "N52b_resume_paused_expense",
                "expense_wizard_auto_resumed",
                resume_expense_session(state),
            )

    # N53 — restore a leave parked while the user completed another task.
    if (
        has_suspended_leave(state)
        and not is_leave_in_progress(state)
        and not is_expense_in_progress(state)
        and not is_cancel
        and (
            wants_resume_suspended_leave(message)
            or _leave_application_excluding_policy(message)
            or _answers_suspended_leave_step(message, state)
        )
    ):
        _apply(
            "N53_restore_suspended_leave",
            "suspended_leave_restored",
            restore_suspended_leave(state, force_active=True),
        )

    # N54 — restore a parked expense when nothing else is active.
    if (
        has_suspended_expense(state)
        and not is_expense_in_progress(state)
        and not is_leave_in_progress(state)
        and not is_cancel
        and wants_resume_or_show_expense(message)
    ):
        _apply(
            "N54_restore_suspended_expense",
            "suspended_expense_restored",
            restore_suspended_expense(state),
        )

    # N55 — active leave: park it for an expense query, or clear a misroute.
    if is_leave_in_progress(state):
        if expense_query_should_suspend_leave(message):
            _apply(
                "N55a_suspend_leave_for_expense_query",
                "leave_suspended_for_expense_query",
                suspend_leave_for_workflow_switch(state),
            )
        elif should_clear_misrouted_leave(message, state):
            _apply(
                "N55b_clear_misrouted_leave",
                "misrouted_leave_cleared",
                clear_suspended_leave(deactivate_leave_session(state)),
            )

    # N56 — drop stale open expense draft after CRM submit (terminal lock).
    from chat.services.expense.session_action_memory import (
        has_expense_submission_lock,
        purge_stale_expense_draft_after_submit,
    )

    if has_expense_submission_lock(state):
        purged = purge_stale_expense_draft_after_submit(state)
        if purged is not state:
            _apply(
                "N56_purge_stale_expense_after_submit",
                "expense_stale_draft_purged_after_submit",
                purged,
            )

    return steps
