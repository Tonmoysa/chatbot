"""
Decide when the leave workflow should call the LLM entity layer.

Short confirm/cancel tokens use rules only; slot answers and corrections use LLM.
"""

from __future__ import annotations

from chat.services.leave.llm_extraction_trigger import should_force_leave_llm_extraction
from chat.services.turn_classifier import TURN_CANCEL, TURN_CONFIRM


def leave_wizard_should_use_llm(
    message: str,
    *,
    workflow_turn: str | None,
) -> bool:
    """
    Return True when LLM structured extraction should run during an active leave wizard.

    Confirm/cancel turns and bare yes/no tokens skip LLM to avoid mis-parsing.
    """
    if workflow_turn in (TURN_CANCEL, TURN_CONFIRM):
        return False
    return True


def leave_extraction_should_use_llm(
    message: str,
    *,
    workflow_turn: str | None,
) -> bool:
    """Force LLM on long compound leave answers; otherwise wizard turn rules."""
    if should_force_leave_llm_extraction(message):
        return True
    return leave_wizard_should_use_llm(message, workflow_turn=workflow_turn)
