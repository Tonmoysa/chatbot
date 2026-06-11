"""
Decide when the expense workflow should call the LLM entity layer.

Short confirm/cancel tokens use rules only; slot answers and free-form lines use LLM.
When LLM runs, ``fill_parser_gaps_with_llm`` can add missing routes/lines the regex missed.
"""

from __future__ import annotations

from chat.services.expense.llm_extraction_trigger import (
    should_force_expense_llm_extraction,
)
from chat.services.turn_classifier import TURN_CANCEL, TURN_CONFIRM


def expense_wizard_should_use_llm(
    message: str,
    *,
    workflow_turn: str | None,
) -> bool:
    """
    Return True when LLM structured extraction should run during an active expense wizard.

    Confirm/cancel turns skip LLM to avoid mis-parsing yes/no as amounts.
    """
    del message
    if workflow_turn in (TURN_CANCEL, TURN_CONFIRM):
        return False
    return True


def expense_extraction_should_use_llm(
    message: str,
    *,
    workflow_turn: str | None,
) -> bool:
    """
    Hybrid gate: force LLM on long compound claims; otherwise wizard turn rules.
    """
    if should_force_expense_llm_extraction(message):
        return True
    return expense_wizard_should_use_llm(message, workflow_turn=workflow_turn)
