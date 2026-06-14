"""Shared leave draft helpers (no workflow/slot circular imports)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

LEAVE_PAYMENT_PAID = "paid"
LEAVE_PAYMENT_LWOP = "lwop"
DAY_SCOPE_FULL = "full"
DAY_SCOPE_HALF = "half"
HALF_PERIOD_FIRST = "first"
HALF_PERIOD_SECOND = "second"

WIZARD_LEAVE_TYPES: frozenset[str] = frozenset({"sick", "annual", "unpaid"})

KEY_STATED_LEAVE_TYPE = "_stated_leave_type"


def resolve_explicit_wizard_leave_type(message: str) -> str | None:
    """Leave type the user explicitly named (wizard token or phrase), if any."""
    from chat.services.leave.normalization import parse_wizard_leave_type_answer
    from chat.services.leave_slot_extraction import explicit_leave_type_from_message

    raw = parse_wizard_leave_type_answer(message) or explicit_leave_type_from_message(
        message
    )
    if not raw:
        return None
    lt = str(raw).strip().lower()
    if lt == "casual":
        return "annual"
    if lt in WIZARD_LEAVE_TYPES:
        return lt
    return None


def persist_stated_leave_type(draft: dict[str, Any], message: str) -> None:
    """Remember an explicit leave-type choice across balance interrupts."""
    lt = resolve_explicit_wizard_leave_type(message)
    if lt:
        draft[KEY_STATED_LEAVE_TYPE] = lt


def stated_leave_type_from_draft(draft: dict[str, Any]) -> str | None:
    lt = str(draft.get(KEY_STATED_LEAVE_TYPE) or "").strip().lower()
    return lt if lt in WIZARD_LEAVE_TYPES else None

_SICK_DOCUMENT_MIN_SPAN_DAYS = 3

_REASON_SKIP_RE = re.compile(
    r"(?:"
    r"^skip$|"
    r"\bskip\b|"
    r"^na$|^no$|"
    r"lagbe\s*na|dorkar\s*na|thak|thakbe|"
    r"না\s*লাগবে|লাগবে\s*না|দরকার\s*না|থাক"
    r")",
    re.I | re.UNICODE,
)

_NON_SICK_LEAVE_REASON_RE = re.compile(
    r"(?:"
    r"famil+y?(?:\s+problem|\s+program|\s+issue|\s+event)?|"
    r"famil\s+program|"
    r"family\s+program|family\s+problem|family\s+issue|family\s+event|"
    r"ফ্যামিলি|পরিবার|পারিবারিক|"
    r"wedding|marriage|biye|বিয়ে|"
    r"travel|trip|tour|vacation|holiday|যাত্রা|"
    r"funeral|bereavement|শোক|"
    r"ceremon|program(?:me)?|event|অনুষ্ঠান|প্রোগ্রাম|"
    r"personal(?:\s+work|\s+matter|\s+reason)?|"
    r"ব্যক্তিগত(?:\s+কাজ)?|"
    r"annual\s+leave|casual\s+leave|maternity|paternity"
    r")",
    re.I | re.UNICODE,
)

_REASON_HOBE_META_RE = re.compile(
    r"^(.+?)\s+(?:hobe|habe|hoy|হবে|হয়)\s+(?:reason|reas[oi]n|karon|কারণ)\s*$",
    re.I | re.UNICODE,
)

_NON_SICK_LEAVE_TYPES = frozenset(
    {
        "casual",
        "annual",
        "maternity",
        "paternity",
        "bereavement",
        "compensatory",
        "emergency",
        "wedding",
        "travel",
    }
)


def today() -> date:
    return date.today()


def parse_iso(d: Any) -> date | None:
    if not d:
        return None
    try:
        return datetime.fromisoformat(str(d).strip().split("T")[0]).date()
    except Exception:
        return None


def calendar_span_days(draft: dict[str, Any]) -> int:
    s = parse_iso(draft.get("start_date"))
    e = parse_iso(draft.get("end_date") or draft.get("start_date"))
    if not s or not e:
        return 0
    return max(0, (e - s).days) + 1


def is_multi_day_leave(draft: dict[str, Any]) -> bool:
    """True when the request spans more than one calendar day."""
    try:
        if draft.get("days") is not None and float(draft["days"]) > 1:
            return True
    except (TypeError, ValueError):
        pass
    span = calendar_span_days(draft)
    return span > 1


def is_reason_skip_message(message: str) -> bool:
    text = (message or "").strip()
    if not text:
        return False
    return bool(_REASON_SKIP_RE.search(text))


def sync_payment_from_leave_type(draft: dict[str, Any]) -> None:
    """Map wizard leave type → paid / lwop payment category."""
    lt = str(draft.get("leave_type") or "").strip().lower()
    if lt == "unpaid":
        draft["leave_payment_category"] = LEAVE_PAYMENT_LWOP
    elif lt in WIZARD_LEAVE_TYPES:
        draft["leave_payment_category"] = LEAVE_PAYMENT_PAID


def apply_multi_day_scope_default(draft: dict[str, Any]) -> None:
    """Multi-day leave is always full day — no half-day prompt."""
    if is_multi_day_leave(draft):
        draft["day_scope"] = DAY_SCOPE_FULL
        draft.pop("half_day_period", None)


def apply_single_day_scope_default(draft: dict[str, Any]) -> None:
    """Single calendar-day leave defaults to full day when scope was never asked."""
    if draft.get("day_scope") or is_multi_day_leave(draft):
        return
    start = str(draft.get("start_date") or "").strip()
    if not start:
        return
    end = str(draft.get("end_date") or start).strip()
    if end != start:
        return
    try:
        days = float(draft.get("days") or 0)
    except (TypeError, ValueError):
        days = 0.0
    if days and days != 1.0:
        return
    draft["day_scope"] = DAY_SCOPE_FULL
    draft.pop("half_day_period", None)


def needs_half_day_period(draft: dict[str, Any]) -> bool:
    return (
        str(draft.get("day_scope") or "").lower() == DAY_SCOPE_HALF
        and not is_multi_day_leave(draft)
    )


def canonicalize_leave_reason(reason: str) -> str:
    """Normalize Banglish reason corrections (famil program, X hobe reason)."""
    text = (reason or "").strip()
    if not text:
        return ""
    m = _REASON_HOBE_META_RE.match(text)
    if m:
        text = m.group(1).strip()
    if re.fullmatch(r"famil+y?", text, re.I):
        return "family"
    if re.search(r"famil+y?\s+program", text, re.I):
        return "family program"
    if re.fullmatch(r"tour", text, re.I):
        return "travel"
    if re.fullmatch(r"travel|trip|vacation", text, re.I):
        return text.lower()
    return text[:2000]


def reason_indicates_non_sick_leave(reason: str) -> bool:
    """Family, travel, wedding, etc. — never treat as sick for document rules."""
    text = canonicalize_leave_reason(reason)
    if not text:
        return False
    return bool(_NON_SICK_LEAVE_REASON_RE.search(text))


def invalidate_leave_type_for_reselect(draft: dict[str, Any]) -> None:
    """Clear wizard leave type so the user must pick Sick / Annual / LWOP again (R03)."""
    draft.pop("leave_type", None)
    draft.pop("leave_payment_category", None)
    draft.pop("_reason_implied", None)
    draft.pop("_leave_bucket", None)
    draft.pop("_leave_bucket_confidence", None)
    draft.pop(KEY_STATED_LEAVE_TYPE, None)
    draft["_leave_type_reselect_required"] = True
    clear_supporting_document_if_unneeded(draft)


def is_non_sick_wizard_leave(draft: dict[str, Any]) -> bool:
    """Family/travel/etc. — user must pick annual vs leave without pay."""
    if draft.get("_leave_type_reselect_required"):
        return True
    reason = canonicalize_leave_reason(str(draft.get("reason") or ""))
    if reason_indicates_non_sick_leave(reason):
        return True
    lt = str(draft.get("leave_type") or "").lower()
    if lt == "sick":
        return False
    if not reason:
        stated = stated_leave_type_from_draft(draft)
        if stated == "sick":
            return False
    return effective_leave_bucket(draft) == "other"


def should_auto_infer_wizard_leave_type(draft: dict[str, Any]) -> bool:
    """Only sick leave may be inferred without asking Select Leave."""
    if is_non_sick_wizard_leave(draft):
        return False
    from chat.services.leave.normalization import infer_leave_type_from_text, text_has_sick_signal

    combined = " ".join(
        x
        for x in (
            str(draft.get("reason") or ""),
            str(draft.get("_last_user_message") or ""),
        )
        if x
    ).strip()
    if not combined:
        return False
    if text_has_sick_signal(combined):
        return True
    return infer_leave_type_from_text(combined) == "sick"


def reconcile_leave_type_from_reason(draft: dict[str, Any]) -> None:
    """Keep leave_type aligned with the stated reason (sick vs family/casual)."""
    from chat.services.leave.reason_bucket_classifier import clear_leave_bucket_cache

    reason = canonicalize_leave_reason(str(draft.get("reason") or ""))
    if not reason:
        return
    if reason != str(draft.get("reason") or "").strip():
        draft["reason"] = reason
    if reason_indicates_non_sick_leave(reason):
        last = str(draft.get("_last_user_message") or "")
        explicit = resolve_explicit_wizard_leave_type(last)
        lt = str(draft.get("leave_type") or "").lower()
        if explicit in ("annual", "unpaid"):
            draft["leave_type"] = explicit
            draft[KEY_STATED_LEAVE_TYPE] = explicit
            sync_payment_from_leave_type(draft)
            draft.pop("_leave_type_reselect_required", None)
        elif lt in {"annual", "unpaid"}:
            sync_payment_from_leave_type(draft)
            draft.pop("_leave_type_reselect_required", None)
        elif lt == "casual":
            draft["leave_type"] = "annual"
            draft[KEY_STATED_LEAVE_TYPE] = "annual"
            sync_payment_from_leave_type(draft)
            draft.pop("_leave_type_reselect_required", None)
        elif lt in ("sick", "medical", "health") or stated_leave_type_from_draft(draft) == "sick":
            invalidate_leave_type_for_reselect(draft)
        elif lt:
            draft.pop("leave_type", None)
            draft.pop("leave_payment_category", None)
            draft.pop(KEY_STATED_LEAVE_TYPE, None)
            draft["_leave_type_reselect_required"] = True
        else:
            draft.pop(KEY_STATED_LEAVE_TYPE, None)
            draft["_leave_type_reselect_required"] = True
        draft.pop("_reason_implied", None)
        clear_supporting_document_if_unneeded(draft)
        clear_leave_bucket_cache(draft)
        return
    from chat.services.leave.normalization import infer_leave_type_from_text, text_has_sick_signal

    if text_has_sick_signal(reason) and str(draft.get("leave_type") or "").lower() in (
        "sick",
        "medical",
        "health",
    ):
        draft.pop("_leave_type_reselect_required", None)
        return
    inferred = infer_leave_type_from_text(reason)
    if inferred == "sick" and not draft.get("leave_type"):
        draft["leave_type"] = inferred
        sync_payment_from_leave_type(draft)


def sync_days_from_calendar_range(draft: dict[str, Any]) -> None:
    """Align ``days`` with start/end when the user gives an explicit date range."""
    span = calendar_span_days(draft)
    if span >= 1:
        draft["days"] = float(span)
        apply_multi_day_scope_default(draft)


def effective_leave_bucket(draft: dict[str, Any]) -> str:
    cached = draft.get("_leave_bucket")
    if cached in ("sick", "other"):
        return str(cached)
    reason = str(draft.get("reason") or "")
    if reason_indicates_non_sick_leave(reason):
        return "other"
    lt = str(draft.get("leave_type") or "").strip().lower()
    if lt in _NON_SICK_LEAVE_TYPES:
        return "other"
    if lt in {"sick", "medical", "health"}:
        return "sick"
    reason_l = reason.lower()
    try:
        from chat.services.leave.normalization import text_has_sick_signal

        if text_has_sick_signal(reason_l):
            return "sick"
    except ImportError:
        pass
    if any(w in reason_l for w in ("sick", "ill", "fever", "medical", "doctor", "অসুস্থ")):
        return "sick"
    return "other"


def supporting_document_needed(draft: dict[str, Any]) -> bool:
    return (
        effective_leave_bucket(draft) == "sick"
        and calendar_span_days(draft) >= _SICK_DOCUMENT_MIN_SPAN_DAYS
    )


def clear_supporting_document_if_unneeded(draft: dict[str, Any]) -> None:
    """Drop document fields when sick/medical proof is no longer required."""
    if supporting_document_needed(draft):
        return
    draft.pop("document_text", None)
    draft.pop("supporting_document_waived", None)


_DOCUMENT_SKIP_RE = re.compile(
    r"(?:"
    r"^skip$|"
    r"\bskip\b|"
    r"parbo\s*na|parbona|parben\s*na|parbo\s*nah|nah\s*parbo|"
    r"parchi\s*na|debo\s*na|dite\s*parbo\s*na|dite\s*parchi\s*na|"
    r"thak|thakbe|rekhe\s*din|por\s*e\s*debo|"
    r"না\s*পারব|পারব\s*না|দিতে\s*পারব\s*না|"
    r"ache\s*na|nai|nei"
    r")",
    re.I | re.UNICODE,
)


def is_supporting_document_skip_message(message: str) -> bool:
    """True when the user cannot / will not attach a document now (wizard skip)."""
    text = (message or "").strip()
    if not text:
        return False
    if text.lower() == "skip":
        return True
    return bool(_DOCUMENT_SKIP_RE.search(text))


def has_real_supporting_document(draft: dict[str, Any]) -> bool:
    """True only when uploaded/pasted document content exists (not a refusal phrase)."""
    if draft.get("supporting_document_waived"):
        return False
    doc = str(draft.get("document_text") or "").strip()
    if not doc:
        return False
    return not is_supporting_document_skip_message(doc)


def normalize_end_equals_start_if_missing(draft: dict[str, Any]) -> None:
    s = draft.get("start_date")
    if draft.get("end_date") or not s:
        return
    draft["end_date"] = str(s).strip().split("T")[0]


def apply_duration_end_date(draft: dict[str, Any]) -> None:
    """
    Extend end_date when draft.days > 1 and only a start day was captured.

    E.g. days=3 from turn 1 + \"agamikal theke\" on the date step → 3-day span.
    Skips when the calendar range already covers the requested day count.
    """
    s = parse_iso(draft.get("start_date"))
    if not s:
        return
    try:
        n = int(float(draft.get("days") or 0))
    except (TypeError, ValueError):
        return
    if n <= 1:
        return
    e = parse_iso(draft.get("end_date") or draft.get("start_date"))
    if not e:
        e = s
    current_span = max(1, (e - s).days + 1)
    if current_span >= n:
        return
    draft["end_date"] = (s + timedelta(days=n - 1)).isoformat()


def validate_dates(draft: dict[str, Any]) -> tuple[bool, str | None]:
    s = parse_iso(draft.get("start_date"))
    e = parse_iso(draft.get("end_date") or draft.get("start_date"))
    if not s:
        return True, None
    if not e:
        e = s
        draft.setdefault("end_date", s.isoformat())
    if s < today():
        return False, "IN_PAST"
    if e < s:
        return False, "BAD_RANGE"
    return True, None


def apply_leave_draft_defaults(draft: dict[str, Any], policy: Any) -> None:
    """
    Enterprise defaults when the user omits category (paid/unpaid), duration, or leave type.
    Tenant policy may override default leave type code; per-type rules adjust paid/unpaid.
    """
    if not draft.get("leave_type") and should_auto_infer_wizard_leave_type(draft):
        from chat.services.leave.normalization import infer_leave_type_from_text

        combined = " ".join(
            x
            for x in (
                str(draft.get("reason") or ""),
                str(draft.get("_last_user_message") or ""),
            )
            if x
        )
        inferred = infer_leave_type_from_text(combined)
        if inferred == "casual":
            inferred = "annual"
        if inferred in WIZARD_LEAVE_TYPES:
            draft["leave_type"] = inferred
    lt = str(draft.get("leave_type") or "").strip().lower()
    if lt == "casual":
        draft["leave_type"] = "annual"
    sync_payment_from_leave_type(draft)
    # Single-day leave: day_scope must come from the user (workflow_schema SLOT_SCOPE).
    # Multi-day spans auto-default to full via apply_multi_day_scope_default below.
    if not draft.get("leave_payment_category"):
        tr = policy.type_rule(str(draft.get("leave_type") or ""))
        if tr and not tr.paid:
            draft["leave_payment_category"] = LEAVE_PAYMENT_LWOP
        else:
            draft["leave_payment_category"] = LEAVE_PAYMENT_PAID
    apply_multi_day_scope_default(draft)


def format_select_leave_label(draft: dict[str, Any]) -> str:
    """CRM Select Leave — sick / annual / unpaid (leave without pay)."""
    lt = str(draft.get("leave_type") or "").strip().lower()
    if lt == "sick":
        return "sick leave"
    if lt == "annual":
        return "annual leave"
    if lt == "unpaid":
        return "leave without pay"
    pay = str(draft.get("leave_payment_category") or "").strip().lower()
    if pay == LEAVE_PAYMENT_PAID:
        return "paid leave"
    if pay == LEAVE_PAYMENT_LWOP:
        return "leave without pay"
    return "—"


def format_half_day_period_label(draft: dict[str, Any]) -> str:
    period = str(draft.get("half_day_period") or "").strip().lower()
    if period == HALF_PERIOD_FIRST:
        return "first half"
    if period == HALF_PERIOD_SECOND:
        return "second half"
    return "—"
