"""
Single-active-workflow priority rules (Phase 1).

Expense queries must not stay trapped in a stale leave wizard; misrouted leave
drafts (started from an expense question) are cleared when the user moves on.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.intent_detector import _strong_expense_day_summary
from chat.services.leave_fsm import ACTIVE_FLOW_LEAVE, read_leave_state


def expense_query_should_suspend_leave(message: str) -> bool:
    """True when the user is asking about expenses, not continuing leave."""
    try:
        from chat.services.expense_workflow import wants_expense_summary

        if wants_expense_summary(message) and not _strong_expense_day_summary(message):
            return False
    except Exception:
        pass
    if _strong_expense_day_summary(message):
        return True
    try:
        from chat.services.expense.expense_total_dispute import (
            is_expense_total_check_query,
        )

        if is_expense_total_check_query(message):
            return True
    except Exception:
        pass
    try:
        from chat.services.expense.session_action_memory import (
            wants_expense_meta_question,
        )

        if wants_expense_meta_question(message):
            return True
    except Exception:
        pass
    low = (message or "").lower()
    if re.search(
        r"(?:expense|খরচ).{0,30}(?:status|ref|reference)",
        low,
    ):
        return True
    if re.search(r"\bEXP-\d{4}-", message or "", re.I):
        return True
    return False


def leave_draft_looks_misrouted(workflow_state: dict[str, Any] | None) -> bool:
    """Leave draft whose reason is clearly an expense/status question, not leave."""
    st = read_leave_state(workflow_state)
    if st.get("active_flow") != ACTIVE_FLOW_LEAVE:
        return False
    draft = st.get("draft") or {}
    reason = str(draft.get("reason") or "").strip()
    if not reason:
        return False
    low = reason.lower()
    if re.search(
        r"(chuti|chhuti|leave|sick|medical|family|casual|annual|personal|maternity|"
        r"পারিবারিক|অসুস্থ|ছুটি)",
        low,
    ):
        return False
    if re.search(
        r"(cost|expense|khoroch|kharcha|total|taka|summery|summary|submit\s+kor)",
        low,
    ):
        return True
    if re.search(r"\bkoto\b", low) and re.search(
        r"(cost|expense|taka|খরচ)", low
    ):
        return True
    return False


def should_clear_misrouted_leave(
    message: str, workflow_state: dict[str, Any] | None
) -> bool:
    """Drop a misrouted leave draft when user starts a clear new HR action."""
    if not leave_draft_looks_misrouted(workflow_state):
        return False
    if expense_query_should_suspend_leave(message):
        return True
    low = (message or "").lower()
    if re.search(
        r"\b(sick|medical|casual|annual|maternity|paternity|bereavement)\s*leave\b",
        low,
    ):
        return True
    if re.search(r"(sick|medical)\s+leave\s+(daw|lagbe|submit|apply)", low):
        return True
    if re.search(r"(ছুটি|chuti|chhuti).{0,30}(lagbe|চাই|apply|submit|daw)", low):
        return True
    if _strong_expense_day_summary(message):
        return True
    return False


def expense_side_answer_during_leave(message: str) -> bool:
    """True when user is answering expense clarify/category, not continuing leave."""
    text = (message or "").strip()
    if not text:
        return False
    try:
        from chat.services.expense.clarify import parse_clarification_partial_confirm
        from chat.services.expense.expense_confirm import parse_category_slot_answer
        from chat.services.expense_extraction import parse_category_token

        if parse_category_slot_answer(text) or parse_category_token(text):
            return True
        if parse_clarification_partial_confirm(text, 5) is not None:
            return True
    except Exception:
        pass
    return False


def leave_wizard_help_hint(lang: str | None = None) -> str:
    """Deterministic hint during active leave draft (no conversational LLM)."""
    if lang == "bn":
        return (
            "ছুটি আবেদন চলছে। উদাহরণ:\n"
            "- paid / unpaid, full day / half day\n"
            "- কারণ লিখুন (যেমন: family program)\n"
            "- yes — জমা, edit — ঠিক করুন, cancel — বাতিল"
        )
    return (
        "Leave request in progress. Examples:\n"
        "- paid / unpaid, full day / half day\n"
        "- reason (e.g. family program)\n"
        "- yes to submit, edit to change, cancel to discard"
    )
