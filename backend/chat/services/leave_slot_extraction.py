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

_LEAVE_TYPE_HINT_RE = re.compile(
    "|".join(f"(?:{pat})" for pat, _code in _TYPE_PATTERNS),
    re.I,
)


def message_mentions_leave_type(message: str) -> bool:
    """True when the user explicitly named a leave type in this message."""
    return bool(_LEAVE_TYPE_HINT_RE.search(message or ""))


def explicit_leave_type_from_message(message: str) -> str | None:
    """Highest-priority leave category in the message (sick/casual/annual — not paid/unpaid)."""
    low = (message or "").lower()
    for pattern, code in _TYPE_PATTERNS:
        if re.search(pattern, low, re.I):
            return code
    return None


def apply_payment_category_from_message(message: str) -> str | None:
    """Return ``paid`` or ``lwop`` when the user is setting payment only."""
    low = (message or "").lower()
    for pattern, pay in _PAYMENT_PATTERNS:
        if re.search(pattern, low, re.I):
            return pay
    return None


_REASON_FOR_RE = re.compile(
    r"\bfor\s+(?:my\s+|our\s+|the\s+|a\s+)?(.+?)"
    r"(?=\s+that\s+|\s+which\s+|\s+who\s+|,\s*|\.\s|$|\s+(?:will\s+be\s+)?(?:unpaid|paid|lwop)\b)",
    re.I | re.UNICODE,
)
_REASON_CAUSE_RE = re.compile(
    r"\b(?:because\s+of|due\s+to|regarding)\s+(.+?)(?=\s+that\s+|,|\.|$|\s+(?:unpaid|paid)\b)",
    re.I | re.UNICODE,
)
_REASON_BN_JONNO_RE = re.compile(
    r"((?:famil+y|পরিবার|wedding|বিয়ে|sick|অসুস্থ)(?:\s+\w+){0,3})\s+er\s+jonno",
    re.I | re.UNICODE,
)
_REASON_BN_TAI_RE = re.compile(
    r"^(.+?)\s+tai\s+"
    r"(?:amar|ami|my|kal|kalke|kalker|agamikal|agami\s*kal|tomorrow|ajke|aj\s*ke|"
    r"leave|chuti|chhuti|chhutti|ছুটি)",
    re.I | re.UNICODE,
)
# Inline causal: "...matha betha..tai full day paid leave" (not only at message start).
_REASON_BN_TAI_INLINE_RE = re.compile(
    r"((?:onek\s+|khub\s+|bishal\s+)?(?:matha|pet|mathar?)\s*betha)\s*\.{0,3}\s*tai\b",
    re.I | re.UNICODE,
)
_REASON_BN_BOLE_RE = re.compile(
    r"^(.+?)\s+bole\s+"
    r"(?:amar|ami|my|kal|kalke|kalker|agamikal|agami\s*kal|tomorrow|ajke|aj\s*ke|"
    r"leave|chuti|chhuti|chhutti|ছুটি)",
    re.I | re.UNICODE,
)
_HEALTH_REASON_RE = re.compile(
    r"\b("
    r"matha\s*betha|mathar?\s*betha|pet\s*betha|stomach\s*(?:pain|ache|hurt)|"
    r"headache|head\s*(?:pain|ache)|fever|"
    r"পেট\s*ব্যথা|মাথা\s*ব্যথা|জ্বর|ব্যথা|অসুস্থ|"
    r"doctor|ডাক্তার|medical\s+appointment"
    r")\b",
    re.I | re.UNICODE,
)
_REASON_KEYWORD_RE = re.compile(
    r"\b("
    r"family\s+(?:program|function|event|matter|emergency|issue|work|gathering|wedding)|"
    r"family\s+wedding|"
    r"wedding|funeral|marriage|travel|personal\s+(?:work|matter)|"
    r"medical\s+appointment|relative(?:'s)?\s+\w+|"
    r"পরিবার(?:ের)?\s*(?:কাজ|অনুষ্ঠান|প্রোগ্রাম)|"
    r"বিয়ে|অনুষ্ঠান|সংসার"
    r")\b",
    re.I | re.UNICODE,
)
_SIDE_QUESTION_IN_REASON_RE = re.compile(
    r"(?:^\s*(?:can|could|may|is|are|do|does|did|what|why|how)\b|"
    r"\b(can\s+i|could\s+i|may\s+i)\b|\?\s*$)",
    re.I,
)


