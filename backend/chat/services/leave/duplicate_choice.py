"""
Duplicate leave session choice — after overlap detect, user picks continue vs new.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.bn_normalize import normalize_message_for_parsing
from chat.services.leave_fsm import read_leave_state

KEY_DUPLICATE_LEAVE_CHOICE = "leave_duplicate_choice_pending"


def mark_duplicate_leave_choice_pending(
    workflow_state: dict[str, Any],
    *,
    target_start: str = "",
    target_end: str = "",
) -> dict[str, Any]:
    wf = dict(workflow_state or {})
    wf[KEY_DUPLICATE_LEAVE_CHOICE] = {
        "target_start": target_start,
        "target_end": target_end or target_start,
    }
    return wf


def clear_duplicate_leave_choice_pending(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = dict(workflow_state or {})
    wf.pop(KEY_DUPLICATE_LEAVE_CHOICE, None)
    return wf


def is_duplicate_leave_choice_pending(workflow_state: dict[str, Any] | None) -> bool:
    return bool((workflow_state or {}).get(KEY_DUPLICATE_LEAVE_CHOICE))


def parse_duplicate_leave_choice(message: str) -> str | None:
    """
    Return 'continue' | 'new' | None.
    Handles: আগের leave, continue করব, নতুন leave, etc.
    """
    raw = normalize_message_for_parsing((message or "").strip())
    if not raw:
        return None
    low = raw.lower()

    if re.search(r"^আগের\s*leave\b", raw, re.I | re.UNICODE):
        return "continue"
    if re.search(r"\bcontinue\b", low) and re.search(r"\b(leave|ছুটি)\b", low):
        return "continue"
    if re.search(r"\b(continue|চালিয়ে|চালু)\b", low) and re.search(
        r"\b(leave|ছুটি|আগের|prior|পুরোনো)\b", low
    ):
        return "continue"
    if re.search(r"\b(আগের|পুরোনো|prior|previous)\b", low) and re.search(
        r"\b(leave|ছুটি)\b", low
    ):
        return "continue"

    if re.search(r"^নতুন\s*leave\b", raw, re.I | re.UNICODE):
        return "new"
    if re.search(r"\b(নতুন|new|fresh|আবার\s*নতুন)\b", low) and re.search(
        r"\b(leave|ছুটি|আবেদন)\b", low
    ):
        return "new"

    return None


def handle_duplicate_leave_choice_turn(
    workflow_state: dict[str, Any],
    message: str,
) -> dict[str, Any] | None:
    """Resolve pending duplicate choice; None if not pending or unrecognized."""
    if not is_duplicate_leave_choice_pending(workflow_state):
        return None

    choice = parse_duplicate_leave_choice(message)
    if not choice:
        return None

    wf = dict(workflow_state or {})

    if choice == "continue":
        from chat.services.leave_confirm import build_leave_review_summary
        from chat.services.leave_fsm import read_leave_last_submission
        from chat.services.leave_meta_queries import build_leave_session_summary_message

        wf = clear_duplicate_leave_choice_pending(wf)
        last = read_leave_last_submission(wf)
        if last.get("submission_id"):
            draft = dict(last.get("draft") or {})
            ref = str(last.get("submission_id") or "")
            summary = build_leave_review_summary(draft)
            msg = f"**জমা দেওয়া leave** · ref: **{ref}**\n\n{summary}"
        else:
            msg = build_leave_session_summary_message(wf)
        return {
            "workflow_state": wf,
            "complete": False,
            "confirmed_submit": False,
            "question": msg,
            "duplicate_choice": "continue",
        }

    if choice == "new":
        from chat.services.leave_fsm import STATUS_ACTIVE, apply_leave_state

        wf = clear_duplicate_leave_choice_pending(wf)
        # Fresh draft — do not seed overlap target dates (user picks dates next).
        draft: dict[str, Any] = {}
        wf = apply_leave_state(
            wf,
            draft=draft,
            step=None,
            status=STATUS_ACTIVE,
            review_pending=False,
        )
        return {
            "workflow_state": wf,
            "complete": False,
            "confirmed_submit": False,
            "duplicate_choice": "new",
            "restart": True,
        }

    return None
