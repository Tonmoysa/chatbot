"""
Tenant-aware leave policy configuration.

Policies are loaded per company_id from Django settings ``COMPANY_LEAVE_POLICIES``
(JSON-serializable dict). Unknown companies receive enterprise-safe defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.conf import settings

# Canonical leave type codes used across wizard, engine, and CRM payloads.
LEAVE_TYPE_CASUAL = "casual"
LEAVE_TYPE_SICK = "sick"
LEAVE_TYPE_ANNUAL = "annual"
LEAVE_TYPE_UNPAID = "unpaid"
LEAVE_TYPE_MATERNITY = "maternity"
LEAVE_TYPE_PATERNITY = "paternity"
LEAVE_TYPE_EMERGENCY = "emergency"
LEAVE_TYPE_COMPENSATORY = "compensatory"

ALL_LEAVE_TYPES = frozenset(
    {
        LEAVE_TYPE_CASUAL,
        LEAVE_TYPE_SICK,
        LEAVE_TYPE_ANNUAL,
        LEAVE_TYPE_UNPAID,
        LEAVE_TYPE_MATERNITY,
        LEAVE_TYPE_PATERNITY,
        LEAVE_TYPE_EMERGENCY,
        LEAVE_TYPE_COMPENSATORY,
    }
)


@dataclass(frozen=True)
class LeaveTypeRule:
    """Per leave-type policy knobs (tenant overrides via settings)."""

    code: str
    paid: bool = True
    requires_manager: bool = False
    requires_hr: bool = False
    medical_doc_min_calendar_days: int | None = None


# Default annual entitlement per leave type (calendar days) for balance queries.
DEFAULT_LEAVE_ENTITLEMENTS: dict[str, float] = {
    LEAVE_TYPE_SICK: 12.0,
    LEAVE_TYPE_ANNUAL: 12.0,
    LEAVE_TYPE_UNPAID: 12.0,
}

WIZARD_LEAVE_TYPES: frozenset[str] = frozenset(
    {LEAVE_TYPE_SICK, LEAVE_TYPE_ANNUAL, LEAVE_TYPE_UNPAID}
)


@dataclass(frozen=True)
class CompanyLeavePolicy:
    company_id: str
    # When the user does not mention a leave type, this code is used (tenant JSON override).
    default_leave_type_if_unspecified: str = LEAVE_TYPE_CASUAL
    # Per-type balance entitlements (days) for chatbot balance answers.
    leave_entitlements: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_LEAVE_ENTITLEMENTS)
    )
    # Approval tiers (ledger days, after half-day multiplier)
    auto_approve_max_ledger_days: float = 1.0
    manager_approve_max_ledger_days: float = 3.0
    hr_approve_min_ledger_days: float = 5.0
    # Sick / medical proof (inclusive calendar span)
    sick_medical_doc_min_calendar_days: int = 3
    # Calendar behaviour
    exclude_weekends_from_ledger: bool = False
    exclude_holidays_from_ledger: bool = False
    warn_on_weekends: bool = True
    warn_on_holidays: bool = True
    holidays_iso: tuple[str, ...] = ()
    # Balance
    allow_split_paid_unpaid: bool = True
    unpaid_always_requires_approval: bool = True
    leave_types: dict[str, LeaveTypeRule] = field(default_factory=dict)

    def type_rule(self, leave_type: str | None) -> LeaveTypeRule | None:
        lt = (leave_type or "").strip().lower()
        if not lt:
            return None
        return self.leave_types.get(lt)


def _default_leave_type_rules() -> dict[str, LeaveTypeRule]:
    return {
        LEAVE_TYPE_CASUAL: LeaveTypeRule(LEAVE_TYPE_CASUAL, paid=True),
        LEAVE_TYPE_SICK: LeaveTypeRule(
            LEAVE_TYPE_SICK,
            paid=True,
            medical_doc_min_calendar_days=3,
        ),
        LEAVE_TYPE_ANNUAL: LeaveTypeRule(LEAVE_TYPE_ANNUAL, paid=True),
        LEAVE_TYPE_UNPAID: LeaveTypeRule(
            LEAVE_TYPE_UNPAID, paid=False, requires_manager=True
        ),
        LEAVE_TYPE_MATERNITY: LeaveTypeRule(
            LEAVE_TYPE_MATERNITY, paid=True, requires_hr=True
        ),
        LEAVE_TYPE_PATERNITY: LeaveTypeRule(
            LEAVE_TYPE_PATERNITY, paid=True, requires_manager=True
        ),
        LEAVE_TYPE_EMERGENCY: LeaveTypeRule(
            LEAVE_TYPE_EMERGENCY, paid=True, requires_manager=True
        ),
        LEAVE_TYPE_COMPENSATORY: LeaveTypeRule(LEAVE_TYPE_COMPENSATORY, paid=True),
    }


def _default_policy(company_id: str) -> CompanyLeavePolicy:
    return CompanyLeavePolicy(
        company_id=company_id,
        leave_types=_default_leave_type_rules(),
    )


def _parse_type_rules(raw: dict[str, Any]) -> dict[str, LeaveTypeRule]:
    out: dict[str, LeaveTypeRule] = {}
    for code, spec in (raw or {}).items():
        if not isinstance(spec, dict):
            continue
        c = str(code).strip().lower()
        out[c] = LeaveTypeRule(
            code=c,
            paid=bool(spec.get("paid", True)),
            requires_manager=bool(spec.get("requires_manager", False)),
            requires_hr=bool(spec.get("requires_hr", False)),
            medical_doc_min_calendar_days=spec.get("medical_doc_min_calendar_days"),
        )
    return out


def _policy_from_settings(company_id: str, raw: dict[str, Any]) -> CompanyLeavePolicy:
    base = _default_policy(company_id)
    type_overrides = _parse_type_rules(raw.get("leave_types") or {})
    merged_types = {**base.leave_types, **type_overrides}
    holidays = raw.get("holidays_iso") or raw.get("holidays") or []
    def_lt = str(
        raw.get("default_leave_type_if_unspecified")
        or base.default_leave_type_if_unspecified
    ).strip().lower()
    if def_lt not in ALL_LEAVE_TYPES:
        def_lt = base.default_leave_type_if_unspecified
    entitlements = dict(DEFAULT_LEAVE_ENTITLEMENTS)
    raw_ent = raw.get("leave_entitlements") or {}
    if isinstance(raw_ent, dict):
        for code, days in raw_ent.items():
            c = str(code).strip().lower()
            if c in entitlements:
                try:
                    entitlements[c] = float(days)
                except (TypeError, ValueError):
                    pass

    return CompanyLeavePolicy(
        company_id=company_id,
        default_leave_type_if_unspecified=def_lt,
        leave_entitlements=entitlements,
        auto_approve_max_ledger_days=float(
            raw.get("auto_approve_max_ledger_days", base.auto_approve_max_ledger_days)
        ),
        manager_approve_max_ledger_days=float(
            raw.get(
                "manager_approve_max_ledger_days", base.manager_approve_max_ledger_days
            )
        ),
        hr_approve_min_ledger_days=float(
            raw.get("hr_approve_min_ledger_days", base.hr_approve_min_ledger_days)
        ),
        sick_medical_doc_min_calendar_days=int(
            raw.get(
                "sick_medical_doc_min_calendar_days",
                base.sick_medical_doc_min_calendar_days,
            )
        ),
        exclude_weekends_from_ledger=bool(
            raw.get("exclude_weekends_from_ledger", base.exclude_weekends_from_ledger)
        ),
        exclude_holidays_from_ledger=bool(
            raw.get("exclude_holidays_from_ledger", base.exclude_holidays_from_ledger)
        ),
        warn_on_weekends=bool(raw.get("warn_on_weekends", base.warn_on_weekends)),
        warn_on_holidays=bool(raw.get("warn_on_holidays", base.warn_on_holidays)),
        holidays_iso=tuple(str(h).split("T")[0] for h in holidays),
        allow_split_paid_unpaid=bool(
            raw.get("allow_split_paid_unpaid", base.allow_split_paid_unpaid)
        ),
        unpaid_always_requires_approval=bool(
            raw.get("unpaid_always_requires_approval", base.unpaid_always_requires_approval)
        ),
        leave_types=merged_types,
    )


def get_company_leave_policy(company_id: str) -> CompanyLeavePolicy:
    """Resolve tenant leave policy; falls back to enterprise defaults."""
    cid = (company_id or "").strip() or "default"
    table = getattr(settings, "COMPANY_LEAVE_POLICIES", None) or {}
    raw = table.get(cid) or table.get("default") or {}
    if not raw:
        return _default_policy(cid)
    return _policy_from_settings(cid, raw)
