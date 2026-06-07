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
    low = (message or "").lower()
    return bool(
        re.search(
            r"(ছুটি|chuti|chhuti|leave).{0,40}(চাই|lagbe|lage|apply|request|নিতে|লাগবে|nit(e)?\s*chai)",
            low,
        )
        or re.search(r"\b(apply|request)\s+(for\s+)?(a\s+)?leave\b", low)
    )


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
    balance_probe: bool = False,
) -> str:
    """
    Classify the user's message while a wizard is active.
    Priority: cancel > confirm > correction > policy/balance > chitchat > new workflow > slot.
    """
    if _is_cancel_form_request(message):
        return TURN_CANCEL

    if expense_active and wants_defer_expense_for_leave_submit(message):
        return TURN_NEW_WORKFLOW

    if expense_active and wants_resume_or_show_expense(message):
        return TURN_SLOT_ANSWER

    if expense_active and looks_like_expense_correction(message):
        return TURN_CORRECTION

    if (
        is_confirmation_yes(message)
        or is_confirmation_cancel(message)
        or expense_is_confirmation_yes(message)
        or expense_is_confirmation_no(message)
        or wants_expense_summary(message)
    ):
        return TURN_CONFIRM

    if balance_probe:
        return TURN_POLICY_QUERY

    if _is_policy_query(message):
        return TURN_POLICY_QUERY

    if _looks_like_chitchat(message, strict=True) or _is_fresh_start_greeting(message):
        return TURN_CHITCHAT

    if expense_active and _is_leave_application_message(message):
        return TURN_NEW_WORKFLOW

    if leave_active:
        if pending_leave_step in ("reason", "supporting_document"):
            if looks_like_wizard_side_question(message):
                return TURN_CHITCHAT
        if _message_answers_wizard_step(message, pending_leave_step) or _canonical_leave_wizard_token(
            message
        ):
            return TURN_SLOT_ANSWER

    if parse_edit_slot(message):
        return TURN_CORRECTION

    if _looks_like_slot_correction(message):
        return TURN_CORRECTION

    if expense_active and looks_like_expense_wizard_continuation(message):
        return TURN_SLOT_ANSWER

    if leave_active:
        if looks_like_wizard_side_question(message):
            return TURN_CHITCHAT
        if is_general_knowledge_out_of_scope(message):
            return TURN_CHITCHAT
        if is_off_topic_for_hr_assistant(message, wizard_active=True):
            return TURN_CHITCHAT
        return TURN_SLOT_ANSWER

    if expense_active:
        if looks_like_wizard_side_question(message):
            return TURN_CHITCHAT
        if is_general_knowledge_out_of_scope(message):
            return TURN_CHITCHAT
        return TURN_CHITCHAT

    return TURN_CHITCHAT
