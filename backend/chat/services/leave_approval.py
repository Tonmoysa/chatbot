"""
Rule-based leave approval routing and lifecycle status (not LLM-driven).
"""

from __future__ import annotations

from typing import Any

from chat.services.leave_policies import CompanyLeavePolicy, LEAVE_TYPE_UNPAID
from chat.services.leave_validation import BalanceAnalysis, LeaveValidationResult, medical_doc_required
from chat.services.leave_workflow import LEAVE_PAYMENT_LWOP, LEAVE_PAYMENT_PAID, supporting_document_needed


# CRM / chat lifecycle states
LEAVE_STATUS_DRAFT = "draft"
LEAVE_STATUS_PENDING = "pending"
LEAVE_STATUS_MANAGER_REVIEW = "manager_review"
LEAVE_STATUS_HR_REVIEW = "hr_review"
LEAVE_STATUS_APPROVED = "approved"
LEAVE_STATUS_REJECTED = "rejected"
LEAVE_STATUS_CANCELLED = "cancelled"


def _approval_tier(
    ledger_days: float, policy: CompanyLeavePolicy
) -> str:
    if ledger_days <= policy.auto_approve_max_ledger_days + 1e-9:
        return "auto"
    if ledger_days <= policy.manager_approve_max_ledger_days + 1e-9:
        return "manager"
    if ledger_days >= policy.hr_approve_min_ledger_days - 1e-9:
        return "hr_manager"
    return "manager"


