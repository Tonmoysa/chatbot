"""
Cross-workflow navigation vs new leave application.

Distinguishes "leave request e back koro" (resume/switch) from
"ami kalke sick leave nite chai" (start applying). Session gates live in
the orchestrator; this module holds shared phrase detection and user-facing copy.
"""

from __future__ import annotations

import re
from typing import Any

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
    try:
        from chat.services.leave_balance_intent import is_leave_balance_query

        if is_leave_balance_query(message):
            return False
    except Exception:
        pass
    try:
        from chat.services.leave_meta_queries import (
            wants_leave_submission_status,
            wants_leave_session_summary,
            wants_submitted_leave_details,
        )

        if (
            wants_leave_submission_status(message)
            or wants_submitted_leave_details(message)
            or wants_leave_session_summary(message)
        ):
            return False
    except Exception:
        pass
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


def format_post_submit_leave_locked_message(
    workflow_state: dict[str, Any] | None,
    *,
    lang: str | None = None,
) -> str:
    """Professional reply when user navigates to leave after CRM submit (no open draft)."""
    from chat.services.leave_fsm import read_leave_last_submission

    wf = workflow_state or {}
    last = read_leave_last_submission(wf)
    ref = str(last.get("submission_id") or "").strip()
    draft = dict(last.get("draft") or {})
    lt = str(draft.get("leave_type") or "").strip()
    start = str(draft.get("start_date") or "").strip()
    detail = f" · {lt}" if lt else ""
    date_part = f" · {start}" if start else ""

    if lang == "en":
        if ref:
            return (
                "Your leave request was **already submitted** in this chat session.\n"
                f"- **Reference:** `{ref}`{detail}{date_part}\n\n"
                "Final approval happens in your company's HR system. "
                "To start a **new** leave, say e.g. **ami kalke sick leave nite chai**."
            )
        return (
            "Your leave request was **already submitted** in this session. "
            "To apply again, start a new leave message."
        )

    if ref:
        return (
            "আপনার leave request **ইতিমধ্যে জমা হয়েছে** (এই session-এ)।\n"
            f"- **রেফারেন্স:** `{ref}`{detail}{date_part}\n\n"
            "চূড়ান্ত অনুমোদন আপনার কোম্পানির **HR সিস্টেমে** হবে।\n"
            "সারাংশ দেখতে বলুন — যেমন: **leave summary daw**।\n"
            "নতুন leave নিতে চাইলে বলুন — যেমন: **ami kalke sick leave nite chai**।"
        )
    return (
        "আপনার leave request **ইতিমধ্যে জমা হয়েছে** এই session-এ। "
        "আবার apply করতে নতুন leave message দিন।"
    )


def wants_ambiguous_workflow_submit_command(message: str) -> bool:
    """Generic submit/joma without naming leave vs expense (dual-session disambiguation)."""
    from chat.services.leave_confirm import _looks_like_generic_submit_command

    t = (message or "").strip()
    if not t or not _looks_like_generic_submit_command(t):
        return False
    has_leave = bool(
        re.search(r"\b(leave|chuti|chhuti|chutti|ছুটি|request)\b", t, re.I | re.UNICODE)
    )
    has_expense = bool(re.search(r"\b(expense|খরচ|kharcha|khoroch)\b", t, re.I | re.UNICODE))
    if has_leave and not has_expense:
        return False
    if has_expense and not has_leave:
        return False
    return True


def build_dual_workflow_submit_clarification(*, lang: str | None = None) -> str:
    """Ask which open workflow to submit when leave and expense are both active."""
    if lang == "en":
        return (
            "You have **both** a leave request and an expense draft open in this session.\n"
            "Which one do you want to **submit**?\n"
            "- **leave submit** — submit leave\n"
            "- **expense submit** — submit expense\n"
            "Reply e.g. `leave submit koro` or `expense submit koro`."
        )
    if lang == "banglish":
        return (
            "Ei session-e **leave** ar **expense** duita-i cholche.\n"
            "Kon ta **submit** korte chan?\n"
            "- **leave submit** — chuti joma\n"
            "- **expense submit** — kharcha joma\n"
            "Likhun: `leave submit koro` ba `expense submit koro`."
        )
    return (
        "এই session-এ **ছুটি** এবং **expense** দুটোই চলমান আছে।\n"
        "আপনি কোনটা **submit** করতে চান?\n"
        "- **leave submit** — ছুটি জমা\n"
        "- **expense submit** — খরচ জমা\n"
        "লিখুন: `leave submit koro` বা `expense submit koro`।"
    )


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
