from datetime import datetime
from typing import Any


def compute_requested_leave_days(entities: dict[str, Any]) -> float:
    """
    Days booked for a leave request — kept identical for DecisionEngine vs CRM deduction.
    """
    days_needed = entities.get("days")
    start = entities.get("start_date")
    end = entities.get("end_date")
    requested = float(days_needed or 1)
    if start and end:
        try:
            s = datetime.fromisoformat(str(start).split("T")[0]).date()
            e = datetime.fromisoformat(str(end).split("T")[0]).date()
            requested = max(1.0, float((e - s).days + 1))
        except Exception:
            requested = float(days_needed or 1)
    return max(1.0, requested)
