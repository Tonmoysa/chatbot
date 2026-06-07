"""
Expense wizard turn routing — distinguish continuation vs unrelated side questions.
"""

from __future__ import annotations

import re


def looks_like_expense_wizard_continuation(message: str) -> bool:
    """True when the user is clearly continuing the expense draft (not a side question)."""
    from chat.services.expense.expense_confirm import (
        is_confirmation_no,
        is_confirmation_yes,
        looks_like_expense_correction,
    )
    from chat.services.expense_extraction import (
        _looks_like_route_answer,
        parse_amount_only,
        parse_category_token,
    )
    from chat.services.expense.wizard_commands import (
        is_expense_wizard_command,
        wants_expense_done_command,
        wants_expense_submit_command,
    )
    from chat.services.expense_workflow import (
        wants_expense_summary,
        wants_resume_or_show_expense,
    )
    from chat.services.intent_detector import _strong_expense_claim

    text = (message or "").strip()
    if not text:
        return False
    if wants_expense_submit_command(text) or wants_expense_done_command(text):
        return True
    if is_expense_wizard_command(text):
        return True
    if wants_resume_or_show_expense(text):
        return True
    if wants_expense_summary(text):
        return True
    if is_confirmation_yes(text) or is_confirmation_no(text):
        return True
    if looks_like_expense_correction(text):
        return True
    if _strong_expense_claim(text):
        return True
    if parse_category_token(text) or parse_amount_only(text) is not None:
        return True
    if _looks_like_route_answer(text):
        return True
    if re.search(r"\d", text):
        return True
    return False
