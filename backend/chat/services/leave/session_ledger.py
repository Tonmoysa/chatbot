"""
Session-aware leave balance — per-type entitlement minus days used in this session.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.leave_days import compute_requested_leave_days
from chat.services.leave_policies import (
    DEFAULT_LEAVE_ENTITLEMENTS,
    LEAVE_TYPE_ANNUAL,
    LEAVE_TYPE_SICK,
    LEAVE_TYPE_UNPAID,
    get_company_leave_policy,
)

_BALANCE_TYPES: tuple[str, ...] = (LEAVE_TYPE_SICK, LEAVE_TYPE_ANNUAL, LEAVE_TYPE_UNPAID)

_TYPE_LABELS: dict[str, str] = {
    LEAVE_TYPE_SICK: "Sick leave",
    LEAVE_TYPE_ANNUAL: "Annual leave",
    LEAVE_TYPE_UNPAID: "Leave without pay",
}

_SICK_BALANCE_RE = re.compile(
    r"\b(sick|medical|health|osusto|oshustho)\b|অসুস্থ|সিক\s*লিভ",
    re.I | re.UNICODE,
)
_ANNUAL_BALANCE_RE = re.compile(
    r"\b(annual|vacation|pto)\b|বার্ষিক|annual\s*leave",
    re.I | re.UNICODE,
)
_UNPAID_BALANCE_RE = re.compile(
    r"\b(lwop|unpaid|without\s+pay|leave\s+without\s+pay)\b|বেতন\s*ছাড়া|বিনা\s*বেতন",
    re.I | re.UNICODE,
)


def detect_balance_leave_type(message: str) -> str | None:
    """Return sick / annual / unpaid when the user asks about a specific balance."""
    raw = (message or "").strip()
    if not raw:
        return None
    if _SICK_BALANCE_RE.search(raw):
        return LEAVE_TYPE_SICK
    if _ANNUAL_BALANCE_RE.search(raw):
        return LEAVE_TYPE_ANNUAL
    if _UNPAID_BALANCE_RE.search(raw):
        return LEAVE_TYPE_UNPAID
    return None


def _normalize_balance_type(leave_type: str | None) -> str | None:
    lt = str(leave_type or "").strip().lower()
    if lt in _BALANCE_TYPES:
        return lt
    if lt in ("medical", "health"):
        return LEAVE_TYPE_SICK
    if lt in ("lwop",):
        return LEAVE_TYPE_UNPAID
    if lt == "casual":
        return LEAVE_TYPE_ANNUAL
    return None


def _entitlements_for_company(company_id: str) -> dict[str, float]:
    policy = get_company_leave_policy((company_id or "").strip() or "default")
    ent = dict(DEFAULT_LEAVE_ENTITLEMENTS)
    ent.update(dict(getattr(policy, "leave_entitlements", {}) or {}))
    return {k: float(v) for k, v in ent.items() if k in _BALANCE_TYPES}


def sum_session_leave_days_by_type(
    crm: Any,
    *,
    company_id: str,
    employee_id: str,
    session_id: str,
) -> dict[str, float]:
    """Sum submitted leave days in this session, grouped by leave type."""
    used: dict[str, float] = {t: 0.0 for t in _BALANCE_TYPES}
    try:
        pack = crm.list_employee_leave_requests(
            company_id=company_id,
            employee_id=employee_id,
            session_id=session_id,
        )
    except Exception:
        return used

    for row in list(pack.get("leave_requests") or []):
        if str(row.get("session_id") or "") != str(session_id):
            continue
        ent = dict(row.get("entities") or {})
        lt = _normalize_balance_type(ent.get("leave_type"))
        if not lt:
            pay = str(ent.get("leave_payment_category") or "").strip().lower()
            if pay == "lwop":
                lt = LEAVE_TYPE_UNPAID
            else:
                continue
        days = compute_requested_leave_days(ent)
        used[lt] = used.get(lt, 0.0) + float(days)
    return used


def build_session_leave_balance(
    crm: Any,
    *,
    company_id: str,
    employee_id: str,
    session_id: str,
    leave_type_filter: str | None = None,
) -> dict[str, Any]:
    """
    Build per-type balance summary: allocated, used_session, remaining.
    """
    entitlements = _entitlements_for_company(company_id)
    used = sum_session_leave_days_by_type(
        crm,
        company_id=company_id,
        employee_id=employee_id,
        session_id=session_id,
    )
    balances: dict[str, dict[str, float]] = {}
    for lt in _BALANCE_TYPES:
        allocated = float(entitlements.get(lt, 12.0))
        taken = float(used.get(lt, 0.0))
        balances[lt] = {
            "allocated": allocated,
            "used_session": taken,
            "remaining": max(0.0, allocated - taken),
        }

    primary_remaining = balances[LEAVE_TYPE_ANNUAL]["remaining"]
    return {
        "balances_by_type": balances,
        "leave_balance_days": primary_remaining,
        "balance_query_type": leave_type_filter,
    }


def format_leave_balance_message(
    balances: dict[str, dict[str, float]],
    *,
    leave_type_filter: str | None = None,
) -> str:
    """Human-readable balance answer (Banglish-friendly)."""
    if leave_type_filter and leave_type_filter in balances:
        row = balances[leave_type_filter]
        label = _TYPE_LABELS.get(leave_type_filter, leave_type_filter)
        return (
            f"**{label}:** মোট **{row['allocated']:g}** দিন, "
            f"এই session-এ নিয়েছেন **{row['used_session']:g}** দিন, "
            f"**বাকি {row['remaining']:g}** দিন।"
        )

    lines = ["**আপনার leave balance (এই session):**"]
    for lt in _BALANCE_TYPES:
        row = balances[lt]
        label = _TYPE_LABELS[lt]
        lines.append(
            f"• **{label}:** মোট {row['allocated']:g} দিন | "
            f"নিয়েছেন {row['used_session']:g} দিন | বাকি **{row['remaining']:g}** দিন"
        )
    return "\n".join(lines)
