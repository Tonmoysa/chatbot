"""Read-only leave session queries: pending draft, summary, duplicate-date guard."""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from chat.services.bn_normalize import normalize_message_for_parsing
from chat.services.leave_confirm import build_leave_review_summary
from chat.services.leave_days import compute_requested_leave_days
from chat.services.leave_fsm import read_leave_state
from chat.services.workflow_suspend import KEY_SUSPENDED_LEAVE, has_suspended_leave

_PENDING_LEAVE_SHOW_RE = re.compile(
    r"(?:pending|চলমান|অসম্পূর্ণ).{0,30}(?:leave|ছুটি|chuti|chhuti|request|আবেদন)"
    r"|(?:leave|ছুটি|chuti|chhuti|request|আবেদন).{0,30}(?:pending|চলমান|দেখাও|dekhao|দেখ|show)"
    r"|pending\s+leave\s+request",
    re.I | re.UNICODE,
)
_LEAVE_SUMMARY_RE = re.compile(
    r"(?:leave|ছুটি|chuti|chhuti).{0,25}(?:summary|summery|সারাংশ|review|পর্যালোচনা)"
    r"|(?:summary|summery|সারাংশ).{0,25}(?:leave|ছুটি|chuti|chhuti)",
    re.I | re.UNICODE,
)
_CANCEL_LEAVE_RE = re.compile(
    r"(?:^|\b)(?:cancel\s*leave|leave\s*cancel|ছুটি\s*cancel|cancel\s*ছুটি|ছুটি\s*বাতিল|বাতিল\s*ছুটি)"
    r"(?:\s*[\.।]|$)",
    re.I | re.UNICODE,
)
_KEY_CANCEL_PENDING = "leave_cancel_verify_pending"


def wants_pending_leave_show(message: str) -> bool:
    raw = normalize_message_for_parsing(message)
    if re.search(r"\b(summary|summery|সারাংশ)\b", raw, re.I | re.UNICODE):
        return False
    return bool(_PENDING_LEAVE_SHOW_RE.search(raw))


def wants_leave_session_summary(message: str) -> bool:
    raw = normalize_message_for_parsing(message)
    if re.search(r"\bexpense\b|খরচ", raw, re.I):
        return False
    return bool(_LEAVE_SUMMARY_RE.search(raw))


def wants_cancel_leave_command(message: str) -> bool:
    raw = (message or "").strip()
    return bool(_CANCEL_LEAVE_RE.search(raw))


def is_leave_cancel_verify_pending(workflow_state: dict[str, Any] | None) -> bool:
    return bool((workflow_state or {}).get(_KEY_CANCEL_PENDING))


