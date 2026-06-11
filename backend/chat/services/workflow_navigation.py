"""
Cross-workflow navigation vs new leave application.

Distinguishes "leave request e back koro" (resume/switch) from
"ami kalke sick leave nite chai" (start applying). Session gates live in
the orchestrator; this module holds shared phrase detection and user-facing copy.
"""

from __future__ import annotations

import re

from chat.services.workflow_suspend import wants_resume_suspended_leave


def is_leave_navigation_phrase(message: str) -> bool:
    """User wants to return to a leave draft — not supply leave slot data."""
    return wants_resume_suspended_leave(message)


def is_leave_application_message(message: str) -> bool:
    """
    New leave request phrasing — not navigation back and not leave *policy*.

    ``leave request e back koro`` is navigation, not application.
    ``leave request ta age submit koro`` is handled separately (defer submit).
    """
    if is_leave_navigation_phrase(message):
        return False
    low = (message or "").lower()
    if re.search(
        r"(ছুটি|chuti|chhuti|chutti|leave|লিভ|সিক\s*লিভ).{0,40}"
        r"(চাই|chai|lagbe|lage|apply|নিতে|লাগবে|nit(e)?\s*chai|nite\s*chai)",
        low,
        re.UNICODE,
    ):
        return True
    if re.search(
        r"\bleave\s+chai\b",
        low,
    ):
        return True
    if re.search(
        r"(?:agami|agamikal|next|coming|upcoming).{0,50}"
        r"\bleave\b.{0,30}(?:chai|lagbe|lage|nit(e)?\s*chai|apply)",
        low,
        re.UNICODE,
    ):
        return True
    if re.search(
        r"(?:\d{1,2}\s+)?"
        r"(?:january|february|march|april|may|june|july|august|september|october|november|december|"
        r"jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec|"
        r"জানু|ফেব|মার্চ|এপ্রিল|মে|জুন|জুলাই|আগস্ট|সেপ্ট|অক্ট|নভে|ডিস)"
        r".{0,40}\bleave\b.{0,30}(?:chai|lagbe|lage|nit(e)?\s*chai|apply)",
        low,
        re.UNICODE,
    ):
        return True
    if re.search(
        r"(শরীর\s*খারাপ|সরীর\s*খারাপ|অসুস্থ).{0,50}"
        r"(ছুটি|লিভ|leave|chuti|chhuti).{0,30}(লাগবে|lagbe|চাই|নিতে)",
        message or "",
        re.I | re.UNICODE,
    ):
        return True
    if re.search(
        r"(ছুটি|লিভ|leave|chuti).{0,30}(লাগবে|lagbe|চাই|নিতে).{0,40}"
        r"(শরীর\s*খারাপ|সরীর\s*খারাপ|অসুস্থ|soril\s*kharap)",
        message or "",
        re.I | re.UNICODE,
    ):
        return True
    if re.search(r"\b(apply|request)\s+(for\s+)?(a\s+)?leave\b", low):
        return True
    if re.search(
        r"leave\s+request.{0,30}(?:korte|kor[te]?|chai|lagbe|lag|apply|submit|daw|dao|nit[e]?)",
        low,
    ):
        return True
    return False


def format_no_active_leave_session_message(*, expense_active: bool = False) -> str:
    """Professional reply when user navigates to leave but no draft exists."""
    lines = [
        "আপনার **leave request** এই session-এ এখনো **চালু নেই** — "
        "ফিরে যাওয়ার মতো কোনো leave draft বা snapshot নেই।",
    ]
    if expense_active:
        lines.append(
            "Expense form **চলছে** — line যোগ/এডিট করতে পারেন, "
            "অথবা **done / joma daw** দিয়ে পরের ধাপে যান।"
        )
        lines.append(
            "নতুন leave শুরু করতে চাইলে বলুন — যেমন: "
            "**ami kalke sick leave nite chai**।"
        )
    else:
        lines.append(
            "নতুন leave শুরু করতে চাইলে বলুন — যেমন: "
            "**ami kalke sick leave nite chai**।"
        )
    return "\n".join(lines)
