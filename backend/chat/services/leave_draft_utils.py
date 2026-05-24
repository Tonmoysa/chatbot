"""Shared leave draft helpers (no workflow/slot circular imports)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

LEAVE_PAYMENT_PAID = "paid"
LEAVE_PAYMENT_LWOP = "lwop"
DAY_SCOPE_FULL = "full"
DAY_SCOPE_HALF = "half"

_SICK_DOCUMENT_MIN_SPAN_DAYS = 3


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


def effective_leave_bucket(draft: dict[str, Any]) -> str:
    lt = str(draft.get("leave_type") or "").strip().lower()
    reason_l = str(draft.get("reason") or "").lower()
    if lt in {"sick", "medical", "health"}:
        return "sick"
    if any(w in reason_l for w in ("sick", "ill", "fever", "medical", "doctor", "অসুস্থ")):
        return "sick"
    return "other"


def supporting_document_needed(draft: dict[str, Any]) -> bool:
    return (
        effective_leave_bucket(draft) == "sick"
        and calendar_span_days(draft) >= _SICK_DOCUMENT_MIN_SPAN_DAYS
    )


def normalize_end_equals_start_if_missing(draft: dict[str, Any]) -> None:
    s = draft.get("start_date")
    if draft.get("end_date") or not s:
        return
    draft["end_date"] = str(s).strip().split("T")[0]


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
    if not draft.get("leave_payment_category"):
        tr = policy.type_rule(str(draft.get("leave_type") or ""))
        if tr and not tr.paid:
            draft["leave_payment_category"] = LEAVE_PAYMENT_LWOP
        else:
            draft["leave_payment_category"] = LEAVE_PAYMENT_PAID
    if not draft.get("day_scope"):
        draft["day_scope"] = DAY_SCOPE_FULL


def format_select_leave_label(draft: dict[str, Any]) -> str:
    """CRM Select Leave — paid or unpaid only."""
    pay = str(draft.get("leave_payment_category") or "").strip().lower()
    if pay == LEAVE_PAYMENT_PAID:
        return "paid"
    if pay == LEAVE_PAYMENT_LWOP:
        return "unpaid"
    return "—"
