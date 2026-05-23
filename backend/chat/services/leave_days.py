from datetime import datetime
from typing import Any

from chat.services.leave_draft_utils import DAY_SCOPE_FULL, DAY_SCOPE_HALF


def compute_requested_leave_days(entities: dict[str, Any]) -> float:
    """
    Paid leave ledger units booked for a request — DecisionEngine vs CRM deduction stay aligned.
    Full-day: calendar span in days when start/end supplied, else explicit days_needed or 1.
    Half-day each calendar day in span: multiply by 0.5.
    """
    days_needed = entities.get("days")
    start = entities.get("start_date")
    end = entities.get("end_date")
    scope = str(entities.get("day_scope") or DAY_SCOPE_FULL).strip().lower()
    multiplier = 0.5 if scope in {"half", "half_day", "half-day"} else 1.0

    requested = float(days_needed or 1)
    if start and end:
        try:
            s = datetime.fromisoformat(str(start).split("T")[0]).date()
            e = datetime.fromisoformat(str(end).split("T")[0]).date()
            requested = max(1.0, float((e - s).days + 1))
        except Exception:
            requested = float(days_needed or 1)

    ledger = requested * multiplier
    return max(0.5, float(ledger))


def leave_booking_signature(entities: dict[str, Any]) -> tuple[str, str, float, str, str]:
    """Stable tuple for duplicate leave detection within a session."""
    start_raw = entities.get("start_date") or entities.get("date")
    end_raw = entities.get("end_date")
    start_s = ""
    end_s = ""
    if start_raw:
        try:
            start_s = str(datetime.fromisoformat(str(start_raw).split("T")[0]).date())
        except Exception:
            start_s = str(start_raw).split("T")[0]
    if end_raw:
        try:
            end_s = str(datetime.fromisoformat(str(end_raw).split("T")[0]).date())
        except Exception:
            end_s = str(end_raw).split("T")[0]
    ledger = compute_requested_leave_days(entities or {})
    pay = str((entities or {}).get("leave_payment_category") or "")
    scope = str((entities or {}).get("day_scope") or "")
    return (start_s, end_s, float(ledger), pay, scope)
