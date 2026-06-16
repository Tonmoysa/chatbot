"""
Shared HR keyword / interrogative signals for LLM gates and regex guards.

Used by turn-understanding LLM fallback, hr_query_classifier, and expense-claim
heuristics so short Banglish HR queries are not blocked by length-only gates.
"""

from __future__ import annotations

import re
from typing import Any

# Broad HR-domain vocabulary (Bangla / Banglish / English).
HR_KEYWORD_RE = re.compile(
    r"(?:"
    r"expense|খরচ|কস্ট|এক্সপেন্স|reimbursement|reimburse|claim|"
    r"leave|ছুটি|লিভ|chuti|chhuti|"
    r"balance|baki|baaki|remaining|pto|"
    r"submit|জমা|approve|approval|pending|escalat|"
    r"policy|নিয়ম|নীতি|handbook|attendance|wfh|"
    r"manager|supervisor|status|track|draft|"
    r"koto|কত|ki|কি|কোন|koi|kothay|kothai|kothao|where"
    r")",
    re.I | re.UNICODE,
)

_INTERROGATIVE_RE = re.compile(
    r"(?:\?|কি|কোন|কত|ki\s|koto|koi|kothay|kothai|kothao|where|keno|why|how)",
    re.I | re.UNICODE,
)

_EXPENSE_DOMAIN_RE = re.compile(
    r"\b(expense|reimbursement|reimburse|claim|kharcha|khoroch)\b|খরচ",
    re.I | re.UNICODE,
)

_STATUS_LOCATOR_RE = re.compile(
    r"\b(status|track|where|koi|kothay|kothai|kothao|pending|approve|approved|"
    r"approval|missing|hoise|hoyeche|done|yet)\b|"
    r"(কোথায়|কই)",
    re.I | re.UNICODE,
)

_APPROVAL_RE = re.compile(
    r"(?:"
    r"\b(manager|supervisor|boss)\b.{0,30}\b(approve|approved|approval|pending|ok|done|"
    r"hoise|hoyeche)\b|"
    r"\b(approve|approval|escalat|escalate)\b"
    r")",
    re.I | re.UNICODE,
)


def message_has_hr_signal(message: str) -> bool:
    """True when the message likely relates to HR workflows (not pure chit-chat)."""
    raw = (message or "").strip()
    if not raw:
        return False
    return bool(HR_KEYWORD_RE.search(raw))


def message_has_interrogative_signal(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    return bool(_INTERROGATIVE_RE.search(raw))


def message_looks_like_expense_status_query(message: str) -> bool:
    """
    Short expense/reimbursement *lookup* phrasing without amounts — not a new claim.
    E.g. ``reimbursement ta koi?``, ``claim ta?``, ``expense koi?``.
    """
    raw = (message or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if not _EXPENSE_DOMAIN_RE.search(raw):
        return False
    if re.search(r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?(?!\d)", raw):
        return False
    if _STATUS_LOCATOR_RE.search(low) or _STATUS_LOCATOR_RE.search(raw):
        return True
    if re.search(r"\b(ta|tar|the)\b", low) and message_has_interrogative_signal(raw):
        return True
    return False


def message_looks_like_approval_query(message: str) -> bool:
    """Pending approval / manager sign-off questions."""
    raw = (message or "").strip()
    if not raw:
        return False
    return bool(_APPROVAL_RE.search(raw))


def should_try_utterance_llm(message: str, snapshot: Any) -> bool:
    """
    Information-based gate for Tier-U utterance LLM — not length-only.

    Skip very short pure chit-chat; call LLM when HR signals or wizard context
    suggests the message may be a short but meaningful HR query.
    """
    text = (message or "").strip()
    if len(text) < 4:
        return False

    wizard_active = bool(
        getattr(snapshot, "leave_active", False)
        or getattr(snapshot, "expense_active", False)
        or getattr(snapshot, "expense_domain_active", False)
        or getattr(snapshot, "leave_domain_active", False)
    )
    has_hr = message_has_hr_signal(text)

    try:
        from chat.services.intent_detector import _looks_like_chitchat

        if _looks_like_chitchat(text, strict=True) and not has_hr and not wizard_active:
            return False
    except Exception:
        pass

    if has_hr:
        return True

    if wizard_active and message_has_interrogative_signal(text):
        return True

    if getattr(snapshot, "has_pending_prompt", False) and len(text) >= 10:
        return True

    # Long free-form voice dumps without crisp keywords still benefit from LLM.
    if len(text) >= 48:
        return True

    return False
