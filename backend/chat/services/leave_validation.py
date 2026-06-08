"""
Deterministic leave validation: overlap, balance, calendar warnings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from chat.services.leave_calendar import (
    active_blocking_statuses,
    calendar_span_days,
    date_ranges_overlap,
    normalize_leave_record_dates,
    parse_iso_date,
    scan_calendar_warnings,
)
from chat.services.leave_days import compute_requested_leave_days
from chat.services.leave_policies import CompanyLeavePolicy
from chat.services.leave_workflow import LEAVE_PAYMENT_LWOP, LEAVE_PAYMENT_PAID


@dataclass
class OverlapHit:
    request_id: str
    start_date: str
    end_date: str
    status: str


@dataclass
class BalanceAnalysis:
    requested_ledger: float
    balance_available: float
    paid_portion: float
    unpaid_portion: float
    remaining_after: float
    sufficient_for_full_paid: bool
    needs_split: bool


@dataclass
class LeaveValidationResult:
    ok: bool
    blocking: bool = False
    overlap_hits: list[OverlapHit] = field(default_factory=list)
    balance: BalanceAnalysis | None = None
    calendar_warnings: list[str] = field(default_factory=list)
    calendar_span: int = 0
    code: str = ""
    message_bn: str = ""


def _effective_leave_bucket(entities: dict[str, Any]) -> str:
    from chat.services.leave_draft_utils import effective_leave_bucket

    return effective_leave_bucket(entities)


def medical_doc_required(
    entities: dict[str, Any], policy: CompanyLeavePolicy
) -> bool:
    lt = str(entities.get("leave_type") or "").strip().lower()
    type_rule = policy.type_rule(lt)
    min_span = policy.sick_medical_doc_min_calendar_days
    if type_rule and type_rule.medical_doc_min_calendar_days is not None:
        min_span = int(type_rule.medical_doc_min_calendar_days)
    if _effective_leave_bucket(entities) != "sick":
        return False
    start = parse_iso_date(entities.get("start_date"))
    end = parse_iso_date(entities.get("end_date") or entities.get("start_date"))
    if not start or not end:
        return False
    return calendar_span_days(start, end) >= min_span


def analyze_balance(
    entities: dict[str, Any],
    *,
    balance_days: float,
    policy: CompanyLeavePolicy,
) -> BalanceAnalysis:
    requested = compute_requested_leave_days(entities)
    pay = str(entities.get("leave_payment_category") or "").lower()
    if pay == LEAVE_PAYMENT_LWOP:
        return BalanceAnalysis(
            requested_ledger=requested,
            balance_available=balance_days,
            paid_portion=0.0,
            unpaid_portion=requested,
            remaining_after=balance_days,
            sufficient_for_full_paid=False,
            needs_split=False,
        )
    sufficient = balance_days + 1e-9 >= requested
    if sufficient:
        return BalanceAnalysis(
            requested_ledger=requested,
            balance_available=balance_days,
            paid_portion=requested,
            unpaid_portion=0.0,
            remaining_after=balance_days - requested,
            sufficient_for_full_paid=True,
            needs_split=False,
        )
    if policy.allow_split_paid_unpaid and balance_days > 0:
        paid_part = min(balance_days, requested)
        unpaid_part = max(0.0, requested - paid_part)
        return BalanceAnalysis(
            requested_ledger=requested,
            balance_available=balance_days,
            paid_portion=paid_part,
            unpaid_portion=unpaid_part,
            remaining_after=0.0,
            sufficient_for_full_paid=False,
            needs_split=unpaid_part > 0,
        )
    return BalanceAnalysis(
        requested_ledger=requested,
        balance_available=balance_days,
        paid_portion=0.0,
        unpaid_portion=requested,
        remaining_after=balance_days,
        sufficient_for_full_paid=False,
        needs_split=False,
    )


def find_overlapping_requests(
    *,
    company_id: str,
    employee_id: str,
    start: date,
    end: date,
    existing_records: list[dict[str, Any]],
    exclude_request_id: str | None = None,
) -> list[OverlapHit]:
    blocking = active_blocking_statuses()
    hits: list[OverlapHit] = []
    for rec in existing_records or []:
        if str(rec.get("company_id") or "") not in ("", company_id):
            if rec.get("company_id") and rec.get("company_id") != company_id:
                continue
        if str(rec.get("employee_id") or "") not in ("", employee_id):
            if rec.get("employee_id") and rec.get("employee_id") != employee_id:
                continue
        rid = str(rec.get("request_id") or "")
        if exclude_request_id and rid == exclude_request_id:
            continue
        st = str(rec.get("status") or rec.get("leave_status") or "").upper()
        st_lower = str(rec.get("status") or rec.get("leave_status") or "").lower()
        if st not in blocking and st_lower not in blocking:
            continue
        rs, re_ = normalize_leave_record_dates(rec)
        if not rs or not re_:
            continue
        if date_ranges_overlap(start, end, rs, re_):
            hits.append(
                OverlapHit(
                    request_id=rid,
                    start_date=rs.isoformat(),
                    end_date=re_.isoformat(),
                    status=st or st_lower,
                )
            )
    return hits


def validate_leave_request(
    entities: dict[str, Any],
    *,
    policy: CompanyLeavePolicy,
    balance_days: float,
    existing_records: list[dict[str, Any]],
    company_id: str,
    employee_id: str,
) -> LeaveValidationResult:
    start = parse_iso_date(entities.get("start_date"))
    end = parse_iso_date(entities.get("end_date") or entities.get("start_date"))
    if not start:
        return LeaveValidationResult(
            ok=False,
            blocking=True,
            code="DATES_MISSING",
            message_bn="ছুটির তারিখ পাওয়া যায়নি।",
        )
    if not end:
        end = start

    span = calendar_span_days(start, end)
    warnings = scan_calendar_warnings(start=start, end=end, policy=policy)
    overlaps = find_overlapping_requests(
        company_id=company_id,
        employee_id=employee_id,
        start=start,
        end=end,
        existing_records=existing_records,
    )
    if overlaps:
        first = overlaps[0]
        return LeaveValidationResult(
            ok=False,
            blocking=True,
            overlap_hits=overlaps,
            calendar_warnings=warnings,
            calendar_span=span,
            code="OVERLAP_EXISTING_LEAVE",
            message_bn=(
                f"এই তারিখে (**{first.start_date}** থেকে **{first.end_date}**) "
                f"ইতিমধ্যে একটি ছুটির আবেদন আছে (রেফারেন্স: {first.request_id or '—'})। "
                "নতুন আবেদন দেওয়ার আগে আগেরটা বাতিল/পরিবর্তন করতে চান কিনা জানান, "
                "অথবা অন্য তারিখ বেছে নিন।"
            ),
        )

    bal = analyze_balance(entities, balance_days=balance_days, policy=policy)
    return LeaveValidationResult(
        ok=True,
        blocking=False,
        balance=bal,
        calendar_warnings=warnings,
        calendar_span=span,
    )
