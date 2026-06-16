"""
Session action memory for leave — meta/status/summary answers after submit.

Mirrors expense ``session_action_memory`` so post-submit questions use session
context instead of restarting the wizard.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.leave_confirm import build_leave_review_summary
from chat.services.leave_fsm import (
    has_leave_submission_history,
    read_leave_last_submission,
    read_leave_state,
)
from chat.services.leave_meta_queries import (
    wants_leave_session_summary,
    wants_leave_submission_status,
    wants_submitted_leave_details,
)

KEY_LAST_LEAVE_ACTION = "last_leave_action"


def wants_leave_meta_question(message: str) -> bool:
    """Umbrella: submitted leave status, details, or session summary."""
    return bool(
        wants_leave_submission_status(message)
        or wants_submitted_leave_details(message)
        or wants_leave_session_summary(message)
    )


def record_leave_submitted(
    workflow_state: dict[str, Any],
    *,
    submission_id: str,
    draft: dict[str, Any],
) -> dict[str, Any]:
    """Remember the last successful leave submit for meta answers."""
    wf = dict(workflow_state or {})
    wf[KEY_LAST_LEAVE_ACTION] = {
        "action_type": "leave_submitted",
        "submission_id": str(submission_id or ""),
        "draft": dict(draft or {}),
    }
    return wf


def read_last_leave_action(workflow_state: dict[str, Any] | None) -> dict[str, Any]:
    return dict((workflow_state or {}).get(KEY_LAST_LEAVE_ACTION) or {})


def format_leave_meta_answer(
    workflow_state: dict[str, Any] | None,
    message: str,
    *,
    lang: str | None = None,
) -> str:
    """Answer status / details / summary using session submission archive."""
    wf = workflow_state or {}
    raw = (message or "").strip()
    last = read_leave_last_submission(wf)
    action = read_last_leave_action(wf)
    ref = str(last.get("submission_id") or action.get("submission_id") or "")
    draft = dict(last.get("draft") or action.get("draft") or {})
    st = read_leave_state(wf)

    if wants_leave_session_summary(raw):
        if ref and draft:
            from chat.services.leave_meta_queries import build_leave_session_summary_message

            return build_leave_session_summary_message(wf)
        if st.get("submission_id") and st.get("draft"):
            from chat.services.leave_meta_queries import build_leave_session_summary_message

            return build_leave_session_summary_message(wf)
        active = dict(st.get("draft") or {})
        if active and st.get("active_flow") == "leave":
            summary = build_leave_review_summary(active)
            return f"**ছুটির সারাংশ (এখনো submit হয়নি):**\n\n{summary}"
        return (
            "এই session-এ এখনো কোনো leave **জমা হয়নি** বা summary নেই।"
        )

    if wants_submitted_leave_details(raw):
        if ref and draft:
            summary = build_leave_review_summary(draft)
            return (
                f"**জমা দেওয়া leave-এর তথ্য** · ref: **{ref}**\n\n{summary}"
            )
        if lang == "en":
            return "No submitted leave details found in this session yet."
        return "এই session-এ জমা দেওয়া leave-এর তথ্য পাওয়া যায়নি।"

    if wants_leave_submission_status(raw):
        if ref:
            lt = str(draft.get("leave_type") or "").strip()
            start = str(draft.get("start_date") or "").strip()
            detail = f" · {lt}" if lt else ""
            date_part = f" · {start}" if start else ""
            return (
                "হ্যাঁ — এই চ্যাট সেশনে আপনার leave request **জমা হয়েছে**।\n"
                f"- **রেফারেন্স:** `{ref}`{detail}{date_part}\n\n"
                "চূড়ান্ত অনুমোদন আপনার কোম্পানির HR সিস্টেমে হবে।"
            )
        active = dict(st.get("draft") or {})
        if active and st.get("active_flow") == "leave" and not st.get("locked"):
            return (
                "না — leave request **এখনো জমা হয়নি**।\n"
                "উপরের ধাপগুলো শেষ করে **yes** দিয়ে submit করুন।"
            )
        if lang == "en":
            return "No leave has been submitted in this session yet."
        return "না — এই session-এ এখনো কোনো leave **জমা হয়নি**।"

    if has_leave_submission_history(wf) and ref:
        summary = build_leave_review_summary(draft)
        return f"**শেষ জমা দেওয়া leave** · ref: **{ref}**\n\n{summary}"

    return "এই session-এ leave সম্পর্কিত তথ্য পাওয়া যায়নি।"
