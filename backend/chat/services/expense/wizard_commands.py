"""
Deterministic expense wizard commands (Phase 1).

Submit / done / remove phrasing must never fall through to conversational LLM.
"""

from __future__ import annotations

import re

from chat.services.expense_extraction import _CATEGORY_TOKEN

_SUBMIT_CMD_RE = re.compile(
    r"(?:"
    r"\b(?:submit|sumit|submmit|submite)\b(?:\s+(?:it|this|koro|kor|now|please|debo|daw|dao))?"
    r"|"
    r"(?:joma|জমা)\s*(?:daw|dao|de|diye|deb[eo]|koro|kor|kore\s*daw|kore\s*debo)"
    r"|"
    r"(?:joma|জমা)\s*(?:koro|kor)"
    r")",
    re.I | re.UNICODE,
)

_DONE_CMD_RE = re.compile(
    r"^(?:"
    r"no\s+more|nothing\s+more|that'?s\s+all|done|finish|শেষ|আর\s*নাই|"
    r"আর\s*কিছু\s*নাই|না\s*আর|bas|শুধু\s*এটুকু"
    r")\s*\.?$",
    re.I | re.UNICODE,
)

_REMOVE_VERB_CAT_RE = re.compile(
    rf"\b(?:remove|delete)\s+(?P<cat>{_CATEGORY_TOKEN})\b",
    re.I,
)

_REMOVE_TYPO_CAT_RE = re.compile(
    r"\b(?:remove|delete)\s+(?P<cat>rtain|rtrain|tran|trin)\b",
    re.I,
)


def wants_expense_submit_command(message: str) -> bool:
    """User wants to submit or finish the draft (joma daw, submit koro, …)."""
    t = (message or "").strip()
    if not t:
        return False
    try:
        from chat.services.leave_confirm import (
            wants_defer_expense_for_leave_submit,
            wants_defer_leave_for_expense_submit,
        )

        if wants_defer_leave_for_expense_submit(
            t
        ) or wants_defer_expense_for_leave_submit(t):
            return False
    except Exception:
        pass
    if re.search(r"\b(age|আগে|first|prothom|before)\b", t, re.I) and _SUBMIT_CMD_RE.search(
        t
    ):
        return False
    low = t.lower()
    if re.search(r"\b(leave|chuti|chhuti|holiday|request)\b", low, re.I) or re.search(
        r"(ছুটি|ছুটির)", t, re.I | re.UNICODE
    ):
        if _SUBMIT_CMD_RE.search(t) and re.search(
            r"\b(hoyeche|hoyese|hoise|hoyechilo|complete|status|ki)\b", low
        ):
            return False
    return bool(_SUBMIT_CMD_RE.search(t))


def wants_expense_done_command_rules(message: str) -> bool:
    """Fast rule path for finish-collecting (exact / common phrases)."""
    t = (message or "").strip()
    if not t:
        return False
    if _DONE_CMD_RE.match(t):
        return True
    return bool(re.search(r"\b(শেষ|আর\s*নেই|no\s+more)\b", t, re.I))


def wants_expense_done_command(
    message: str,
    *,
    trace_id: str = "",
    use_llm: bool = True,
) -> bool:
    """User signals collecting is complete (rules + optional LLM)."""
    from chat.services.expense.done_collecting import detect_finish_collecting_intent

    return detect_finish_collecting_intent(
        message, trace_id=trace_id, use_llm=use_llm
    )


def parse_remove_category_command(message: str) -> str | None:
    """Extract category from 'remove train' / 'delete lunch' (verb-first)."""
    low = (message or "").lower()
    m = _REMOVE_VERB_CAT_RE.search(low)
    if m:
        return m.group("cat")
    m = _REMOVE_TYPO_CAT_RE.search(low)
    if m:
        raw = m.group("cat")
        if raw in ("rtain", "rtrain", "tran", "trin"):
            return "train"
        return raw
    return None


def is_expense_wizard_command(message: str) -> bool:
    """True when message is a known expense wizard command (not free chit-chat)."""
    if wants_expense_submit_command(message) or wants_expense_done_command(message):
        return True
    if parse_remove_category_command(message):
        return True
    from chat.services.expense.expense_confirm import looks_like_expense_correction

    return looks_like_expense_correction(message)


def expense_wizard_help_hint(lang: str | None = None) -> str:
    """Deterministic hint when input is not understood during an active expense draft."""
    if lang == "bn":
        return (
            "খরচ ফর্ম চলছে। বলুন:\n"
            "- lunch 100, bus 50 office to badda\n"
            "- bus 50 hobe (review-তে ঠিক করতে)\n"
            "- remove train / train bad daw\n"
            "- done / **joma daw** / submit — পরের ধাপে\n"
            "- cancel — ফর্ম বাতিল"
        )
    return (
        "Expense form in progress. Try:\n"
        "- lunch 100, bus 50 office to badda\n"
        "- bus 50 (fix at review)\n"
        "- remove train\n"
        "- done / **joma daw** / submit for next step\n"
        "- cancel to discard the form"
    )
