"""
Apply leave draft corrections while leave is parked (suspended) during expense wizard.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from chat.services.bn_normalize import infer_bn_calendar_date, normalize_message_for_parsing
from chat.services.leave.review_turn_parser import try_apply_review_compound_update
from chat.services.leave_days import compute_requested_leave_days
from chat.services.leave_fsm import deep_merge_draft
from chat.services.leave_slot_extraction import extract_leave_slots
from chat.services.workflow_suspend import KEY_SUSPENDED_LEAVE
from chat.services.wizard_turn_gate import looks_like_leave_review_update

_REASON_SWAP_RE = re.compile(
    r"(?:কারণ|reason).{0,20}(?:না|na|not).{0,40}(?:হবে|hobe|habe|হয়)",
    re.I | re.UNICODE,
)
_DATE_SWAP_RE = re.compile(
    r"(?:ছুটি|chuti|chhuti|leave|তারিখ|date).{0,30}(?:না|na|not).{0,40}(?:কর|করে|hobe|habe|দাও|dao)",
    re.I | re.UNICODE,
)
_END_DATE_SWAP_RE = re.compile(
    r"(?:শেষ\s*তারিখ|end\s*date).{0,40}(?:না|na|not).{0,40}(?:হবে|hobe|habe|হবে)",
    re.I | re.UNICODE,
)
_DURATION_SWAP_RE = re.compile(
    r"(?:ছুটি|chuti|chhuti|leave|একদিন|one\s*day).{0,30}(?:না|na|not).{0,40}(?:দিন|din|day)",
    re.I | re.UNICODE,
)
_BN_DURATION_RE = re.compile(
    r"(\d+|দুই|তিন|চার|পাঁচ|দুইদিনের|তিনদিনের)\s*(?:দিন|din|days?)",
    re.I | re.UNICODE,
)
_BN_WORD_DAYS = {
    "দুই": 2,
    "তিন": 3,
    "চার": 4,
    "পাঁচ": 5,
    "দুইদিনের": 2,
    "তিনদিনের": 3,
}


def looks_like_suspended_leave_correction(message: str) -> bool:
    raw = normalize_message_for_parsing(message)
    if not raw.strip():
        return False
    from chat.services.expense.expense_confirm import looks_like_expense_correction

    if looks_like_expense_correction(raw):
        return False
    if re.search(
        r"\b(policy|policies|পলিসি|নীতি|নিয়ম|ডকুমেন্ট|document|নথি)\b",
        raw,
        re.I | re.UNICODE,
    ):
        return False
    if re.search(
        r"\b(submit|জমা|joma|summary|summery|সারাংশ|pending|dekhao|দেখাও)\b",
        raw,
        re.I | re.UNICODE,
    ):
        return False
    if re.search(r"cancel\s*leave|ছুটি\s*cancel|বাতিল", raw, re.I):
        return False
    if re.search(
        r"\b(expense|খরচ|entry|line)\b",
        raw,
        re.I | re.UNICODE,
    ) and re.search(
        r"\b(প্রথম|দ্বিতীয়|তৃতীয়|শেষ|শেষটা|first|second|third|last)\b",
        raw,
        re.I | re.UNICODE,
    ):
        return False
    if re.search(
        r"\b(expense|খরচ)\b",
        raw,
        re.I | re.UNICODE,
    ) and re.search(r"\d", raw):
        return False
    if looks_like_leave_review_update(raw):
        return True
    if (
        _END_DATE_SWAP_RE.search(raw)
        or _REASON_SWAP_RE.search(raw)
        or _DATE_SWAP_RE.search(raw)
        or _DURATION_SWAP_RE.search(raw)
    ):
        return True
    if re.search(r"(ছুটি|chuti|chhuti|leave|কারণ|reason)", raw, re.I) and re.search(
        r"(না|na|not|বদল|change|করে\s*দাও|hobe|habe)", raw, re.I
    ):
        return True
    return False


def _parse_bn_duration_days(message: str) -> float | None:
    raw = normalize_message_for_parsing(message)
    m = _BN_DURATION_RE.search(raw)
    if not m:
        if re.search(r"দুই\s*দিন|দুইদিন|two\s*days?", raw, re.I):
            return 2.0
        if re.search(r"এক\s*দিন|one\s*day|1\s*দিন", raw, re.I):
            return 1.0
        return None
    token = m.group(1).strip()
    if token.isdigit():
        return float(int(token))
    return float(_BN_WORD_DAYS.get(token, 0) or 0) or None


def _apply_end_date_correction(draft: dict[str, Any], message: str, *, today: date) -> bool:
    raw = normalize_message_for_parsing(message)
    if not _END_DATE_SWAP_RE.search(raw):
        return False
    nums = re.findall(
        r"\b(\d{1,2})\s*(?:জুন|june|jun)\b", raw, flags=re.I | re.UNICODE
    )
    new_iso: str | None = None
    if nums:
        try:
            new_iso = infer_bn_calendar_date(f"{nums[-1]} june", today=today)
        except Exception:
            new_iso = None
    if not new_iso:
        new_iso = infer_bn_calendar_date(raw, today=today)
    if not new_iso:
        return False
    draft["end_date"] = new_iso
    start = str(draft.get("start_date") or "")
    if start:
        try:
            s = date.fromisoformat(start)
            e = date.fromisoformat(new_iso)
            if e >= s:
                draft["days"] = float((e - s).days + 1)
        except ValueError:
            pass
    return True


def _apply_date_correction(draft: dict[str, Any], message: str, *, today: date) -> bool:
    if _apply_end_date_correction(draft, message, today=today):
        return True
    raw = normalize_message_for_parsing(message)
    nums = re.findall(
        r"\b(\d{1,2})\s*(?:জুন|june|jun)\b", raw, flags=re.I | re.UNICODE
    )
    if len(nums) >= 2 and re.search(r"না|na|not", raw, re.I):
        try:
            dnum = int(nums[-1])
            new_iso = infer_bn_calendar_date(f"{dnum} june", today=today)
            if new_iso:
                draft["start_date"] = new_iso
                days = draft.get("days") or compute_requested_leave_days(draft)
                if days and float(days) > 1:
                    s = date.fromisoformat(new_iso)
                    e = s + timedelta(days=int(float(days)) - 1)
                    draft["end_date"] = e.isoformat()
                else:
                    draft["end_date"] = new_iso
                return True
        except ValueError:
            pass
    new_iso = infer_bn_calendar_date(raw, today=today)
    if not new_iso:
        slots = extract_leave_slots(raw, today=today, skip_leave_phrase_gate=True)
        if slots.start_date.confidence == "high" and slots.start_date.value:
            new_iso = str(slots.start_date.value)
    if not new_iso:
        nums = re.findall(r"\b(\d{1,2})\s*(?:জুন|june|jun)\b", raw.lower())
        if len(nums) >= 2 and re.search(r"না|na|not", raw, re.I):
            try:
                dnum = int(nums[-1])
                new_iso = infer_bn_calendar_date(f"{dnum} june", today=today)
            except ValueError:
                pass
        elif len(nums) >= 1 and re.search(r"করে\s*দাও|করে\s*দাও|hobe|habe|কর", raw, re.I):
            try:
                dnum = int(nums[-1])
                new_iso = infer_bn_calendar_date(f"{dnum} june", today=today)
            except ValueError:
                pass
        elif len(nums) == 1:
            try:
                new_iso = infer_bn_calendar_date(f"{nums[0]} june", today=today)
            except ValueError:
                pass
    if not new_iso:
        return False
    draft["start_date"] = new_iso
    days = draft.get("days") or compute_requested_leave_days(draft)
    if days and float(days) > 1:
        s = date.fromisoformat(new_iso)
        e = s + timedelta(days=int(float(days)) - 1)
        draft["end_date"] = e.isoformat()
    else:
        draft["end_date"] = new_iso
    return True


def _apply_reason_correction(draft: dict[str, Any], message: str) -> bool:
    raw = normalize_message_for_parsing(message)
    if re.search(r"ব্যক্তিগত|personal", raw, re.I) and re.search(
        r"না|na|not", raw, re.I
    ) and re.search(r"পারিবারিক|family", raw, re.I):
        draft["reason"] = "পারিবারিক কাজ"
        return True
    if re.search(r"অসুস্থ|osusto|oshustho|sick", raw, re.I) and re.search(
        r"না|na|not", raw, re.I
    ) and re.search(r"ব্যক্তিগত|personal", raw, re.I):
        draft["reason"] = "ব্যক্তিগত কাজ"
        return True
    if re.search(r"পারিবারিক|family", raw, re.I):
        draft["reason"] = "পারিবারিক কাজ"
        return True
    if re.search(r"ব্যক্তিগত|personal", raw, re.I):
        draft["reason"] = "ব্যক্তিগত কাজ"
        return True
    if re.search(r"অসুস্থ|osusto|oshustho|sick", raw, re.I):
        draft["reason"] = "অসুস্থতা"
        draft["leave_type"] = draft.get("leave_type") or "sick"
        return True
    from chat.services.leave.reason_value import extract_reason_value

    reason = extract_reason_value(raw, edit_context=True)
    if reason:
        draft["reason"] = reason
        return True
    return False


def _patch_leave_draft(
    draft: dict[str, Any],
    message: str,
    *,
    today: date | None = None,
) -> tuple[dict[str, Any], bool]:
    today_d = today or date.today()
    changed = False
    raw = normalize_message_for_parsing(message)

    if _DURATION_SWAP_RE.search(raw) or re.search(r"দুই\s*দিন|two\s*days?", raw, re.I):
        days = _parse_bn_duration_days(raw)
        if days and days > 0:
            draft["days"] = days
            if draft.get("start_date"):
                s = date.fromisoformat(str(draft["start_date"]))
                e = s + timedelta(days=int(days) - 1)
                draft["end_date"] = e.isoformat()
            changed = True

    if _DATE_SWAP_RE.search(raw) or re.search(r"জুন|june|তারিখ|date", raw, re.I):
        if _apply_date_correction(draft, raw, today=today_d):
            changed = True

    if _REASON_SWAP_RE.search(raw) or re.search(r"কারণ|reason", raw, re.I):
        if _apply_reason_correction(draft, raw):
            changed = True

    if not changed:
        changed = try_apply_review_compound_update(draft, raw, use_llm=False)

    if not changed:
        slots = extract_leave_slots(raw, today=today_d, skip_leave_phrase_gate=True).as_dict()
        if slots:
            draft = deep_merge_draft(draft, slots)
            changed = True

    return draft, changed


def _format_correction_reply(draft: dict[str, Any]) -> str:
    parts = []
    if draft.get("start_date"):
        parts.append(f"তারিখ: **{draft['start_date']}**")
    if draft.get("reason"):
        parts.append(f"কারণ: **{draft['reason']}**")
    days = compute_requested_leave_days(draft)
    if days:
        parts.append(f"মোট: **{days:g}** দিন")
    body = "ছুটির তথ্য আপডেট হয়েছে।"
    if parts:
        body += " " + " · ".join(parts)
    return body


def apply_suspended_leave_correction(
    workflow_state: dict[str, Any],
    message: str,
    *,
    today: date | None = None,
) -> tuple[dict[str, Any], str, bool]:
    """Patch suspended_leave.draft. Returns (wf, user_message, changed)."""
    wf = dict(workflow_state or {})
    sl = dict(wf.get(KEY_SUSPENDED_LEAVE) or {})
    draft = dict(sl.get("draft") or {})
    if not draft and not sl:
        return wf, "", False

    draft, changed = _patch_leave_draft(draft, message, today=today)
    if not changed:
        return wf, "", False

    sl["draft"] = draft
    wf[KEY_SUSPENDED_LEAVE] = sl
    return wf, _format_correction_reply(draft), True


def apply_active_leave_correction(
    workflow_state: dict[str, Any],
    message: str,
    *,
    today: date | None = None,
) -> tuple[dict[str, Any], str, bool]:
    """Patch active leave draft (e.g. after policy interrupt resume)."""
    from chat.services.leave_fsm import (
        ACTIVE_FLOW_LEAVE,
        STATUS_ACTIVE,
        apply_leave_state,
        read_leave_state,
    )

    wf = dict(workflow_state or {})
    st = read_leave_state(wf)
    if st.get("active_flow") != ACTIVE_FLOW_LEAVE:
        return wf, "", False
    draft = dict(st.get("draft") or {})
    draft, changed = _patch_leave_draft(draft, message, today=today)
    if not changed:
        return wf, "", False
    wf = apply_leave_state(
        wf,
        draft=draft,
        step=st.get("step"),
        status=str(st.get("status") or STATUS_ACTIVE),
        review_pending=bool(st.get("review_pending")),
    )
    return wf, _format_correction_reply(draft), True


def apply_leave_draft_correction(
    workflow_state: dict[str, Any],
    message: str,
    *,
    today: date | None = None,
) -> tuple[dict[str, Any], str, bool]:
    """Unified correction for suspended or active leave drafts."""
    from chat.services.workflow_suspend import has_suspended_leave

    if has_suspended_leave(workflow_state):
        return apply_suspended_leave_correction(workflow_state, message, today=today)
    return apply_active_leave_correction(workflow_state, message, today=today)
