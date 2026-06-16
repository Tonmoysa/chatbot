"""
Slot-agnostic leave date correction — rules first, LLM when the utterance is
clearly a date answer but regex missed (voice / free-form Bengali).
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from chat.services.bn_normalize import (
    infer_bn_calendar_date,
    infer_bn_calendar_date_range,
    normalize_message_for_parsing,
)

logger = logging.getLogger("hr_chatbot")

_DATE_MARKERS_RE = re.compile(
    r"থেকে|পর্যন্ত|theke|porjonto|"
    r"জানু|ফেব|মার্চ|এপ্রিল|মে|জুন|জুলাই|আগস্ট|সেপ্টেম্বর|অক্টোবর|নভেম্বর|ডিসেম্বর|"
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}\s*(?:জুন|june)",
    re.I | re.UNICODE,
)

_LEAVE_TYPE_RE = re.compile(
    r"\b(sick|annual|casual|unpaid|medical|maternity|paternity)\b|"
    r"অসুস্থ|বার্ষিক|ক্যাজুয়াল|বেতন\s*ছাড়া",
    re.I | re.UNICODE,
)

_DATE_LLM_SYSTEM = """You extract calendar leave dates from a short user message.

Return STRICT JSON only:
{
  "start_date": "YYYY-MM-DD" or null,
  "end_date": "YYYY-MM-DD" or null,
  "confidence": 0.0 to 1.0
}

RULES
- Bengali: "২৫ জুন থেকে ২৬ জুন" → start 2026-06-25, end 2026-06-26 (use sensible year >= today)
- Single day: end_date equals start_date
- If the message is NOT primarily about dates (reason, leave type, expense), return null dates and confidence 0
- Never invent dates not implied by the user
"""


def looks_like_date_only_message(message: str, *, today: date | None = None) -> bool:
    """True when the utterance is mainly a date or date-range answer."""
    today_d = today or date.today()
    raw = normalize_message_for_parsing((message or "").strip())
    if not raw:
        return False
    if infer_bn_calendar_date_range(raw, today=today_d):
        return True
    if infer_bn_calendar_date(raw, today=today_d) and not _LEAVE_TYPE_RE.search(raw):
        if len(re.findall(r"\S+", raw)) <= 10:
            return True
    if _DATE_MARKERS_RE.search(raw) and not _LEAVE_TYPE_RE.search(raw):
        words = re.findall(r"\S+", raw)
        if len(words) <= 8:
            return True
    return False


def _apply_iso_range(draft: dict[str, Any], start_iso: str, end_iso: str) -> bool:
    from chat.services.leave_draft_utils import sync_days_from_calendar_range

    before = (draft.get("start_date"), draft.get("end_date"))
    draft["start_date"] = start_iso
    draft["end_date"] = end_iso
    sync_days_from_calendar_range(draft)
    draft.pop("_needs_date_clarify", None)
    draft.pop("_vague_next_week", None)
    after = (draft.get("start_date"), draft.get("end_date"))
    return before != after


def _llm_extract_dates(
    message: str,
    *,
    today: date,
    trace_id: str = "",
) -> tuple[str | None, str | None]:
    from chat.services.llm_client import LLMClient

    client = LLMClient()
    if not client.is_configured():
        return None, None
    out = client.chat_json(
        system_prompt=_DATE_LLM_SYSTEM,
        user_prompt=(
            f"Today: {today.isoformat()}\n"
            f"User message:\n{(message or '').strip()}\n\n"
            "Return JSON only."
        ),
        trace_id=trace_id or "leave-date-llm",
    )
    if not isinstance(out, dict):
        return None, None
    try:
        confidence = float(out.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.55:
        return None, None
    start = str(out.get("start_date") or "").strip()
    end = str(out.get("end_date") or start or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", start):
        return None, None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
        end = start
    return start, end


def looks_like_leave_date_correction(message: str, *, today: date | None = None) -> bool:
    """True when the user is correcting leave dates (not stating a fresh range)."""
    today_d = today or date.today()
    raw = normalize_message_for_parsing((message or "").strip())
    if not raw:
        return False
    low = raw.lower()
    if re.search(
        r"(?:sesh|শেষ|ses|end|শেষের)\s*(?:date|তারিখ|din|day)?|"
        r"leave\s+er\s+sesh",
        low,
        re.I | re.UNICODE,
    ) and re.search(r"\b(?:na|nah|not|না)\b", low, re.I | re.UNICODE):
        return True
    if not re.search(r"\b(?:na|nah|not|না)\b", low, re.I | re.UNICODE):
        return False
    if not re.search(
        r"(?:ছুটি|chuti|chhuti|leave|তারিখ|date|tarikh|tarik|sesh|শেষ)",
        low,
        re.I | re.UNICODE,
    ):
        return False
    if infer_bn_calendar_date_range(raw, today=today_d):
        return True
    if infer_bn_calendar_date(raw, today=today_d):
        return True
    return False


def try_apply_leave_end_date_only(
    draft: dict[str, Any],
    message: str,
    *,
    today: date | None = None,
) -> bool:
    """``sesh date 7 august na 8 august hobe`` — update end_date only, keep start."""
    today_d = today or date.today()
    raw = normalize_message_for_parsing((message or "").strip())
    if not raw:
        return False
    low = raw.lower()
    if not re.search(
        r"(?:sesh|শেষ|ses|end|শেষের)\s*(?:date|তারিখ|din|day)?|"
        r"leave\s+er\s+sesh",
        low,
        re.I | re.UNICODE,
    ):
        return False
    if not re.search(r"\b(?:na|nah|not|না)\b", low, re.I | re.UNICODE):
        return False
    start_iso = str(draft.get("start_date") or "").strip()
    if not start_iso:
        return False
    parts = re.split(r"\b(?:na|nah|not|না)\b", raw, maxsplit=1, flags=re.I | re.UNICODE)
    tail = parts[-1] if len(parts) > 1 else raw
    end_iso = infer_bn_calendar_date(tail, today=today_d)
    if not end_iso:
        rng = infer_bn_calendar_date_range(tail, today=today_d)
        if rng:
            end_iso = rng[1]
    if not end_iso:
        return False
    return _apply_iso_range(draft, start_iso, end_iso)


def try_apply_leave_date_correction(
    draft: dict[str, Any],
    message: str,
    *,
    today: date | None = None,
    trace_id: str = "",
    use_llm: bool = True,
) -> bool:
    """
    Apply calendar dates from message regardless of pending wizard slot.
    Returns True when draft start/end changed.
    """
    today_d = today or date.today()
    raw = normalize_message_for_parsing((message or "").strip())
    if not raw:
        return False

    if try_apply_leave_end_date_only(draft, message, today=today_d):
        return True

    from chat.services.expense.expense_confirm import looks_like_expense_correction

    if looks_like_expense_correction(raw):
        return False

    rng = infer_bn_calendar_date_range(raw, today=today_d)
    if rng:
        return _apply_iso_range(draft, rng[0], rng[1])

    if looks_like_date_only_message(raw, today=today_d):
        single = infer_bn_calendar_date(raw, today=today_d)
        if single:
            return _apply_iso_range(draft, single, single)

    if use_llm and looks_like_date_only_message(raw, today=today_d):
        start, end = _llm_extract_dates(raw, today=today_d, trace_id=trace_id)
        if start and end:
            logger.info("leave_date_llm_applied trace_id=%s start=%s end=%s", trace_id, start, end)
            return _apply_iso_range(draft, start, end)

    return False
