"""Leave utterance rules with confidence."""

from __future__ import annotations

from typing import Any


def probe_leave_act(message: str, snapshot: Any) -> tuple[str | None, float, str]:
    from chat.services.leave_confirm import wants_leave_submit_command
    from chat.services.leave_meta_queries import wants_leave_session_summary
    from chat.services.turn_understanding.schemas import (
        ACT_SUBMIT,
        ACT_SUMMARY,
        ACT_WORKFLOW_SWITCH,
    )
    from chat.services.workflow_navigation import is_leave_application_message
    from chat.services.intent_detector import _strong_expense_claim

    msg = (message or "").strip()
    if not msg:
        return None, 0.0, ""

    if wants_leave_submit_command(msg):
        return ACT_SUBMIT, 0.95, "wants_leave_submit_command"
    if wants_leave_session_summary(msg):
        return ACT_SUMMARY, 0.88, "wants_leave_session_summary"
    if is_leave_application_message(msg):
        return ACT_WORKFLOW_SWITCH, 0.88, "is_leave_application_message"
    if _strong_expense_claim(msg):
        return ACT_WORKFLOW_SWITCH, 0.85, "_strong_expense_claim"

    return None, 0.0, ""
