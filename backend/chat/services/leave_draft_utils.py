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
    r"ফ্যামিলি|পরিবার|"
    r"wedding|marriage|biye|বিয়ে|"
    r"travel|trip|tour|vacation|holiday|যাত্রা|"
    r"funeral|bereavement|শোক|"
    r"ceremon|program(?:me)?|event|অনুষ্ঠান|প্রোগ্রাম|"
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


def reconcile_leave_type_from_reason(draft: dict[str, Any]) -> None:
    """Keep leave_type aligned with the stated reason (sick vs family/casual)."""
    reason = canonicalize_leave_reason(str(draft.get("reason") or ""))
    if not reason:
        return
    if reason != str(draft.get("reason") or "").strip():
        draft["reason"] = reason
    if reason_indicates_non_sick_leave(reason):
        lt = str(draft.get("leave_type") or "").lower()
        if lt in ("sick", "medical", "health", "casual"):
            draft["leave_type"] = "annual"
            sync_payment_from_leave_type(draft)
        draft.pop("_reason_implied", None)
        clear_supporting_document_if_unneeded(draft)
        return
    from chat.services.leave.normalization import infer_leave_type_from_text

    inferred = infer_leave_type_from_text(reason)
    if inferred:
        draft["leave_type"] = inferred


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
    from chat.services.leave_policies import ALL_LEAVE_TYPES

    if not draft.get("leave_type"):
        dlt = str(
            getattr(policy, "default_leave_type_if_unspecified", "casual") or "casual"
        ).strip().lower()
        if dlt not in ALL_LEAVE_TYPES:
            dlt = "casual"
        draft["leave_type"] = dlt
    sync_payment_from_leave_type(draft)
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
