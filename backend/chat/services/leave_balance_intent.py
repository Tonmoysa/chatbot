"""
Deterministic leave-balance query detection (Bangla / Banglish / English).
Shared by orchestrator workflow gates and intent_detector.
"""

from __future__ import annotations

import re


def _is_leave_application_message(message: str) -> bool:
    """New leave request — not a balance lookup."""
    low = (message or "").lower()
    return bool(
        re.search(
            r"(ছুটি|chuti|chhuti|chutti|leave).{0,40}"
            r"(চাই|lagbe|lage|apply|request|নিতে|লাগবে|nit(e)?\s*chai|nite\s*chai)",
            low,
        )
        or re.search(r"\b(apply|request)\s+(for\s+)?(a\s+)?leave\b", low)
    )


def is_leave_balance_query(message: str) -> bool:
    """
    True when the user asks how much leave they have left — not applying for leave.
    """
    raw = (message or "").strip()
    if not raw:
        return False
    if _is_leave_application_message(raw):
        return False

    low = raw.lower()

    if re.search(
        r"\b(balance|remaining|left|pto|how\s+many\s+days|vacation\s+left|baki|baaki)\b",
        low,
    ):
        if re.search(r"\b(leave|chuti|chhuti|chutti|pto|vacation|holiday|ছুটি)\b", low) or re.search(
            r"(ছুটি|ছুটির)", raw
        ):
            return True
        if re.search(r"\b(balance|remaining|baki|baaki)\b", low):
            return True

    if re.search(r"(ছুটি\s*কত|কত\s*দিন|কয়\s*দিন|কতদিন|কয়দিন|কয়\s*টা\s*ছুটি)", raw):
        return True

    if re.search(r"\b(koto|koy|kon)\s*din\b", low) and re.search(
        r"\b(leave|chuti|chhuti|chutti|chuti|holiday|ছুটি)\b", low
    ):
        return True

    if re.search(r"\b(kotodin|koydin|kondin)\b", low):
        return True

    if re.search(
        r"\b(koy\s*ta|koyta|koto\s*ta|kotota)\s*(leave|chuti|chhuti|chutti|chuti)\b",
        low,
    ):
        return True

    if re.search(
        r"\b(leave|chuti|chhuti|chutti|chuti)\b.{0,25}\b(ache|ase|ase|baki|balance|remaining)\b",
        low,
    ) or re.search(
        r"\b(ache|ase|baki)\b.{0,25}\b(leave|chuti|chhuti|chutti)\b",
        low,
    ):
        return True

    if re.search(r"\b(amar|amr|my)\b", low) and re.search(
        r"\b(koy|koto|kotodin|koydin)\b", low
    ) and re.search(r"\b(leave|chuti|chhuti|chutti|ছুটি)\b", low):
        return True

    if re.search(r"\bhow\s+many\s+leave\b", low):
        return True

    return False
