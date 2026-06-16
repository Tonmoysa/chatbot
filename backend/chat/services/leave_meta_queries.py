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
    r"|(?:summary|summery|সারাংশ).{0,25}(?:leave|ছুটি|chuti|chhuti)"
    r"|(?:leave|ছুটি|chuti|chhuti).{0,20}(?:summery|summary|সারাংশ).{0,15}(?:daw|dao|দাও|দেখাও|dekhao|bolo)",
    re.I | re.UNICODE,
)
_LEAVE_SUBMIT_STATUS_RE = re.compile(
    r"(?:"
    r"(?:ki|kono|any).{0,35}(?:leave|chuti|chhuti|chutti|ছুটি).{0,35}"
    r"(?:apply|submit|joma|জমা|request).{0,25}(?:korechi|korchi|kor[eo]chi|hoyeche|hoise|hoise|done|হয়েছে|হয়েছে)"
    r"|"
    r"(?:leave|chuti|chhuti|chutti|ছুটি|request).{0,35}"
    r"(?:apply|submit|joma|জমা).{0,25}(?:korechi|korchi|kor[eo]chi|hoyeche|hoise|done|হয়েছে|হয়েছে)"
    r"|"
    r"(?:submit|joma|জমা).{0,20}(?:hoyeche|hoise|হয়েছে|হয়েছে).{0,25}(?:leave|chuti|chhuti|ছুটি)"
    r"|"
    r"(?:amar|my).{0,20}(?:leave|chuti|chhuti).{0,25}(?:submit|joma|জমা).{0,15}(?:hoyeche|hoise|হয়েছে)"
    r")",
    re.I | re.UNICODE,
)
_SUBMITTED_LEAVE_DETAILS_RE = re.compile(
    r"(?:"
    r"(?:sei|that|last|submitted|joma|জমা).{0,30}(?:leave|chuti|chhuti|chutti|ছুটি|request)"
    r".{0,35}(?:info|information|details|summary|summery|তথ্য|সারাংশ|dekhao|দেখাও|daw|dao|দাও|bolo)"
    r"|"
    r"(?:leave|chuti|chhuti|chutti|ছুটি).{0,25}(?:info|information|details|তথ্য).{0,20}(?:daw|dao|দাও|dekhao|দেখাও|bolo)"
    r"|"
    r"(?:ref|reference|রেফারেন্স).{0,20}(?:leave|chuti|chhuti|ছুটি)"
    r")",
    re.I | re.UNICODE,
)
_CANCEL_LEAVE_RE = re.compile(
    r"(?:^|\b)(?:"
    r"cancel\s+(?:the\s+)?(?:leave|chuti|chhuti|chutti|ছুটি)(?:\s+request)?"
    r"|cancel\s+(?:the\s+)?leave\s+request"
    r"|cancel\s*leave"
    r"|leave\s*cancel"
    r"|ছুটি\s*cancel"
    r"|cancel\s*ছুটি"
    r"|ছুটি\s*বাতিল"
    r"|বাতিল\s*ছুটি"
    r")(?:\s*[\.।]|$)",
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


def wants_leave_submission_status(message: str) -> bool:
    """User asks whether they already applied/submitted leave in this session."""
    raw = normalize_message_for_parsing(message)
    if re.search(r"\bexpense\b|খরচ", raw, re.I):
        return False
    if wants_leave_session_summary(raw) or wants_submitted_leave_details(raw):
        return False
    return bool(_LEAVE_SUBMIT_STATUS_RE.search(raw))


def wants_submitted_leave_details(message: str) -> bool:
    """User wants details of a leave already submitted in session."""
    raw = normalize_message_for_parsing(message)
    if re.search(r"\bexpense\b|খরচ", raw, re.I):
        return False
    if wants_leave_session_summary(raw):
        return False
    return bool(_SUBMITTED_LEAVE_DETAILS_RE.search(raw))


def session_has_leave_summary_context(workflow_state: dict[str, Any] | None) -> bool:
    """True when this session has a leave draft or a submitted leave to summarize."""
    from chat.services.leave_fsm import read_leave_last_submission

    wf = workflow_state or {}
    if _active_leave_draft(wf):
        return True
    if read_leave_last_submission(wf).get("submission_id"):
        return True
    st = read_leave_state(wf)
    return bool(st.get("submission_id") or st.get("locked"))


_PARALLEL_LEAVE_BLOCK_BN = (
    "আপনার **আগের leave request** এখনো চলছে — আগে সেটা **submit** করুন অথবা **cancel** করুন, "
    "তারপর নতুন leave request শুরু করতে পারবেন।"
)
_PARALLEL_LEAVE_BLOCK_EN = (
    "Your **previous leave request** is still in progress — please **submit** or **cancel** it "
    "before starting a new leave request."
)


def _leave_has_parallel_block_progress(workflow_state: dict[str, Any]) -> bool:
    """True when an active leave wizard already has draft progress worth protecting."""
    st = read_leave_state(workflow_state)
    if st.get("review_pending"):
        return True
    if st.get("step"):
        return True
    draft = dict(st.get("draft") or {})
    progress_keys = ("start_date", "end_date", "leave_type", "reason", "days", "day_scope")
    return any(draft.get(k) for k in progress_keys)


def should_block_parallel_leave_application(
    message: str,
    workflow_state: dict[str, Any] | None,
) -> bool:
    """
    Predicate (R04): block a fresh leave application while another leave wizard is active.
    Corrections, date edits, submit, and duplicate-overlap flows are excluded.
    """
    from chat.services.leave_fsm import is_leave_in_progress
    from chat.services.workflow_navigation import is_leave_application_message

    wf = workflow_state or {}
    if not is_leave_in_progress(wf):
        return False
    from chat.services.leave_fsm import (
        ACTIVE_FLOW_LEAVE,
        STATUS_ACTIVE,
        read_leave_state,
    )

    st = read_leave_state(wf)
    if (
        st.get("active_flow") == ACTIVE_FLOW_LEAVE
        and st.get("step")
        and not st.get("review_pending")
        and str(st.get("status") or "") == STATUS_ACTIVE
    ):
        return False
    if not _leave_has_parallel_block_progress(wf):
        return False
    if not is_leave_application_message(message):
        return False
    from chat.services.leave.date_correction import looks_like_date_only_message
    from chat.services.leave.reason_correction_parser import looks_like_reason_correction
    from chat.services.leave_confirm import parse_edit_slot, wants_leave_submit_command

    if looks_like_reason_correction(message):
        return False
    if looks_like_date_only_message(message):
        return False
    if parse_edit_slot(message):
        return False
    if wants_leave_submit_command(message) or wants_cancel_leave_command(message):
        return False
    if re.search(r"^(yes|হ্যাঁ|haan|yep|ok|ঠিক)$", (message or "").strip(), re.I):
        return False
    return True


def build_parallel_leave_block_message(*, lang: str = "bn") -> str:
    return _PARALLEL_LEAVE_BLOCK_EN if lang == "en" else _PARALLEL_LEAVE_BLOCK_BN


def wants_cancel_leave_command(message: str) -> bool:
    raw = (message or "").strip()
    return bool(_CANCEL_LEAVE_RE.search(raw))


def format_submitted_leave_cancel_blocked_message(
    workflow_state: dict[str, Any] | None,
    *,
    lang: str | None = None,
) -> str:
    """Explain that a submitted leave cannot be cancelled in chat."""
    from chat.services.leave_fsm import read_leave_last_submission
    from chat.services.leave_confirm import build_leave_review_summary

    wf = workflow_state or {}
    last = read_leave_last_submission(wf)
    ref = str(last.get("submission_id") or "").strip()
    draft = dict(last.get("draft") or {})
    summary = build_leave_review_summary(draft) if draft else ""
    days = compute_requested_leave_days(draft) if draft else 0

    if lang == "en":
        lines = [
            "**This leave request is already submitted** — it cannot be cancelled or edited in this chat.",
        ]
        if ref:
            lines.append(f"- **Reference:** `{ref}`")
        if days:
            lines.append(f"- **Days requested:** {days:g}")
        if summary:
            lines.extend(["", summary])
        lines.extend(
            [
                "",
                "Final approval is handled in your company's HR system. "
                "For withdrawal or changes after submit, please contact **HR** directly.",
                "",
                "To apply for a **new** leave, start fresh — e.g. *ami kalke sick leave nite chai*.",
            ]
        )
        return "\n".join(lines)

    lines = [
        "**আপনার ছুটির আবেদন ইতিমধ্যে জমা হয়েছে** — এই চ্যাট থেকে আর **বাতিল বা সম্পাদনা** করা যাবে না।",
    ]
    if ref:
        lines.append(f"- **রেফারেন্স:** `{ref}`")
    if days:
        lines.append(f"- **আবেদনকৃত দিন:** {days:g} দিন")
    if summary:
        lines.extend(["", summary])
    lines.extend(
        [
            "",
            "চূড়ান্ত অনুমোদন আপনার কোম্পানির **HR সিস্টেমে** হবে। "
            "জমা দেওয়ার পর বাতিল বা পরিবর্তন চাইলে সরাসরি **HR-এর সাথে যোগাযোগ** করুন।",
            "",
            "নতুন ছুটির জন্য আবার শুরু করতে পারেন — যেমন: **ami kalke sick leave nite chai**।",
        ]
    )
    return "\n".join(lines)


def format_submitted_leave_edit_blocked_message(
    workflow_state: dict[str, Any] | None,
    *,
    lang: str | None = None,
) -> str:
    """Explain that a submitted leave cannot be edited in chat."""
    from chat.services.leave_fsm import read_leave_last_submission

    wf = workflow_state or {}
    last = read_leave_last_submission(wf)
    ref = str(last.get("submission_id") or "").strip()
    draft = dict(last.get("draft") or {})
    start = str(draft.get("start_date") or "").strip()
    end = str(draft.get("end_date") or start).strip()
    date_line = f"**{start}**" if start == end else f"**{start}** → **{end}**"

    if lang == "en":
        lines = [
            "**This leave is already submitted** — dates, type, or reason cannot be changed in chat.",
        ]
        if ref:
            lines.append(f"- **Reference:** `{ref}`")
        if start:
            lines.append(f"- **Dates:** {date_line}")
        lines.extend(
            [
                "",
                "Please contact **HR** for post-submit changes.",
                "For a **new** request, start a fresh leave message.",
            ]
        )
        return "\n".join(lines)

    lines = [
        "**ছুটি ইতিমধ্যে জমা** — তারিখ, ধরন বা কারণ এই চ্যাট থেকে **বদলানো যাবে না**।",
    ]
    if ref:
        lines.append(f"- **রেফারেন্স:** `{ref}`")
    if start:
        lines.append(f"- **তারিখ:** {date_line}")
    lines.extend(
        [
            "",
            "পরিবর্তন চাইলে **HR-এর সাথে যোগাযোগ** করুন।",
            "নতুন আবেদনের জন্য আবার leave message দিন।",
        ]
    )
    return "\n".join(lines)


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
    from chat.services.leave_copy import lang_from_draft
    from chat.services.expense_copy import normalize_reply_lang

    def _submitted_card(
        draft: dict[str, Any], *, ref: str, days: float
    ) -> str:
        lang = normalize_reply_lang(lang_from_draft(draft))
        summary = build_leave_review_summary(draft)
        if lang == "en":
            header = f"**Submitted leave summary** ({days:g} day(s))"
            footer = (
                "\n\n---\n"
                f"**Status:** Submitted · ref `{ref}`\n"
                "Final approval happens in your company's HR system."
            )
        else:
            header = f"**জমা দেওয়া ছুটির সারাংশ** ({days:g} দিন)"
            footer = (
                "\n\n---\n"
                f"**স্ট্যাটাস:** জমা হয়েছে · রেফারেন্স `{ref}`\n"
                "চূড়ান্ত অনুমোদন HR সিস্টেমে হবে — এই চ্যাট শুধু আবেদন জমা নেয়।"
            )
        return f"{header}\n\n{summary}{footer}"

    last = read_leave_last_submission(workflow_state)
    if last.get("submission_id") and not _active_leave_draft(workflow_state):
        draft = dict(last.get("draft") or {})
        ref = str(last.get("submission_id") or "")
        days = compute_requested_leave_days(draft)
        return _submitted_card(draft, ref=ref, days=days)

    st = read_leave_state(workflow_state)
    if st.get("locked") and st.get("submission_id"):
        draft = dict(st.get("draft") or {})
        ref = str(st.get("submission_id") or "")
        days = compute_requested_leave_days(draft)
        return _submitted_card(draft, ref=ref, days=days)

    draft = _active_leave_draft(workflow_state)
    if not draft:
        return (
            "এই session-এ আপনার leave summary নেই — হয়তো cancel করা হয়েছে বা এখনো শুরু হয়নি।"
        )
    summary = build_leave_review_summary(draft)
    days = compute_requested_leave_days(draft)
    lang = normalize_reply_lang(lang_from_draft(draft))
    if lang == "en":
        return f"**Leave summary** ({days:g} day(s) — not submitted yet):\n\n{summary}"
    return f"**ছুটির সারাংশ** ({days:g} দিন — এখনো জমা হয়নি):\n\n{summary}"


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


def block_duplicate_submitted_leave_dates(
    workflow_state: dict[str, Any],
    entities: dict[str, Any],
) -> str | None:
    """Block submitting the same calendar date twice in one session."""
    from chat.services.leave_fsm import read_leave_last_submission

    last = read_leave_last_submission(workflow_state)
    if not last.get("submission_id"):
        return None
    submitted = dict(last.get("draft") or {})
    sub_start = str(submitted.get("start_date") or "")
    sub_end = str(submitted.get("end_date") or sub_start)
    if not sub_start:
        return None
    start = str(entities.get("start_date") or "")
    end = str(entities.get("end_date") or start)
    if not start:
        return None
    if _iso_ranges_overlap(start, end, sub_start, sub_end):
        return build_duplicate_session_leave_message(
            target_iso=start,
            submission_id=str(last.get("submission_id") or ""),
        )
    return None


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
