"""Expense utterance rules with confidence."""

from __future__ import annotations

from typing import Any


def probe_expense_act(message: str, snapshot: Any) -> tuple[str | None, float, str]:
    from chat.services.expense.expense_confirm import looks_like_bare_delete_request, looks_like_expense_correction
    from chat.services.expense.wizard_commands import (
        wants_cancel_expense_command,
        wants_expense_done_command_rules,
        wants_expense_submit_command,
    )
    from chat.services.expense_workflow import wants_expense_summary
    from chat.services.intent_detector import _strong_expense_claim
    from chat.services.turn_understanding.schemas import (
        ACT_ADD,
        ACT_CANCEL,
        ACT_DELETE,
        ACT_MODIFY,
        ACT_SUBMIT,
        ACT_SUMMARY,
        ACT_WORKFLOW_SWITCH,
    )
    from chat.services.workflow_navigation import is_leave_application_message

    msg = (message or "").strip()
    if not msg:
        return None, 0.0, ""

    if wants_expense_submit_command(msg):
        return ACT_SUBMIT, 0.95, "wants_expense_submit_command"
    if wants_cancel_expense_command(msg):
        return ACT_CANCEL, 0.95, "wants_cancel_expense_command"
    if looks_like_bare_delete_request(msg):
        return ACT_DELETE, 0.93, "looks_like_bare_delete_request"
    if looks_like_expense_correction(msg):
        return ACT_MODIFY, 0.88, "looks_like_expense_correction"
    if wants_expense_summary(msg):
        return ACT_SUMMARY, 0.9, "wants_expense_summary"
    if wants_expense_done_command_rules(msg):
        return ACT_SUBMIT, 0.85, "wants_expense_done_command_rules"
    if is_leave_application_message(msg):
        return ACT_WORKFLOW_SWITCH, 0.9, "is_leave_application_message"
    if _strong_expense_claim(msg):
        return ACT_ADD, 0.82, "_strong_expense_claim"

    return None, 0.0, ""
