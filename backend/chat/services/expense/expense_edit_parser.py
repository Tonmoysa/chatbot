"""
Structured expense line-edit parsing (rules-first).

Handles Banglish ordinals like ``1 no expense``, ``line 1``, ``#2`` and
explicit old→new amount swaps (``120 na 140 hobe``) for wizard corrections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from chat.services.expense.expense_confirm import _normalize_correction_message

_ORDINAL_INDEX_MAP = {
    "first": 0,
    "প্রথম": 0,
    "prothom": 0,
    "prothome": 0,
    "1st": 0,
    "second": 1,
    "দ্বিতীয়": 1,
    "ditio": 1,
    "ditiyo": 1,
    "dwitiyo": 1,
    "ditiyoto": 1,
    "2nd": 1,
    "third": 2,
    "তৃতীয়": 2,
    "tritio": 2,
    "tritiyo": 2,
    "3rd": 2,
    "fourth": 3,
    "চতুর্থ": 3,
    "choturtho": 3,
    "4th": 3,
}

_LAST_ORDINAL_RE = re.compile(
    r"(?:শেষ|শেষটা|last)\s*(?:expense|entry|line|খরচ|টা)?",
    re.I | re.UNICODE,
)

_WORD_ORDINAL_AMOUNT_RE = re.compile(
    r"(?:প্রথম|দ্বিতীয়|তৃতীয়|শেষ|শেষটা|first|second|third|last|prothom|ditio).{0,40}"
    r"(?:entry|line|expense|খরচ)(?:\s+টা)?\s+"
    r"(?P<old>\d+(?:[.,]\d+)?)\s*(?:না|na)\s*,?\s*(?P<new>\d+(?:[.,]\d+)?)",
    re.I | re.UNICODE,
)

_OLD_NA_NEW_RE = re.compile(
    r"(?P<old>\d{1,6}(?:[.,]\d{1,2})?)\s*(?:না|na)\s*,?\s*(?P<new>\d{1,6}(?:[.,]\d{1,2})?)",
    re.I | re.UNICODE,
)

_EXPLICIT_UPDATE_SIGNAL_RE = re.compile(
    r"(?:hobe|habe|hoy|হবে|হয়|kore\s*d(?:aw|e|ao|in)|replace|poriborto|update|change|"
    r"thik|ঠিক|korbo|করব|dao|daw|de|taka|tk|টাকা)",
    re.I | re.UNICODE,
)

_LINE_CONTEXT_RE = re.compile(
    r"\b(?:expense|entry|line|খরচ|টা)\b",
    re.I | re.UNICODE,
)


@dataclass(frozen=True)
class LineAmountUpdate:
    line_index: int
    new_amount: float
    old_amount: float | None = None
    source: str = "rules"


def _safe_amount(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return float(str(raw).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def parse_old_new_amount_pair(message: str) -> tuple[float, float] | None:
    """Extract ``old na new`` swap amounts from a correction message."""
    low = _normalize_correction_message(message)
    m = _OLD_NA_NEW_RE.search(low)
    if not m:
        return None
    old_amt = _safe_amount(m.group("old"))
    new_amt = _safe_amount(m.group("new"))
    if old_amt is None or new_amt is None:
        return None
    return old_amt, new_amt


def parse_line_index_from_message(
    message: str, *, item_count: int | None = None
) -> int | None:
    """
    Map ordinal / numeric line references to a 0-based item index.

    Supports: prothom, first, ``1 no``, ``1 number``, ``line 1``, ``#1``,
    ``no 1``, ``1 ta``, ``1 নং``.
    """
    low = _normalize_correction_message(message)
    if not low.strip():
        return None

    if _LAST_ORDINAL_RE.search(low):
        if item_count is not None and item_count > 0:
            return item_count - 1
        return None

    patterns = (
        r"#\s*(?P<n>\d{1,2})\b",
        r"\b(?:line|entry|no|number|nombor)\s*(?P<n>\d{1,2})\b",
        r"\b(?P<n>\d{1,2})\s*(?:no|number|nombor|নম্বর|নং)\b",
        r"\b(?P<n>\d{1,2})\s*(?:tatei|tite|te|ta|টা|টায়|টাই)\b",
        r"\b(?P<n>\d{1,2})(?:st|nd|rd|th)\b",
    )
    for pat in patterns:
        m = re.search(pat, low, re.I | re.UNICODE)
        if not m:
            continue
        try:
            idx = int(m.group("n")) - 1
        except (TypeError, ValueError):
            continue
        if idx >= 0 and (item_count is None or idx < item_count):
            return idx

    for word, idx in _ORDINAL_INDEX_MAP.items():
        if word.isascii():
            if re.search(rf"\b{re.escape(word)}\b", low, re.I):
                if item_count is None or idx < item_count:
                    return idx
        elif re.search(re.escape(word), low, re.I | re.UNICODE):
            if item_count is None or idx < item_count:
                return idx

    return None


def parse_line_amount_update(
    message: str, *, item_count: int | None = None
) -> LineAmountUpdate | None:
    """
    Parse a line-indexed amount correction when line + old→new are present.
    """
    low = _normalize_correction_message(message)
    if not low.strip():
        return None

    wm = _WORD_ORDINAL_AMOUNT_RE.search(low)
    if wm:
        idx = parse_line_index_from_message(message, item_count=item_count)
        new_amt = _safe_amount(wm.group("new"))
        old_amt = _safe_amount(wm.group("old"))
        if idx is not None and new_amt is not None:
            return LineAmountUpdate(
                line_index=idx,
                new_amount=new_amt,
                old_amount=old_amt,
                source="rules_word_ordinal",
            )

    pair = parse_old_new_amount_pair(message)
    if pair is None:
        return None

    idx = parse_line_index_from_message(message, item_count=item_count)
    if idx is None:
        return None

    old_amt, new_amt = pair
    return LineAmountUpdate(
        line_index=idx,
        new_amount=new_amt,
        old_amount=old_amt,
        source="rules_line_index",
    )


def is_explicit_line_amount_update(
    message: str,
    update: LineAmountUpdate,
    items: list[dict[str, Any]],
) -> bool:
    """
    True when the user gave an unambiguous line + old→new instruction that
    matches the draft — safe to apply without a yes/no confirm step.
    """
    if not (0 <= update.line_index < len(items)):
        return False
    if update.old_amount is None:
        return False

    current = float(items[update.line_index].get("amount") or 0)
    if abs(current - update.old_amount) > 0.01:
        return False

    low = _normalize_correction_message(message)
    if not _OLD_NA_NEW_RE.search(low):
        return False

    has_line_ref = parse_line_index_from_message(message, item_count=len(items)) is not None
    if not has_line_ref:
        return False

    if _EXPLICIT_UPDATE_SIGNAL_RE.search(low):
        return True
    if _LINE_CONTEXT_RE.search(low):
        return True
    return False


def should_auto_apply_line_amount_update(
    message: str,
    items: list[dict[str, Any]],
    *,
    line_index: int,
    new_amount: float,
    old_amount: float | None = None,
) -> bool:
    update = LineAmountUpdate(
        line_index=line_index,
        new_amount=new_amount,
        old_amount=old_amount,
        source="rules",
    )
    return is_explicit_line_amount_update(message, update, items)
