"""Gate LLM correction parsing (Phase 2.5)."""

from __future__ import annotations

from typing import Any

from chat.services.expense.expense_confirm import (
    is_confirmation_no,
    is_confirmation_yes,
    looks_like_compound_expense_claim,
    looks_like_expense_correction,
)
from chat.services.expense.wizard_commands import (
    wants_expense_done_command,
    wants_expense_submit_command,
)


def correction_llm_should_use(
    message: str,
    items: list[dict[str, Any]] | None = None,
    *,
    review_stage: bool = False,
) -> bool:
    """
    Use LLM command parse only when rules may miss and message is a real correction.

    Review stage only — collecting uses structured line extraction instead.
    """
    del items
    if not review_stage:
        return False
    text = (message or "").strip()
    if not text:
        return False
    if is_confirmation_yes(text) or is_confirmation_no(text):
        return False
    if wants_expense_submit_command(text) or wants_expense_done_command(text):
        return False
    if looks_like_compound_expense_claim(text):
        return False
    return looks_like_expense_correction(text)
