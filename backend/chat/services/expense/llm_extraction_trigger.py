"""
Option A — when regex extraction is likely incomplete, force the LLM entity layer.

Routing stays in ``session_turn_router`` (rules-only). This module only gates
extraction/polish helpers per TURN_ROUTER_SPEC §10 (expense entity pipeline).
"""

from __future__ import annotations

import re

from chat.services.expense.expense_confirm import (
    is_confirmation_no,
    is_confirmation_yes,
    looks_like_compound_expense_claim,
    looks_like_expense_correction,
)

_AMOUNT_MENTION_RE = re.compile(
    r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?(?!\d)",
)
_LONG_MESSAGE_CHARS = 72
_MULTI_CLAUSE_RE = re.compile(
    r"[,;]| এবং | and | আর |\bar\b",
    re.I | re.UNICODE,
)


def count_distinct_amount_mentions(message: str) -> int:
    """Distinct numeric amounts mentioned in free text."""
    seen: set[float] = set()
    for m in _AMOUNT_MENTION_RE.finditer(message or ""):
        whole = m.group(1)
        frac = m.group(2) or ""
        try:
            val = float(f"{whole}.{frac}" if frac else whole)
        except ValueError:
            continue
        if val > 0:
            seen.add(round(val, 2))
    return len(seen)


def looks_like_long_compound_expense_message(message: str) -> bool:
    """
    Long or multi-clause expense utterance where regex often misses lines/routes.

    Excludes bare confirm/cancel and explicit correction phrasing (handled by
    turn_router rules).
    """
    raw = (message or "").strip()
    if not raw:
        return False
    if is_confirmation_yes(raw) or is_confirmation_no(raw):
        return False
    if looks_like_expense_correction(raw):
        return False
    if looks_like_compound_expense_claim(raw):
        return True
    if len(raw) >= _LONG_MESSAGE_CHARS and _MULTI_CLAUSE_RE.search(raw):
        if count_distinct_amount_mentions(raw) >= 1:
            return True
    if count_distinct_amount_mentions(raw) >= 3:
        return True
    low = raw.lower()
    if _MULTI_CLAUSE_RE.search(raw) and re.search(
        r"\b(bus|bike|lunch|snack|train|metro|rail|cng|rickshaw|travel|nasta|breakfast|dinner)\b",
        low,
    ):
        if count_distinct_amount_mentions(raw) >= 2:
            return True
    return False


def should_force_expense_llm_extraction(message: str) -> bool:
    """True when hybrid pipeline must call LLM even if the wizard gate is narrow."""
    return looks_like_long_compound_expense_message(message)