def _trim_reason_fragment(text: str) -> str:
    s = (text or "").strip(" ,.-")
    s = re.sub(r"\s+that\s+.*$", "", s, flags=re.I)
    s = re.sub(r"\s+(?:will\s+be|is|was)\s+.*$", "", s, flags=re.I)
    return s.strip()


def _normalize_reason_token(reason: str) -> str:
    s = (reason or "").strip()
    if re.fullmatch(r"famil+y", s, re.I):
        return "family"
    return s


def extract_reason_from_message(message: str) -> str | None:
    """
    Pull a leave reason from compound or short wizard answers.

    Examples: ``for my family program``, ``family wedding``, ``fever``.
  """
    raw = (message or "").strip()
    if len(raw) < 4:
        return None
    low = raw.lower()
    if _SIDE_QUESTION_IN_REASON_RE.search(raw):
        return None
    if re.match(r"^(paid|unpaid|lwop|full|half)\b", low) and len(raw.split()) <= 3:
        return None

    m = _REASON_FOR_RE.search(raw)
    if m:
        reason = _trim_reason_fragment(m.group(1))
        if len(reason) >= 4:
            return reason[:2000]

    m = _REASON_CAUSE_RE.search(raw)
    if m:
        reason = _trim_reason_fragment(m.group(1))
        if len(reason) >= 4:
            return reason[:2000]

    m = _REASON_BN_JONNO_RE.search(raw)
    if m:
        reason = _normalize_reason_token(_trim_reason_fragment(m.group(1)))
        if len(reason) >= 3:
            return reason[:2000]

    m = _REASON_BN_TAI_INLINE_RE.search(raw)
    if m:
        reason = _trim_reason_fragment(m.group(1))
        if len(reason) >= 3:
            return reason[:2000]

    m = _REASON_BN_TAI_RE.search(raw)
    if m:
        reason = _trim_reason_fragment(m.group(1))
        if len(reason) >= 3:
            return reason[:2000]

    m = _REASON_BN_BOLE_RE.search(raw)
    if m:
        reason = _trim_reason_fragment(m.group(1))
        if len(reason) >= 3:
            return reason[:2000]

    if _LEAVE_INTENT_RE.search(raw):
        hm = _HEALTH_REASON_RE.search(raw)
        if hm:
            return hm.group(0).strip()[:2000]

    m = _REASON_KEYWORD_RE.search(raw)
    if m:
        return m.group(0).strip()[:2000]

    if _LEAVE_INTENT_RE.search(raw) and re.search(r"\bfamil+y\b", low):
        m_fam = re.search(r"\bfamil+y\b", raw, re.I)
        if m_fam:
            return _normalize_reason_token(m_fam.group(0))

    # Short wizard reply (e.g. ``family program``, ``fever``) — not leave boilerplate.
    if len(raw.split()) <= 8 and not _LEAVE_INTENT_RE.search(raw):
        if not re.search(
            r"\b(tomorrow|today|kal|agamikal|unpaid|paid|lwop|full|half)\b",
            low,
        ):
            return raw[:2000]
    return None


def is_payment_only_message(message: str) -> bool:
    """
    True for short paid/unpaid corrections (e.g. ``unpaid hobe``) that must not
    change Select Leave / sick / casual / annual.

    Long or multi-slot sentences (e.g. ``tomorrow … unpaid leave for family``)
    are never payment-only — those must run full slot extraction including dates.
    """
    low = (message or "").lower().strip()
    if not apply_payment_category_from_message(message):
        return False
    words = re.findall(r"\S+", low)
    if len(words) > 6:
        return False
    if re.search(
        r"\b(tomorrow|tomarrow|tommorow|tommorrow|tomorow|tmrw|tmw|"
        r"today|ajke|aj\s*ke|kalke|kalker|\bkal\b|agamikal|agami\s*kal|"
        r"next\s+week|আগামীকাল|আজকে|আজ)\b",
        low,
    ):
        return False
    if _LEAVE_INTENT_RE.search(low) and len(words) > 3:
        return False
    if explicit_leave_type_from_message(message):
        return False
    if re.search(
        r"\b(sick|casual|annual|medical|maternity|paternity|emergency|compensatory)\b|"
        r"অসুস্থ|জ্বর|ক্যাজুয়াল|বার্ষিক",
        low,
    ):
        return False
    if re.search(r"\b(full|half)\b|full\s*day|half\s*day|হাফ|পুরো", low):
        return False
    if re.search(r"\d{4}-\d{2}-\d{2}", message or ""):
        return False
    return True


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

    reason = extract_reason_from_message(raw)
    if reason and out.reason.confidence != "high":
        _set(out.reason, reason, confidence="high", source="rules_reason")

    return out
