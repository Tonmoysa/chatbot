"""
Strict gates for wizard review screens — distinguish slot updates from casual side talk.

At leave/expense confirmation, only explicit corrections should mutate the draft.
Weather observations and other off-topic statements (with or without "?") pause the wizard.
"""

from __future__ import annotations

import re

from chat.services.expense.expense_confirm import looks_like_expense_correction
from chat.services.leave_confirm import parse_edit_slot


def is_leave_navigation_phrase(message: str) -> bool:
    """Resume/back-to-leave navigation — not a draft field update."""
    try:
        from chat.services.workflow_suspend import wants_resume_suspended_leave

        return wants_resume_suspended_leave(message)
    except Exception:
        return False


_CASUAL_WEATHER_SMALLTALK_RE = re.compile(
    r"(?:"
    r"\b(gorom|garam|thanda|sheet|bristi|rain|weather|barof|heat|hot|cold|temp|humid|sunny|cloudy)\b|"
    r"(?:গরম|ঠান্ডা|বৃষ্টি|আবহাওয়া|আবহাঔয়া|বর্ষা)"
    r")",
    re.I | re.UNICODE,
)

_LEAVE_REVIEW_UPDATE_RE = re.compile(
    r"(?:"
    r"\b(paid|unpaid|lwop|sick|casual|annual|full|half|medical|maternity|paternity|emergency)\b|"
    r"full\s*day|half\s*day|"
    r"(?:হাফ|পুরো|বেতন|বেতনসহ|বেতন\s*ছাড়া)|"
    r"\b(reason|karon|cause|tarikh|tarik|date|dates)\b|"
    r"(?:কারণ|তারিখ)|"
    r"\b(leave|chuti|chhuti|chhuti|chutti|holiday|pto|sick|medical|betha|betah|oshustho|"
    r"অসুস্থ|ছুটি)\b|"
    r"\b(hobe|habe|change|update|correction|edit|badlao|badlan|ঠিক\s*কর|বদল)\b|"
    r"(?:হবে|বদল)|"
    r"(?:family|funeral|wedding|travel|village|janaza|ceremon|program|programme|"
    r"অনুষ্ঠান|গ্রাম|বিয়ে|জানাজা|প্রোগ্রাম)|"
    r"(?:kalke|kal|kalk|ajke|tomorrow|today|agamikal|parshu).{0,40}"
    r"(?:na|change|hobe|lagbe|chuti|leave|tarikh|date|karon|reason)"
    r")",
    re.I | re.UNICODE,
)


def looks_like_leave_review_update(message: str) -> bool:
    """True when text at leave review plausibly updates a leave slot (not casual talk)."""
    text = (message or "").strip()
    if not text:
        return False
    if parse_edit_slot(text):
        return True
    if is_leave_navigation_phrase(text):
        return False
    if _CASUAL_WEATHER_SMALLTALK_RE.search(text) and not re.search(
        r"\b(leave|chuti|chhuti|ছুটি|sick|medical|betha|betah|karon|reason|paid|unpaid)\b",
        text,
        re.I,
    ):
        return False
    return bool(_LEAVE_REVIEW_UPDATE_RE.search(text))


def is_casual_wizard_side_statement(message: str) -> bool:
    """
    Off-topic observation during a wizard (weather, trivia) — with or without "?".
    Does not fire for explicit leave/expense corrections or named HR/policy asks.
    """
    text = (message or "").strip()
    if not text:
        return False
    try:
        from chat.services.policy_intent_helpers import (
            is_hr_assistant_in_scope,
            is_policy_kb_query,
        )
    except Exception:
        return False
    if is_hr_assistant_in_scope(text) or is_policy_kb_query(text):
        return False
    if looks_like_leave_review_update(text) or looks_like_expense_correction(text):
        return False
    if _CASUAL_WEATHER_SMALLTALK_RE.search(text):
        return True
    return False
