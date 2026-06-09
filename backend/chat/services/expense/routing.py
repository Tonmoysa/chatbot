"""
Expense wizard turn routing — distinguish continuation vs unrelated side questions.
"""

from __future__ import annotations

import re


def is_expense_draft_status_question(message: str) -> bool:
    """Expense totals / draft completeness — not an unrelated side question."""
    from chat.services.expense.session_action_memory import wants_expense_meta_question
    from chat.services.expense.session_ledger import wants_session_expense_ledger_query
    from chat.services.intent_detector import _strong_expense_day_summary

    text = (message or "").strip()
    if not text:
        return False
    if wants_expense_meta_question(text):
        return True
    if wants_session_expense_ledger_query(text):
        return True
    if _strong_expense_day_summary(text):
        return True
    low = text.lower()
    if re.search(r"\b(expense|kharcha|khoroch|summary|summery|draft|pending)\b", low) or re.search(
        r"(খরচ|expense)", text, re.I
    ):
        if re.search(
            r"\b(keno|why|kothai|where|missing|only|shudhu|add|koto|total|mot)\b", low
        ) or re.search(r"\bbaki\s+(expense|line|gula|kharcha)\b", low):
            return True
    return False


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
        wants_expense_done_command_rules_only,
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
    if is_expense_draft_status_question(text):
        return True
    if wants_expense_submit_command(text) or wants_expense_done_command_rules_only(text):
        return True
    from chat.services.expense.clarify_praise import looks_like_wizard_praise_message

    if looks_like_wizard_praise_message(text):
        return True
    if is_expense_wizard_command(text):
        return True
    if wants_resume_or_show_expense(text):
        return True
    if wants_expense_summary(text):
        return True
    from chat.services.expense.clarify import looks_like_clarify_reply_signal

    if looks_like_clarify_reply_signal(text):
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
    return False
