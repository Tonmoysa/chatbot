"""
Option A — force LLM leave extraction on long compound slot answers.

Routing remains rules-only; this gates the leave entity pipeline only.
"""

from __future__ import annotations

import re

from chat.services.leave_confirm import is_confirmation_cancel, is_confirmation_yes


def looks_like_long_compound_leave_message(message: str) -> bool:
    """Comma-separated or multi-field leave replies need full LLM+regex merge."""
    raw = (message or "").strip()
    if not raw:
        return False
    if is_confirmation_yes(raw) or is_confirmation_cancel(raw):
        return False
    if re.search(r"[,;]| এবং | and ", raw, re.I | re.UNICODE):
        return True
    low = raw.lower()
    signals = 0
    if re.search(r"\b(paid|unpaid|lwop)\b", low):
        signals += 1
    if re.search(
        r"\b(sick|casual|annual|medical|emergency|maternity|paternity)\b", low
    ):
        signals += 1
    if re.search(r"\b(full|half)\b|full\s*day|half\s*day", low):
        signals += 1
    if re.search(
        r"\b(tomorrow|today|kal|agamikal|next\s+week|আগামীকাল|আজ)\b",
        low,
    ):
        signals += 1
    if re.search(
        r"\b(family|wedding|funeral|travel|program|পরিবার|অনুষ্ঠান)\b",
        low,
    ):
        signals += 1
    if re.search(r"\b(leave|chuti|chhuti|ছুটি|লিভ)\b", low, re.I | re.UNICODE):
        signals += 1
    return signals >= 2 or (len(raw) >= 80 and signals >= 1)


def should_force_leave_llm_extraction(message: str) -> bool:
    return looks_like_long_compound_leave_message(message)
