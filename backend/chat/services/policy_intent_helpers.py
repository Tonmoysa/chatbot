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
