"""
Canonical synonym normalization for leave draft fields.

Maps informal Bangla/Banglish/English phrases to stable workflow values.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.leave_draft_utils import (
    DAY_SCOPE_FULL,
    DAY_SCOPE_HALF,
    LEAVE_PAYMENT_LWOP,
    LEAVE_PAYMENT_PAID,
)

# Sick / medical signals used for leave_type inference and document rules.
SICK_SIGNALS: tuple[str, ...] = (
    "matha betha",
    "mathar betha",
    "pet betha",
    "stomach pain",
    "stomach ache",
    "stomach hurt",
    "fever",
    "headache",
    "পেট ব্যথা",
    "মাথা ব্যথা",
    "জ্বর",
    "অসুস্থ",
    "চিকিৎসা",
    "chikitsa",
    "onek osusto",
    "osusto",
    "oshustho",
    "doctor",
    "ডাক্তার",
    "medical",
    "illness",
    "sick",
)

_DURATION_RE = re.compile(
    r"\b(\d+)\s*(din|diner|days?|দিন)\b",
    re.I | re.UNICODE,
)

_PAYMENT_LWOP_RE = re.compile(
    r"\b(lwop|unpaid|without\s+pay|no\s+pay|salary\s+cut|"
    r"বেতন\s*ছাড়া|বেতন\s*ছাড়া|বিনা\s*বেতন|বেতন\s*কাটা)\b",
    re.I,
)
_PAYMENT_PAID_RE = re.compile(
    r"\b(paid|with\s+pay|betonsokh|বেতনসহ|বেতন\s*সহ)\b",
    re.I,
)
_SCOPE_HALF_RE = re.compile(
    r"(?:\b(half[- ]?day|half)\b|হাফ\s*(?:দিন|ডে)?|অর্ধ\s*দিন)",
    re.I | re.UNICODE,
)
_SCOPE_FULL_RE = re.compile(
    r"(?:"
    r"\b(full[- ]?day|full|ful\s*day|whole\s*day)\b|"
    r"পুরো\s*দিন|সম্পূর্ণ\s*দিন|"
    r"ফুল{1,2}(?:ি)?\s*(?:ডে|দিন)"
    r")",
    re.I | re.UNICODE,
)


_LEAVE_DATE_SIGNAL_RE = re.compile(
    r"(?:"
    r"\b\d{4}-\d{1,2}-\d{1,2}\b|"
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|"
    r"(?:tarikh|tarik|date|dates|তারিখ).{0,35}(?:badl|change|hobe|lagbe|na|kor)|"
    r"(?:badl|change|hobe).{0,35}(?:tarikh|tarik|date|তারিখ)|"
    r"(?:kalke|kalker|agamikal|tomorrow|porer\s+din).{0,45}"
    r"(?:chuti|chhuti|chhuti|leave|lagbe|tarikh|date|তারিখ)|"
    r"(?:chuti|chhuti|chhuti|leave|ছুটি).{0,45}(?:kalke|kalker|agamikal|tomorrow)|"
    r"\b(?:parshu|yesterday|goto\s*kal|gata\s*kal|gato\s*kal)\b"
    r")",
    re.I | re.UNICODE,
)


def message_mentions_leave_duration(message: str) -> bool:
    """True when the user states a leave length in days (e.g. 3 diner jonno)."""
    return bool(_DURATION_RE.search((message or "").strip()))


def extract_leave_duration_days(message: str) -> int | None:
    m = _DURATION_RE.search((message or "").strip())
    if not m:
        return None
    try:
        n = int(m.group(1))
        return n if n > 0 else None
    except ValueError:
        return None


def message_explicitly_states_leave_date(message: str) -> bool:
    """True when the user is clearly changing or stating a leave date — not casual ajke talk."""
    text = (message or "").strip()
    if not text:
        return False
    try:
        from chat.services.wizard_turn_gate import is_casual_wizard_side_statement

        if is_casual_wizard_side_statement(text):
            return False
    except Exception:
        pass
    if _LEAVE_DATE_SIGNAL_RE.search(text):
        return True
    if re.search(r"\bajke\b", text, re.I) and re.search(
        r"\b(leave|chuti|chhuti|chhuti|ছুটি|tarikh|tarik|date|তারিখ|lagbe|hobe|change|badl)\b",
        text,
        re.I,
    ):
        return True
    return False


def parse_day_scope_answer(message: str) -> str | None:
    """
    Parse explicit full/half day from user text (voice STT, Bangla, Banglish, English).

    Returns DAY_SCOPE_FULL, DAY_SCOPE_HALF, or None when not clearly stated.
    """
    text = (message or "").strip()
    if not text:
        return None
    has_half = bool(_SCOPE_HALF_RE.search(text))
    has_full = bool(_SCOPE_FULL_RE.search(text))
    if has_half and not has_full:
        return DAY_SCOPE_HALF
    if has_full and not has_half:
        return DAY_SCOPE_FULL
    if has_half and has_full:
        if re.search(r"(?:half|হাফ|অর্ধ)", text, re.I | re.UNICODE):
            return DAY_SCOPE_HALF
        return DAY_SCOPE_FULL
    return None


_WIZARD_SICK_RE = re.compile(
    r"\b(sick|medical|health|osusto|oshustho|ill(?:ness)?)\b|অসুস্থ|জ্বর|মেডিকেল",
    re.I | re.UNICODE,
)
_WIZARD_ANNUAL_RE = re.compile(
    r"\b(annual|vacation|pto)\b|বার্ষিক|annual\s*leave",
    re.I | re.UNICODE,
)
_WIZARD_UNPAID_RE = re.compile(
    r"\b(lwop|unpaid|without\s+pay|leave\s+without\s+pay)\b|বেতন\s*ছাড়া|বিনা\s*বেতন",
    re.I | re.UNICODE,
)
_HALF_FIRST_RE = re.compile(
    r"(?:"
    r"\bfirst\s*half\b|morning|am\b|"
    r"প্রথম\s*অর্ধ|সকাল|ফার্স্ট\s*হাফ"
    r")",
    re.I | re.UNICODE,
)
_HALF_SECOND_RE = re.compile(
    r"(?:"
    r"\bsecond\s*half\b|afternoon|pm\b|evening|"
    r"দ্বিতীয়\s*অর্ধ|দিতীয়\s*অর্ধ|বিকেল|সেকেন্ড\s*হাফ"
    r")",
    re.I | re.UNICODE,
)


def parse_wizard_leave_type_answer(message: str) -> str | None:
    """Parse sick / annual / unpaid wizard Select Leave answers."""
    text = (message or "").strip()
    if not text:
        return None
    low = text.lower()
    if low in {"sick", "annual", "unpaid"}:
        return low
    if _WIZARD_UNPAID_RE.search(text):
        return "unpaid"
    if _WIZARD_SICK_RE.search(text):
        return "sick"
    if _WIZARD_ANNUAL_RE.search(text):
        return "annual"
    return None


def parse_half_day_period_answer(message: str) -> str | None:
    """Parse first half vs second half for half-day leave."""
    text = (message or "").strip()
    if not text:
        return None
    has_first = bool(_HALF_FIRST_RE.search(text))
    has_second = bool(_HALF_SECOND_RE.search(text))
    if has_first and not has_second:
        return "first"
    if has_second and not has_first:
        return "second"
    if text.lower() in {"first", "1", "1st", "prothom", "prathom"}:
        return "first"
    if text.lower() in {"second", "2", "2nd", "ditiyo", "ditio"}:
        return "second"
    return None


def message_explicitly_states_day_scope(message: str) -> bool:
    """True only when the user clearly said full/half day in this message."""
    return parse_day_scope_answer(message) is not None


def message_explicitly_states_payment_category(message: str) -> bool:
    """True only when the user clearly said paid or unpaid in this message."""
    text = (message or "").strip()
    if not text:
        return False
    if _PAYMENT_LWOP_RE.search(text) or _PAYMENT_PAID_RE.search(text):
        return True
    return bool(
        re.search(
            r"\b(paid|unpaid|lwop)\b.{0,20}\b(hobe|habe|lagbe|nite|chai)\b|"
            r"\b(hobe|habe|lagbe)\b.{0,20}\b(paid|unpaid|lwop)\b",
            text,
            re.I | re.UNICODE,
        )
    )


def strip_ungrounded_payment_category(
    entities: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    """Remove invented paid/unpaid unless the message explicitly states payment."""
    if not entities or message_explicitly_states_payment_category(message):
        return entities
    out = dict(entities)
    out.pop("leave_payment_category", None)
    return out


def should_suppress_inferred_leave_dates(message: str) -> bool:
    """
    True when the user gave leave length (e.g. 3 din) but not a calendar start date.

    LLM often guesses tomorrow — we must ask SLOT_DATES instead.
    """
    text = (message or "").strip()
    if not message_mentions_leave_duration(text):
        return False
    return not message_explicitly_states_leave_date(text)


def strip_ungrounded_leave_dates(
    entities: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    """Drop guessed calendar dates when only duration was stated."""
    if not entities or not should_suppress_inferred_leave_dates(message):
        return entities
    out = dict(entities)
    for key in ("start_date", "end_date", "date"):
        out.pop(key, None)
    return out


def strip_ungrounded_day_scope(
    entities: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    """Remove invented day_scope unless the message explicitly states full/half."""
    if not entities or message_explicitly_states_day_scope(message):
        return entities
    out = dict(entities)
    out.pop("day_scope", None)
    return out


def _low(text: str) -> str:
    return (text or "").lower()


def text_has_sick_signal(text: str) -> bool:
    low = _low(text)
    return any(sig in low for sig in SICK_SIGNALS)


def normalize_payment_category(value: Any) -> str | None:
    if value is None or value == "":
        return None
    raw = str(value).strip().lower()
    if raw in {LEAVE_PAYMENT_PAID, "pto", "with pay"}:
        return LEAVE_PAYMENT_PAID
    if raw in {LEAVE_PAYMENT_LWOP, "unpaid", "without pay", "no pay"}:
        return LEAVE_PAYMENT_LWOP
    if _PAYMENT_LWOP_RE.search(raw):
        return LEAVE_PAYMENT_LWOP
    if _PAYMENT_PAID_RE.search(raw):
        return LEAVE_PAYMENT_PAID
    return None


def normalize_day_scope(value: Any) -> str | None:
    if value is None or value == "":
        return None
    raw = str(value).strip().lower()
    if raw in {DAY_SCOPE_HALF, "half_day", "half-day"}:
        return DAY_SCOPE_HALF
    if raw in {DAY_SCOPE_FULL, "full_day", "full-day"}:
        return DAY_SCOPE_FULL
    if _SCOPE_HALF_RE.search(raw):
        return DAY_SCOPE_HALF
    if _SCOPE_FULL_RE.search(raw):
        return DAY_SCOPE_FULL
    return None


def infer_leave_type_from_text(*texts: str) -> str | None:
    """Infer sick leave type from reason or message text when not explicitly set."""
    combined = " ".join(t for t in texts if t).strip()
    if not combined:
        return None
    low = _low(combined)
    if text_has_sick_signal(low):
        return "sick"
    if re.search(
        r"\b(casual)\b|ক্যাজুয়াল|নৈমিত্তিক",
        low,
    ):
        return "casual"
    if re.search(r"\b(annual|vacation|pto)\b|বার্ষিক", low):
        return "annual"
    return None


def normalize_reason_text(reason: Any) -> str | None:
    if reason is None:
        return None
    s = str(reason).strip()
    if len(s) < 3:
        return None
    from chat.services.leave_draft_utils import canonicalize_leave_reason

    return canonicalize_leave_reason(s) or None


def normalize_leave_draft(draft: dict[str, Any]) -> None:
    """
    Apply canonical values to a leave draft in place.

    Never clears existing fields; only normalizes or fills gaps.
    """
    pay = normalize_payment_category(draft.get("leave_payment_category"))
    if pay:
        draft["leave_payment_category"] = pay

    scope = normalize_day_scope(draft.get("day_scope"))
    if scope:
        draft["day_scope"] = scope

    reason = normalize_reason_text(draft.get("reason"))
    if reason:
        draft["reason"] = reason

    from chat.services.leave_draft_utils import reconcile_leave_type_from_reason

    reconcile_leave_type_from_reason(draft)
    reason = str(draft.get("reason") or "").strip() or None

    last_msg = str(draft.get("_last_user_message") or "")
    combined = " ".join(
        x
        for x in (
            str(draft.get("reason") or ""),
            last_msg,
        )
        if x
    )
    if not draft.get("leave_type"):
        inferred = infer_leave_type_from_text(combined)
        if inferred == "sick":
            draft["leave_type"] = inferred

    # Sick type with no explicit reason → implied reason (matches legacy behavior).
    lt = str(draft.get("leave_type") or "").lower()
    if lt in ("sick", "medical") and not draft.get("reason") and draft.get("start_date"):
        draft.setdefault("reason", "অসুস্থতা / sick leave")
        draft["_reason_implied"] = True

    from chat.services.leave_draft_utils import apply_duration_end_date

    apply_duration_end_date(draft)
