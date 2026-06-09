"""Affirmative / invalid-location detection for expense clarify replies (P0 guardrails)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chat.services.expense.clarify import ClarificationIssue

# Whole-message affirmatives — never valid as location names.
CLARIFY_AFFIRMATIVE_ONLY_RE = re.compile(
    r"^(?:"
    r"yes|yep|yeah|yup|y|ok|okay|sure|fine|alright|correct|right|"
    r"ha|hae|haan|han|hmm|hmmm|ji|j|hoy|thik|"
    r"perfectly\s+ok(?:ay)?|thik\s*ache|thik\s*ae|"
    r"হ্যাঁ|হ্যা|ঠিক|ঠিক\s*আছে"
    r")\s*\.?!?$",
    re.I,
)

# Token search inside longer clarify replies.
CLARIFY_AFFIRMATIVE_TOKEN_RE = re.compile(
    r"\b("
    r"yes|yep|yeah|yup|ok|okay|ha|hae|haan|han|hmm|ji|j|hoy|thik|correct|right|"
    r"perfectly\s+ok(?:ay)?|thik\s*ache|"
    r"হ্যাঁ|হ্যা|ঠিক"
    r")\b",
    re.I,
)

_INVALID_LOCATION_VALUES = frozenset(
    {
        "yes",
        "yep",
        "yeah",
        "yup",
        "ok",
        "okay",
        "ha",
        "hae",
        "haan",
        "han",
        "hmm",
        "hmmm",
        "ji",
        "j",
        "hoy",
        "thik",
        "correct",
        "right",
        "sure",
        "fine",
        "হ্যাঁ",
        "হ্যা",
        "ঠিক",
    }
)


def is_clarify_affirmative_only(message: str) -> bool:
    """True when the entire message is a yes/confirm (ha, hae, thik, etc.)."""
    return bool(CLARIFY_AFFIRMATIVE_ONLY_RE.match((message or "").strip()))


def is_clarify_affirmative_token(text: str) -> bool:
    """True when text is or contains a standalone affirmative token."""
    raw = (text or "").strip()
    if not raw:
        return False
    if is_clarify_affirmative_only(raw):
        return True
    return bool(CLARIFY_AFFIRMATIVE_TOKEN_RE.fullmatch(raw))


# User praises the typo catch or admits a spelling mistake — not a place name.
_TYPO_ACKNOWLEDGMENT_RE = re.compile(
    r"(?:"
    r"banan\s*vul|vul\s*diyechi|vul\s*chilo|vul\s*chhilo|"
    r"spelling|typo|"
    r"detect\s*kor|perfe?ctly\s*detect|thik\s*detect|"
    r"awesome|thanks|thank\s*you|dhonnobad|"
    r"ami\s+.*\bvul\b"
    r")",
    re.I,
)


def looks_like_typo_acknowledgment(message: str) -> bool:
    """True when user agrees the bot caught their spelling mistake."""
    text = (message or "").strip()
    if not text:
        return False
    if _TYPO_ACKNOWLEDGMENT_RE.search(text):
        return True
    if re.search(r"\bvul\b", text, re.I) and len(text.split()) >= 3:
        return True
    return False


def is_invalid_clarify_location(value: str) -> bool:
    """True when value must not be stored as from/to location."""
    low = (value or "").strip().lower()
    if not low:
        return True
    if low in _INVALID_LOCATION_VALUES:
        return True
    if is_clarify_affirmative_only(low):
        return True
    return False


def is_implausible_clarify_location(
    value: str,
    *,
    issue: ClarificationIssue | None = None,
) -> bool:
    """True when value is clearly not a place (sentence, meta reply, etc.)."""
    if is_invalid_clarify_location(value):
        return True
    raw = (value or "").strip()
    if not raw:
        return True
    if looks_like_typo_acknowledgment(raw):
        return True
    words = raw.split()
    if len(words) > 4:
        return True
    if ".." in raw or raw.count(".") >= 2:
        return True
    if _TYPO_ACKNOWLEDGMENT_RE.search(raw):
        return True

    low = raw.lower()
    if issue:
        sug = (issue.suggestion or "").strip().lower()
        orig = (issue.original or "").strip().lower()
        if sug and (sug == low or sug in low or low in sug):
            return False
        if orig and low == orig:
            return True

    from chat.services.expense_locations import (
        KNOWN_EXPENSE_LOCATIONS,
        strip_location_punctuation,
    )

    cleaned = strip_location_punctuation(low)
    if cleaned in {c.lower() for c in KNOWN_EXPENSE_LOCATIONS}:
        return False
    if len(words) <= 2 and len(raw) <= 24:
        return False
    if len(raw) > 25:
        return True
    return False
