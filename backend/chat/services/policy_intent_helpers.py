"""Lightweight policy / rules topic detection for routing (no static handbook).

`rules_handbook.py` is kept in the repo for reference only; orchestrator and RAG
must not import it for answers. This module duplicates only the regex heuristics
needed so `IntentDetector` and `ChatOrchestrator` can recognize policy-shaped
messages without pulling in handbook data.
"""

from __future__ import annotations

import re

_RULES_QUERY_PATTERNS = (
    r"\b(rule|rules|regulation|regulations|policy|policies|handbook|guideline|guidelines)\b",
    r"\b(allowed|prohibited|must|mustn't|forbidden|mandatory|required|may\s+not)\b",
)

_BENGALI_RULES_HINT = (
    r"(নিয়ম|বিধি|নীতি|হ্যান্ডবুক|রুলস|পলিসি)",
    r"\b(niyom|niyam|bidhi|niti|rules?|policy|policies|handbook)\b",
)


def is_rules_query(message: str) -> bool:
    """True if the message is about rules / regulations / handbook topics."""
    if not message:
        return False
    low = message.lower()
    for pat in _RULES_QUERY_PATTERNS:
        if re.search(pat, low):
            return True
    for pat in _BENGALI_RULES_HINT:
        if re.search(pat, message) or re.search(pat, low):
            return True
    return False


_EXPENSE_SUBMIT_RE = re.compile(
    r"\b(submit|submitted|log(?:ged)?|spent|hoyeche|hoyese|claim|reimburse)\b",
    re.I,
)
_EXPENSE_SPEND_DOMAIN_RE = re.compile(
    r"\b(expense|kharcha|khoroch|খরচ|reimbursement)\b",
    re.I,
)
_AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?(?!\d)")


def is_expense_entitlement_query(message: str) -> bool:
    """
    User asks about daily allowance / TA-DA / per-day limits (policy lookup),
    not logging or summarizing actual spend.
    """
    if not message:
        return False
    low = message.lower()
    raw = message or ""
    if _EXPENSE_SUBMIT_RE.search(low) and (
        _EXPENSE_SPEND_DOMAIN_RE.search(low) or re.search(r"খরচ", raw)
    ):
        return False
    if _AMOUNT_RE.search(message) and re.search(
        r"\b(cost|kharcha|khoroch|hoyeche|taka|টাকা)\b", low
    ):
        return False

    if re.search(
        r"\b(allowance|allowances|travel\s+allowance|dearness\s+allowance|daily\s+allowance)\b",
        low,
    ):
        if re.search(r"\b(daily|per\s*day|each\s*day|protidin)\b", low) or re.search(
            r"\bkoto\b", low
        ):
            return True
        if re.search(r"\b(amar|my|entitled|rate|limit|cap)\b", low) or re.search(
            r"(কত|ভাতা)", raw
        ):
            return True

    if re.search(r"\b(ta\s*/\s*da|t\s*/\s*d|tada|ta\s+da)\b", low):
        return True

    if re.search(r"(দৈনিক\s*ভাতা|ভাতা\s*কত|টিএ|ডিএ|টিএ\s*/\s*ডিএ)", raw, re.I):
        return True

    if (
        re.search(r"\b(amar|my)\b", low)
        and re.search(r"\b(allowance|ta|da)\b", low)
        and re.search(r"\bkoto\b", low)
    ):
        return True

    if re.search(r"\b(per\s*day|protidin|protiti\s*din)\b", low) and re.search(
        r"\b(koto|limit|cap|rate|allowance|ta|da)\b", low
    ):
        if _EXPENSE_SPEND_DOMAIN_RE.search(low) or re.search(r"খরচ", raw):
            return False
        return True

    if re.search(r"\b(daily\s+budget|budget\s+koto|daily\s+cap)\b", low) or re.search(
        r"(দৈনিক\s*বাজেট|বাজেট\s*কত)", raw, re.I
    ):
        return True

    if re.search(r"\b(expense|reimbursement|reimburse)\b", low) and (
        is_rules_query(message)
        or re.search(r"\b(budget|cap|limit)\b", low)
        or re.search(r"(বাজেট|সীমা|নিয়ম)", raw)
    ):
        if _EXPENSE_SUBMIT_RE.search(low) and _AMOUNT_RE.search(message):
            return False
        return True

    return False


_BAD_ANSWER_COMPLAINT_RE = re.compile(
    r"(relation\s*nai|related\s*na|relevant\s*na|not\s*related|no\s*relation|"
    r"wrong\s*answer|hallucinat|manasse\s*nai|"
    r"প্রাসঙ্গিক\s*না|সম্পর্ক\s*নেই|মিল\s*নেই|ভুল\s*উত্তর|এই\s*উত্তর|"
    r"ei\s*ans|amar\s*question.{0,40}(sathe|satha).{0,20}(nai|ney|na))",
    re.I | re.UNICODE,
)


def is_irrelevant_answer_complaint(message: str) -> bool:
    """User says the bot's previous reply did not match their question."""
    if not message:
        return False
    return bool(_BAD_ANSWER_COMPLAINT_RE.search(message))
