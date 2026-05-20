"""
Deterministic leave slot extraction with confidence scores (Bangla / Banglish / English).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from chat.services.entity_extractor import _infer_leave_calendar_start

Confidence = Literal["high", "low", "none"]

_MIN_HIGH = "high"


@dataclass
class SlotValue:
    value: Any = None
    confidence: Confidence = "none"
    source: str = ""


@dataclass
class LeaveSlotExtraction:
    leave_type: SlotValue = field(default_factory=SlotValue)
    start_date: SlotValue = field(default_factory=SlotValue)
    end_date: SlotValue = field(default_factory=SlotValue)
    days: SlotValue = field(default_factory=SlotValue)
    leave_payment_category: SlotValue = field(default_factory=SlotValue)
    day_scope: SlotValue = field(default_factory=SlotValue)
    reason: SlotValue = field(default_factory=SlotValue)
    vague_date: bool = False
    clarification_needed: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in (
            "leave_type",
            "start_date",
            "end_date",
            "days",
            "leave_payment_category",
            "day_scope",
            "reason",
        ):
            sv: SlotValue = getattr(self, name)
            if sv.confidence == _MIN_HIGH and sv.value is not None:
                out[name] = sv.value
        return out


_LEAVE_INTENT_RE = re.compile(
    r"(leave|time\s*off|pto|vacation|holiday|day\s*off|ছুটি|chuti|chhuti|chutti|"
    r"need\s+(?:a\s+)?\w*\s*leave|leave\s*lagbe|leave\s*nite|chuti\s*nite|"
    r"ছুটি\s*লাগবে|ছুটি\s*নিতে|ছুটি\s*চাই|ছুটি\s*লাগবে)",
    re.I,
)

_VAGUE_DATE_RE = re.compile(
    r"\b(maybe|perhaps|probably|somewhere|around|ashamkha|ashankha|mone\s+hoy|"
    r"hoito|hoyto|janina|জানি\s*না|মনে\s*হয়|হয়তো)\b",
    re.I,
)

_TYPE_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"\b(sick|medical|health)\s*leave\b|\bsick(?:ness)?\b|\bill(?:ness)?\b|"
        r"medical\s*leave|অসুস্থ|জ্বর|মেডিকেল",
        "sick",
    ),
    (r"\bcasual\s*leave\b|\bcasual\b|ক্যাজুয়াল|নৈমিত্তিক", "casual"),
    (r"\b(annual|vacation|pto)\s*leave\b|\bannual\b|বার্ষিক", "annual"),
    (r"\b(unpaid|lwop)\s*leave\b|\bunpaid\b|বেতন\s*ছাড়া|বিনা\s*বেতন", "unpaid"),
    (r"\bmaternity\b|মাতৃত্ব", "maternity"),
    (r"\bpaternity\b|পিতৃত্ব", "paternity"),
    (r"\bemergency\b|জরুরি", "emergency"),
    (r"\bcompensatory\b|comp\s*off|কম্পেনসেটরি", "compensatory"),
)

_PAYMENT_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(lwop|leave without pay|without pay|unpaid)\b|বেতন\s*ছাড়া|বিনা\s*বেতন", "lwop"),
    (r"\bpaid\s*leave\b|\bpaid\b|বেতনসহ|বেতন\s*সহ", "paid"),
)

_SCOPE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bhalf[- ]?day\b|\bhalf\b|হাফ|অর্ধ\s*দিন", "half"),
    (r"\bfull[- ]?day\b|\bfull\b|পুরো\s*দিন|সম্পূর্ণ\s*দিন", "full"),
)


def _today() -> date:
    return date.today()


def _set(sv: SlotValue, value: Any, *, confidence: Confidence, source: str) -> None:
    if confidence == "none":
        return
    if sv.confidence == "high" and confidence != "high":
        return
    sv.value = value
    sv.confidence = confidence
    sv.source = source


def extract_leave_slots(
    message: str,
    *,
    today: date | None = None,
    skip_leave_phrase_gate: bool = False,
) -> LeaveSlotExtraction:
    """Rule-based slot extraction; LLM entities merged separately in prefill layer."""
    today = today or _today()
    raw = (message or "").strip()
    low = raw.lower()
    out = LeaveSlotExtraction()

    if not raw:
        return out
    # LEGACY: first versions required a leave phrase in every utterance. That broke
    # wizard follow-ups ("paid", "tomorrow", reason-only) and dropped parsed dates.
    if not skip_leave_phrase_gate and not _LEAVE_INTENT_RE.search(raw):
        return out

    if _VAGUE_DATE_RE.search(low):
        out.vague_date = True
        out.clarification_needed = (
            "তারিখটা একটু স্পষ্ট করবেন? যেমন: **আগামীকাল**, **কাল**, বা নির্দিষ্ট তারিখ (২০২৬-০৫-১৫)।"
        )

    for pattern, code in _TYPE_PATTERNS:
        if re.search(pattern, low, re.I):
            _set(out.leave_type, code, confidence="high", source="rules_type")
            break

    for pattern, pay in _PAYMENT_PATTERNS:
        if re.search(pattern, low, re.I):
            _set(
                out.leave_payment_category,
                pay,
                confidence="high",
                source="rules_payment",
            )
            break

    for pattern, scope in _SCOPE_PATTERNS:
        if re.search(pattern, low, re.I):
            _set(out.day_scope, scope, confidence="high", source="rules_scope")
            break

    # Relative dates
    # Common EN typos: tomarrow, tommorow, tomorow (still means tomorrow)
    if re.search(
        r"\b(tomorrow|tomarrow|tommorow|tommorrow|tomorow|tmrw|tmw)\b", low
    ):
        ds = (today + timedelta(days=1)).isoformat()
        _set(out.start_date, ds, confidence="high", source="tomorrow_en")
        _set(out.end_date, ds, confidence="high", source="tomorrow_en")
    elif re.search(r"(আগামীকাল|agamikal|agami\s*kal|kalke|kalker|\bkal\b)", low):
        ds = (today + timedelta(days=1)).isoformat()
        _set(out.start_date, ds, confidence="high", source="tomorrow_bn")
        _set(out.end_date, ds, confidence="high", source="tomorrow_bn")
    elif re.search(r"\b(today|ajke|aj\s*ke)\b|আজকে|আজ\b", low):
        ds = today.isoformat()
        _set(out.start_date, ds, confidence="high", source="today")
        _set(out.end_date, ds, confidence="high", source="today")

    # Next week (low confidence on exact day unless user confirms)
    if re.search(r"\bnext\s+week\b|আগামী\s*সপ্তাহ|পরের\s*সপ্তাহ", low):
        start = today + timedelta(days=7)
        if out.vague_date:
            _set(out.start_date, start.isoformat(), confidence="low", source="next_week_vague")
        else:
            _set(out.start_date, start.isoformat(), confidence="high", source="next_week")
        if not out.end_date.value:
            _set(out.end_date, start.isoformat(), confidence=out.start_date.confidence, source="next_week")

    cal = _infer_leave_calendar_start(low)
    if cal:
        _set(out.start_date, cal, confidence="high", source="calendar_phrase")
        if not out.end_date.value:
            _set(out.end_date, cal, confidence="high", source="calendar_phrase")

    # ISO / DMY numeric
    m_iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", raw)
    if m_iso:
        try:
            ds = date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))).isoformat()
            _set(out.start_date, ds, confidence="high", source="iso_date")
            if not out.end_date.value:
                _set(out.end_date, ds, confidence="high", source="iso_date")
        except ValueError:
            pass

    # Duration: "3 din", "3 diner chuti"
    m_dur = re.search(r"\b(\d+)\s*(din|diner|days?|দিন)\b", low)
    if m_dur:
        try:
            n = int(m_dur.group(1))
            if n > 0:
                _set(out.days, float(n), confidence="high", source="duration")
                if out.start_date.confidence == "high" and out.start_date.value:
                    s = date.fromisoformat(str(out.start_date.value))
                    e = s + timedelta(days=n - 1)
                    _set(out.end_date, e.isoformat(), confidence="high", source="duration_range")
        except ValueError:
            pass

    # Range: from X to Y
    if re.search(r"\b(from|to|until|till)\b|থেকে|পর্যন্ত", low):
        pass  # end_date may come from LLM / separate patterns

    # Implied reason from leave type (conversational — avoids re-asking)
    if out.leave_type.confidence == "high":
        lt = str(out.leave_type.value)
        if lt in ("sick", "medical") and not out.reason.value:
            _set(
                out.reason,
                "অসুস্থতা / sick leave",
                confidence="high",
                source="implied_sick",
            )
        elif lt == "maternity":
            _set(out.reason, "Maternity leave", confidence="high", source="implied_maternity")
        elif lt == "paternity":
            _set(out.reason, "Paternity leave", confidence="high", source="implied_paternity")

    return out


def merge_llm_entities_into_extraction(
    extraction: LeaveSlotExtraction, entities: dict[str, Any]
) -> None:
    """Overlay LLM/rule extractor fields when slot extractor did not set high-confidence values."""

    def _merge_slot(name: str, key: str | None = None) -> None:
        key = key or name
        v = entities.get(key)
        if v is None or v == "":
            return
        sv: SlotValue = getattr(extraction, name)
        if sv.confidence == "high":
            return
        _set(sv, v, confidence="high", source="llm_entities")

    _merge_slot("leave_type")
    _merge_slot("start_date")
    _merge_slot("end_date")
    if entities.get("date") and extraction.start_date.confidence != "high":
        _set(
            extraction.start_date,
            str(entities["date"]).split("T")[0],
            confidence="high",
            source="llm_date",
        )
    _merge_slot("days")
    _merge_slot("reason")
    pay = entities.get("leave_payment_category")
    if pay:
        p = str(pay).lower()
        if p in {"paid", "pto", "annual", "casual"}:
            _merge_slot("leave_payment_category")
            extraction.leave_payment_category.value = "paid"
        elif p in {"lwop", "unpaid"}:
            extraction.leave_payment_category.value = "lwop"
    scope = entities.get("day_scope")
    if scope:
        s = str(scope).lower()
        if s in {"half", "half_day", "half-day"}:
            extraction.day_scope.value = "half"
            extraction.day_scope.confidence = "high"
        elif s in {"full", "full_day", "full-day"}:
            extraction.day_scope.value = "full"
            extraction.day_scope.confidence = "high"