def mark_leave_cancel_verify_pending(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = dict(workflow_state or {})
    wf[_KEY_CANCEL_PENDING] = True
    return wf


def clear_leave_cancel_verify_pending(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = dict(workflow_state or {})
    wf.pop(_KEY_CANCEL_PENDING, None)
    return wf


def _active_leave_draft(workflow_state: dict[str, Any]) -> dict[str, Any] | None:
    wf = workflow_state or {}
    if has_suspended_leave(wf):
        sl = wf.get(KEY_SUSPENDED_LEAVE) or {}
        draft = dict(sl.get("draft") or {})
        return draft if draft else None
    st = read_leave_state(wf)
    if st.get("active_flow") == "leave":
        draft = dict(st.get("draft") or {})
        return draft if draft else None
    return None


def build_pending_leave_show_message(workflow_state: dict[str, Any]) -> str:
    draft = _active_leave_draft(workflow_state)
    if not draft:
        return "এই session-এ কোনো pending leave request নেই।"
    summary = build_leave_review_summary(draft)
    return f"**আপনার pending leave request:**\n\n{summary}"


def build_leave_session_summary_message(workflow_state: dict[str, Any]) -> str:
    from chat.services.leave_fsm import read_leave_last_submission, read_leave_state

    last = read_leave_last_submission(workflow_state)
    if last.get("submission_id") and not _active_leave_draft(workflow_state):
        draft = dict(last.get("draft") or {})
        summary = build_leave_review_summary(draft)
        ref = str(last.get("submission_id") or "")
        days = compute_requested_leave_days(draft)
        return (
            f"**জমা দেওয়া ছুটির সারাংশ** ({days:g} দিন) · ref: **{ref}**\n\n{summary}"
        )

    st = read_leave_state(workflow_state)
    if st.get("locked") and st.get("submission_id"):
        draft = dict(st.get("draft") or {})
        summary = build_leave_review_summary(draft)
        ref = str(st.get("submission_id") or "")
        days = compute_requested_leave_days(draft)
        return (
            f"**জমা দেওয়া ছুটির সারাংশ** ({days:g} দিন) · ref: **{ref}**\n\n{summary}"
        )
    draft = _active_leave_draft(workflow_state)
    if not draft:
        return "এই session-এ আপনার leave summary নেই — হয়তো cancel করা হয়েছে বা এখনো শুরু হয়নি।"
    summary = build_leave_review_summary(draft)
    days = compute_requested_leave_days(draft)
    return f"**ছুটির সারাংশ** ({days:g} দিন):\n\n{summary}"


def session_has_leave_on_date(
    workflow_state: dict[str, Any],
    target_iso: str,
) -> bool:
    draft = _active_leave_draft(workflow_state)
    if not draft:
        return False
    start = str(draft.get("start_date") or "")
    end = str(draft.get("end_date") or start)
    if not start:
        return False
    try:
        t = date.fromisoformat(target_iso)
        s = date.fromisoformat(start)
        e = date.fromisoformat(end) if end else s
        return s <= t <= e
    except ValueError:
        return start == target_iso


def build_duplicate_tomorrow_leave_message(*, tomorrow_iso: str) -> str:
    return (
        f"আপনার ইতিমধ্যে **আগামীকাল** ({tomorrow_iso})-এর জন্য একটি leave request "
        f"এই session-এ আছে। নতুন আবেদন করতে হলে আগেরটা edit বা cancel করুন।"
    )


def check_duplicate_tomorrow_leave(
    workflow_state: dict[str, Any],
    *,
    today: date | None = None,
) -> str | None:
    """Return block message if session already books tomorrow."""
    today_d = today or date.today()
    tomorrow = (today_d + timedelta(days=1)).isoformat()
    if session_has_leave_on_date(workflow_state, tomorrow):
        return build_duplicate_tomorrow_leave_message(tomorrow_iso=tomorrow)
    return None


def _submitted_leave_draft(workflow_state: dict[str, Any]) -> dict[str, Any] | None:
    from chat.services.leave_fsm import read_leave_last_submission, read_leave_state

    last = read_leave_last_submission(workflow_state)
    if last.get("submission_id"):
        draft = dict(last.get("draft") or {})
        return draft if draft else None
    st = read_leave_state(workflow_state)
    if st.get("locked") and st.get("submission_id"):
        draft = dict(st.get("draft") or {})
        return draft if draft else None
    return None


def _target_date_range_from_leave_message(
    message: str, *, today: date | None = None
) -> tuple[str, str] | None:
    from chat.services.bn_normalize import infer_bn_calendar_date, infer_bn_calendar_date_range

    today_d = today or date.today()
    rng = infer_bn_calendar_date_range(message, today=today_d)
    if rng:
        return rng[0], rng[1]
    single = infer_bn_calendar_date(message, today=today_d)
    if single:
        return single, single
    return None


def _target_date_from_leave_message(message: str, *, today: date | None = None) -> str | None:
    rng = _target_date_range_from_leave_message(message, today=today)
    return rng[0] if rng else None


def _iso_ranges_overlap(a_start: str, a_end: str, b_start: str, b_end: str) -> bool:
    try:
        as_ = date.fromisoformat(a_start)
        ae = date.fromisoformat(a_end or a_start)
        bs = date.fromisoformat(b_start)
        be = date.fromisoformat(b_end or b_start)
        return as_ <= be and bs <= ae
    except ValueError:
        return a_start == b_start


def build_duplicate_session_leave_message(
    *,
    target_iso: str,
    submission_id: str = "",
) -> str:
    ref = f" (ref: **{submission_id}**)" if submission_id else ""
    return (
        f"এই session-এ **{target_iso}** তারিখে ইতিমধ্যে একটি leave request **জমা** আছে{ref}।\n\n"
        f"আপনি কী করতে চান?\n"
        f"- **আগেরটা** দেখতে/চালিয়ে নিতে লিখুন: `আগের leave`\n"
        f"- **নতুন** আবেদন শুরু করতে লিখুন: `নতুন leave`"
    )


def _draft_covers_date(draft: dict[str, Any], target_iso: str) -> bool:
    start = str(draft.get("start_date") or "")
    end = str(draft.get("end_date") or start)
    if not start:
        return False
    try:
        t = date.fromisoformat(target_iso)
        s = date.fromisoformat(start)
        e = date.fromisoformat(end) if end else s
        return s <= t <= e
    except ValueError:
        return start == target_iso


def check_overlapping_submitted_leave(
    workflow_state: dict[str, Any],
    message: str,
    *,
    today: date | None = None,
) -> str | None:
    """Detect new leave request overlapping submitted or active leave in session."""
    if not re.search(
        r"(ছুটি|chuti|chhuti|leave).{0,30}(?:চাই|lagbe|lage|নিতে|লাগবে)",
        message or "",
        re.I | re.UNICODE,
    ):
        return None
    target_rng = _target_date_range_from_leave_message(message, today=today)
    if not target_rng:
        return None
    target_start, target_end = target_rng

    from chat.services.leave_fsm import is_leave_submission_locked, read_leave_state

    submitted = _submitted_leave_draft(workflow_state)
    if submitted:
        sub_start = str(submitted.get("start_date") or "")
        sub_end = str(submitted.get("end_date") or sub_start)
        if sub_start and _iso_ranges_overlap(
            target_start, target_end, sub_start, sub_end
        ):
            from chat.services.leave_fsm import read_leave_last_submission

            st = read_leave_state(workflow_state)
            last = read_leave_last_submission(workflow_state)
            ref = str(st.get("submission_id") or last.get("submission_id") or "")
            return build_duplicate_session_leave_message(
                target_iso=target_start,
                submission_id=ref,
            )

    active = _active_leave_draft(workflow_state)
    if active:
        act_start = str(active.get("start_date") or "")
        act_end = str(active.get("end_date") or act_start)
        if act_start and _iso_ranges_overlap(
            target_start, target_end, act_start, act_end
        ):
            if is_leave_submission_locked(workflow_state):
                st = read_leave_state(workflow_state)
                return build_duplicate_session_leave_message(
                    target_iso=target_start,
                    submission_id=str(st.get("submission_id") or ""),
                )
            return (
                f"এই session-এ **{target_start}** তারিখে ইতিমধ্যে একটি **leave draft** চলছে।\n\n"
                f"আপনি কী করতে চান?\n"
                f"- **আগেরটা** চালিয়ে নিতে লিখুন: `আগের leave`\n"
                f"- **নতুন** আবেদন শুরু করতে লিখুন: `নতুন leave`"
            )
    return None
