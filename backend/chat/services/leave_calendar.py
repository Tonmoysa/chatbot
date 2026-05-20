"""
Working-day and holiday helpers for leave validation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable

from chat.services.leave_policies import CompanyLeavePolicy


def parse_iso_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).strip().split("T")[0]).date()
    except Exception:
        return None


def iter_dates_inclusive(start: date, end: date) -> list[date]:
    if end < start:
        return []
    out: list[date] = []
    cur = start
    while cur <= end:
        out.append(cur)
        cur += timedelta(days=1)
    return out


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def is_company_holiday(d: date, policy: CompanyLeavePolicy) -> bool:
    return d.isoformat() in set(policy.holidays_iso)


def calendar_span_days(start: date, end: date) -> int:
    return max(0, (end - start).days) + 1


def count_ledger_eligible_days(
    *,
    start: date,
    end: date,
    policy: CompanyLeavePolicy,
) -> float:
    """
    Days that count toward balance / approval tiers.
    May exclude weekends/holidays per tenant policy.
    """
    days = iter_dates_inclusive(start, end)
    if not days:
        return 0.0
    n = 0
    for d in days:
        if policy.exclude_weekends_from_ledger and is_weekend(d):
            continue
        if policy.exclude_holidays_from_ledger and is_company_holiday(d, policy):
            continue
        n += 1
    return float(max(1, n)) if n else float(len(days))


def scan_calendar_warnings(
    *,
    start: date,
    end: date,
    policy: CompanyLeavePolicy,
) -> list[str]:
    """Non-blocking warnings for UX (weekends / holidays in range)."""
    warnings: list[str] = []
    holidays_hit: list[str] = []
    weekend_days = 0
    for d in iter_dates_inclusive(start, end):
        if policy.warn_on_holidays and is_company_holiday(d, policy):
            holidays_hit.append(d.isoformat())
        if policy.warn_on_weekends and is_weekend(d):
            weekend_days += 1
    if holidays_hit:
        warnings.append(
            "নির্বাচিত তারিখে কোম্পানির ছুটির দিন আছে: "
            + ", ".join(holidays_hit[:5])
            + ("…" if len(holidays_hit) > 5 else "")
        )
    if weekend_days and policy.warn_on_weekends:
        warnings.append(
            f"আপনার বেছে নেওয়া রেঞ্জে {weekend_days}টি সাপ্তাহিক ছুটির দিন (শুক্র–শনি) পড়েছে।"
        )
    return warnings


def date_ranges_overlap(
    a_start: date,
    a_end: date,
    b_start: date,
    b_end: date,
) -> bool:
    return a_start <= b_end and b_start <= a_end


def normalize_leave_record_dates(record: dict[str, Any]) -> tuple[date | None, date | None]:
    ent = record.get("entities") or record
    s = parse_iso_date(ent.get("start_date") or ent.get("date"))
    e = parse_iso_date(ent.get("end_date") or ent.get("start_date") or ent.get("date"))
    if s and not e:
        e = s
    return s, e


def active_blocking_statuses() -> frozenset[str]:
    return frozenset(
        {
            "PENDING",
            "PENDING_APPROVAL",
            "APPROVED",
            "manager_review",
            "hr_review",
            "pending",
            "approved",
        }
    )