def resolve_leave_decision(
    entities: dict[str, Any],
    *,
    policy: CompanyLeavePolicy,
    validation: LeaveValidationResult,
    crm_context: dict[str, Any],
) -> dict[str, Any]:
    """
    Produce decision-engine-shaped dict with outcome, leave_status, route_to, balance summary.
    """
    if not validation.ok:
        if validation.code == "OVERLAP_EXISTING_LEAVE":
            return {
                "outcome": "NEEDS_CLARIFICATION",
                "reason": validation.message_bn,
                "rules_applied": ["LEAVE_OVERLAP_BLOCKED"],
                "leave_status": LEAVE_STATUS_PENDING,
                "overlap_requests": [
                    {
                        "request_id": h.request_id,
                        "start_date": h.start_date,
                        "end_date": h.end_date,
                        "status": h.status,
                    }
                    for h in validation.overlap_hits
                ],
            }
        return {
            "outcome": "NEEDS_CLARIFICATION",
            "reason": validation.message_bn or "ছুটির তথ্য যাচাই করা যায়নি।",
            "rules_applied": [validation.code or "LEAVE_VALIDATION_FAILED"],
        }

    pay = str(entities.get("leave_payment_category") or "").strip().lower()
    scope = str(entities.get("day_scope") or "full").strip().lower()
    start = entities.get("start_date")
    end = entities.get("end_date") or start
    leave_type = str(entities.get("leave_type") or "").strip().lower()
    type_rule = policy.type_rule(leave_type)

    docs_required = supporting_document_needed(entities) or medical_doc_required(
        entities, policy
    )
    doc_plain = str(entities.get("document_text") or "").strip()
    waived = bool(entities.get("supporting_document_waived"))

    if docs_required and not doc_plain:
        if waived:
            pending = _pending_decision(
                entities=entities,
                validation=validation,
                policy=policy,
                reason=(
                    "কাগজপত্র এখন দেননি — **ম্যানেজার রিভিউ**-তে পাঠানো হয়েছে। "
                    "অনুমোদনের পর ছুটি চূড়ান্ত হবে।"
                ),
                rules=["LEAVE_MEDICAL_SKIPPED_MANAGER_REVIEW"],
                leave_status=LEAVE_STATUS_MANAGER_REVIEW,
                route_to="MANAGER",
            )
            return pending
        return {
            "outcome": "NEEDS_CLARIFICATION",
            "reason": (
                "এই ছুটির জন্য সাধারণত **ডাক্তারের চিট বা প্রেসক্রিপশন** লাগে। "
                "ফাইল দিন বা লেখা পেস্ট করুন; দরকার হলে **skip** লিখে ম্যানেজার রিভিউ নিন।"
            ),
            "rules_applied": ["LEAVE_MEDICAL_DOC_REQUIRED"],
        }

    bal: BalanceAnalysis | None = validation.balance
    if not bal:
        bal_days = float(crm_context.get("leave_balance_days") or 0)
        from chat.services.leave_validation import analyze_balance

        bal = analyze_balance(entities, balance_days=bal_days, policy=policy)

    ledger = bal.requested_ledger
    tier = _approval_tier(ledger, policy)
    warn_suffix = ""
    if validation.calendar_warnings:
        warn_suffix = "\n\n_" + " ".join(validation.calendar_warnings) + "_"

    balance_summary = (
        f"আবেদন: **{ledger:g}** দিন | ব্যালান্স: **{bal.balance_available:g}** দিন | "
        f"অবশিষ্ট (যদি অনুমোদিত হয়): **{max(0.0, bal.balance_available - bal.paid_portion):g}** দিন"
    )
    if bal.needs_split:
        balance_summary += (
            f"\n• বেতনসহ অংশ: **{bal.paid_portion:g}** দিন | বেতন ছাড়া: **{bal.unpaid_portion:g}** দিন"
        )

    # Unpaid / LWOP always needs approval
    if pay == LEAVE_PAYMENT_LWOP or leave_type == LEAVE_TYPE_UNPAID:
        return _pending_decision(
            entities=entities,
            validation=validation,
            policy=policy,
            reason=(
                f"{balance_summary}\n\nবেতন ছাড়া ছুটি — **ম্যানেজার অনুমোদন** প্রয়োজন।"
                + warn_suffix
            ),
            rules=["LEAVE_LWOP_MANAGER_APPROVAL"],
            leave_status=LEAVE_STATUS_MANAGER_REVIEW,
            route_to="MANAGER",
            balance=bal,
        )

    if type_rule and type_rule.requires_hr:
        return _pending_decision(
            entities=entities,
            validation=validation,
            policy=policy,
            reason=(
                f"{balance_summary}\n\nএই ধরনের ছুটিতে **HR ও ম্যানেজার** অনুমোদন লাগে।"
                + warn_suffix
            ),
            rules=["LEAVE_TYPE_REQUIRES_HR"],
            leave_status=LEAVE_STATUS_HR_REVIEW,
            route_to="HR",
            balance=bal,
            also_manager=True,
        )

    if type_rule and type_rule.requires_manager:
        return _pending_decision(
            entities=entities,
            validation=validation,
            policy=policy,
            reason=(
                f"{balance_summary}\n\nএই ধরনের ছুটিতে **ম্যানেজার অনুমোদন** লাগে।"
                + warn_suffix
            ),
            rules=["LEAVE_TYPE_REQUIRES_MANAGER"],
            leave_status=LEAVE_STATUS_MANAGER_REVIEW,
            route_to="MANAGER",
            balance=bal,
        )

    # Insufficient balance without split
    if pay == LEAVE_PAYMENT_PAID and not bal.sufficient_for_full_paid and not bal.needs_split:
        short = bal.requested_ledger - bal.balance_available
        return _pending_decision(
            entities=entities,
            validation=validation,
            policy=policy,
            reason=(
                f"{balance_summary}\n\nবেতনসহ ছুটির জন্য **{bal.requested_ledger:g}** দিন লাগছে, "
                f"ব্যালান্সে **{bal.balance_available:g}** দিন — **{short:g}** দিন কম। "
                "তারিখ কমান, বেতন ছাড়া অংশ নিন, অথবা HR-এর সাথে কথা বলুন।"
                + warn_suffix
            ),
            rules=["LEAVE_PAID_UNDERBALANCE_ROUTE_HR"],
            leave_status=LEAVE_STATUS_HR_REVIEW,
            route_to="HR",
            balance=bal,
        )

    # Split paid + unpaid portion
    if bal.needs_split:
        entities_out = dict(entities)
        entities_out["paid_leave_days"] = bal.paid_portion
        entities_out["unpaid_leave_days"] = bal.unpaid_portion
        return _pending_decision(
            entities=entities,
            validation=validation,
            policy=policy,
            reason=(
                f"{balance_summary}\n\nব্যালান্স শেষ — বেতনসহ **{bal.paid_portion:g}** দিন ও "
                f"বেতন ছাড়া **{bal.unpaid_portion:g}** দিন হিসেবে **HR/ম্যানেজার** রিভিউতে পাঠানো হয়েছে।"
                + warn_suffix
            ),
            rules=["LEAVE_SPLIT_PAID_UNPAID_PENDING"],
            leave_status=LEAVE_STATUS_HR_REVIEW,
            route_to="HR",
            balance=bal,
            also_manager=True,
            extra_entities=entities_out,
        )

    # Duration-based approval tiers
    if tier == "hr_manager":
        return _pending_decision(
            entities=entities,
            validation=validation,
            policy=policy,
            reason=(
                f"{balance_summary}\n\n**{ledger:g}** দিনের ছুটি — কোম্পানি নীতি অনুযায়ী "
                "**HR ও ম্যানেজার** অনুমোদন লাগে।"
                + warn_suffix
            ),
            rules=["LEAVE_LONG_DURATION_HR_MANAGER"],
            leave_status=LEAVE_STATUS_HR_REVIEW,
            route_to="HR",
            balance=bal,
            also_manager=True,
        )

    if tier == "manager":
        return _pending_decision(
            entities=entities,
            validation=validation,
            policy=policy,
            reason=(
                f"{balance_summary}\n\n**{ledger:g}** দিনের ছুটি — **ম্যানেজার অনুমোদন** প্রয়োজন।"
                + warn_suffix
            ),
            rules=["LEAVE_DURATION_MANAGER_APPROVAL"],
            leave_status=LEAVE_STATUS_MANAGER_REVIEW,
            route_to="MANAGER",
            balance=bal,
        )

    # Validated paid leave — submit to CRM; approval happens only in HR/PHP workflow.
    day_word = "হাফ দিন" if "half" in scope else "পুরো দিন"
    return {
        "outcome": "SUBMITTED",
        "reason": (
            f"{balance_summary}\n\n"
            f"**{ledger:g}** দিন ({day_word}), **{start}** থেকে **{end}** — "
            "আবেদনটি আপনার কোম্পানির HR সিস্টেমে জমা দেওয়া হবে; চূড়ান্ত অনুমোদন সেখানে হবে।"
            + warn_suffix
        ),
        "rules_applied": [
            "LEAVE_VALIDATED_SUBMITTED_TO_CRM",
            "LEAVE_BALANCE_SUFFICIENT",
            "LEAVE_PAYLOAD_COMPLETE",
        ],
        "leave_status": LEAVE_STATUS_PENDING,
        "route_to": "CRM_WORKFLOW",
        "requested_ledger_days": ledger,
        "balance_days": bal.balance_available,
        "remaining_balance_days": bal.remaining_after,
    }


def _pending_decision(
    *,
    entities: dict[str, Any],
    validation: LeaveValidationResult,
    policy: CompanyLeavePolicy,
    reason: str,
    rules: list[str],
    leave_status: str,
    route_to: str,
    balance: BalanceAnalysis | None = None,
    also_manager: bool = False,
    extra_entities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Chatbot never approves leave — we only record workflow hints for the CRM.
    """
    out: dict[str, Any] = {
        "outcome": "SUBMITTED",
        "reason": reason,
        "rules_applied": rules,
        "leave_status": leave_status,
        "route_to": route_to,
    }
    if balance:
        out["requested_ledger_days"] = balance.requested_ledger
        out["balance_days"] = balance.balance_available
        if balance.needs_split:
            out["paid_leave_days"] = balance.paid_portion
            out["unpaid_leave_days"] = balance.unpaid_portion
    if also_manager:
        out["approval_chain"] = ["MANAGER", "HR"]
    if extra_entities:
        out["_entity_patches"] = extra_entities
    return out
