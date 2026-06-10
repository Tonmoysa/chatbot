"""
Deterministic turn classification while a leave or expense workflow is active.

Used by the orchestrator to decide whether to run workflow turns, pause for
side questions, or treat a message as chit-chat — without LLM involvement.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense.expense_confirm import (
    is_confirmation_no as expense_is_confirmation_no,
    is_confirmation_yes as expense_is_confirmation_yes,
    looks_like_expense_correction,
)
from chat.services.expense.routing import looks_like_expense_wizard_continuation
from chat.services.expense_workflow import (
    wants_expense_summary,
    wants_resume_or_show_expense,
)
from chat.services.intent_detector import (
    _is_cancel_form_request,
    _is_fresh_start_greeting,
    _looks_like_chitchat,
    _message_answers_wizard_step,
    _strong_expense_claim,
    _strong_hr_policy,
    looks_like_wizard_side_question,
)
from chat.services.leave_confirm import (
    _looks_like_slot_correction,
    is_confirmation_cancel,
    is_confirmation_yes,
    parse_edit_slot,
    wants_defer_expense_for_leave_submit,
)
from chat.services.policy_intent_helpers import (
    is_expense_entitlement_query,
    is_general_knowledge_out_of_scope,
    is_off_topic_for_hr_assistant,
    is_rules_query,
)
from chat.services.wizard_turn_gate import (
    is_casual_wizard_side_statement,
    looks_like_leave_review_update,
)

TURN_SLOT_ANSWER = "SLOT_ANSWER"
TURN_CORRECTION = "CORRECTION"
TURN_CONFIRM = "CONFIRM"
TURN_POLICY_QUERY = "POLICY_QUERY"
TURN_CHITCHAT = "CHITCHAT"
TURN_NEW_WORKFLOW = "NEW_WORKFLOW"
TURN_CANCEL = "CANCEL"

_WORKFLOW_TURNS = frozenset(
    {TURN_SLOT_ANSWER, TURN_CORRECTION, TURN_CONFIRM}
)


def is_workflow_continuation_turn(turn_type: str) -> bool:
    return turn_type in _WORKFLOW_TURNS


def _is_policy_query(message: str) -> bool:
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


def _is_leave_application_message(message: str) -> bool:
    if _is_policy_query(message):
        return False
    from chat.services.workflow_navigation import is_leave_application_message

    return is_leave_application_message(message)


def _canonical_leave_wizard_token(message: str) -> bool:
    t = (message or "").strip().lower()
    if not t or len(t) > 48:
        return False
    if re.match(
        r"^(paid|unpaid|lwop|full|half|sick|casual|annual|emergency|maternity|paternity)(\s+day)?s?$",
        t,
    ):
        return True
    if re.match(r"^(full|half)\s*day$", t):
        return True
    return bool(re.search(r"(বেতনসহ|বেতন\s*ছাড়া)", message or ""))


def classify_workflow_turn(
    message: str,
    *,
    leave_active: bool,
    expense_active: bool,
    pending_leave_step: str | None = None,
    pending_expense_step: str = "",
    leave_review_pending: bool = False,
    expense_review_pending: bool = False,
    balance_probe: bool = False,
    has_suspended_expense: bool = False,
    has_expense_draft: bool = False,
) -> str:
    """
    Classify the user's message while a wizard is active.

    Phase 4: delegates to ``session_turn_router`` when possible; legacy rules
    remain as P99 fallback only.
    """
    from chat.services.session_snapshot import build_classifier_snapshot
    from chat.services.session_turn_bridge import workflow_turn_from_router_decision
    from chat.services.session_turn_router import route_session_turn

    snap = build_classifier_snapshot(
        message,
        leave_active=leave_active,
        expense_active=expense_active,
        pending_leave_step=pending_leave_step,
        pending_expense_step=pending_expense_step,
        leave_review_pending=leave_review_pending,
        expense_review_pending=expense_review_pending,
        balance_probe=balance_probe,
        has_suspended_expense=has_suspended_expense,
        has_expense_draft=has_expense_draft,
    )
    decision = route_session_turn(snap)
    if not decision.reason.startswith("P99"):
        routed = workflow_turn_from_router_decision(decision)
        if routed:
            return routed

    return _classify_workflow_turn_legacy(
        message,
        leave_active=leave_active,
        expense_active=expense_active,
        pending_leave_step=pending_leave_step,
        pending_expense_step=pending_expense_step,
        leave_review_pending=leave_review_pending,
        expense_review_pending=expense_review_pending,
        balance_probe=balance_probe,
        has_suspended_expense=has_suspended_expense,
        has_expense_draft=has_expense_draft,
    )


def _classify_workflow_turn_legacy(
    message: str,
    *,
    leave_active: bool,
    expense_active: bool,
    pending_leave_step: str | None = None,
    pending_expense_step: str = "",
    leave_review_pending: bool = False,
    expense_review_pending: bool = False,
    balance_probe: bool = False,
    has_suspended_expense: bool = False,
    has_expense_draft: bool = False,
) -> str:
    """Legacy priority chain — used only when router returns P99."""
    if _is_cancel_form_request(message):
        return TURN_CANCEL

    if expense_active and wants_defer_expense_for_leave_submit(message):
        return TURN_NEW_WORKFLOW

    if expense_active and wants_resume_or_show_expense(message):
        return TURN_SLOT_ANSWER

    if expense_active and str(pending_expense_step or "").strip().lower() == "clarify":
        from chat.services.expense.clarify import looks_like_clarify_reply_signal

        if looks_like_clarify_reply_signal(message):
            return TURN_SLOT_ANSWER

    expense_domain_active = expense_active or has_suspended_expense or has_expense_draft

    if expense_domain_active and looks_like_expense_correction(message):
        return TURN_CORRECTION

    if expense_domain_active:
        from chat.services.suspended_leave_correction import (
            looks_like_suspended_leave_correction,
        )

        if looks_like_suspended_leave_correction(message):
            return TURN_CORRECTION

    if expense_active and looks_like_expense_wizard_continuation(message):
        return TURN_SLOT_ANSWER

    if (
        is_confirmation_yes(message)
        or is_confirmation_cancel(message)
        or expense_is_confirmation_yes(message)
        or expense_is_confirmation_no(message)
    ):
        return TURN_CONFIRM

    if wants_expense_summary(message):
        return TURN_CHITCHAT

    if balance_probe:
        return TURN_POLICY_QUERY

    if _is_policy_query(message):
        return TURN_POLICY_QUERY

    if _looks_like_chitchat(message, strict=True) or _is_fresh_start_greeting(message):
        return TURN_CHITCHAT

    if expense_active and _is_leave_application_message(message):
        return TURN_NEW_WORKFLOW

    if expense_active or leave_active:
        from chat.services.wizard_interrupt_classifier import (
            WizardInterruptContext,
            classify_active_wizard_interrupt,
            interrupt_is_workflow_switch,
        )

        intr = classify_active_wizard_interrupt(
            message,
            leave_active=leave_active,
            expense_active=expense_active,
            leave_review_pending=leave_review_pending,
            expense_review_pending=expense_review_pending,
            pending_leave_step=pending_leave_step or "",
            use_llm=False,
        )
        if interrupt_is_workflow_switch(intr) and intr.maps_to_turn:
            return intr.maps_to_turn
        if intr.maps_to_turn == TURN_POLICY_QUERY:
            return TURN_POLICY_QUERY

    if leave_active:
        if pending_leave_step in ("reason", "supporting_document"):
            if looks_like_wizard_side_question(message):
                return TURN_CHITCHAT
            if is_casual_wizard_side_statement(message):
                return TURN_CHITCHAT
        if _message_answers_wizard_step(message, pending_leave_step) or _canonical_leave_wizard_token(
            message
        ):
            return TURN_SLOT_ANSWER

    if leave_review_pending or expense_review_pending:
        if leave_review_pending and (
            looks_like_leave_review_update(message) or parse_edit_slot(message)
        ):
            return TURN_CORRECTION
        if expense_review_pending and looks_like_expense_correction(message):
            return TURN_CORRECTION
        if (
            is_casual_wizard_side_statement(message)
            or looks_like_wizard_side_question(message)
            or is_general_knowledge_out_of_scope(message)
            or is_off_topic_for_hr_assistant(message, wizard_active=True)
        ):
            return TURN_CHITCHAT
        if leave_review_pending:
            return TURN_CHITCHAT
        if expense_review_pending and looks_like_expense_wizard_continuation(message):
            return TURN_SLOT_ANSWER
        if expense_review_pending:
            return TURN_CHITCHAT

    if parse_edit_slot(message):
        return TURN_CORRECTION

    if _looks_like_slot_correction(message):
        return TURN_CORRECTION

    if expense_active and looks_like_expense_wizard_continuation(message):
        return TURN_SLOT_ANSWER

    if leave_active:
        if looks_like_wizard_side_question(message):
            return TURN_CHITCHAT
        if is_casual_wizard_side_statement(message):
            return TURN_CHITCHAT
        if is_general_knowledge_out_of_scope(message):
            return TURN_CHITCHAT
        if is_off_topic_for_hr_assistant(message, wizard_active=True):
            return TURN_CHITCHAT
        if leave_review_pending:
            return TURN_CHITCHAT
        return TURN_SLOT_ANSWER

    if expense_active:
        if looks_like_wizard_side_question(message):
            return TURN_CHITCHAT
        if is_general_knowledge_out_of_scope(message):
            return TURN_CHITCHAT
        return TURN_CHITCHAT

    return TURN_CHITCHAT
