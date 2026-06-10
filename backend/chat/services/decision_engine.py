from datetime import date
from typing import Any

from chat.constants import (
    EXPENSE_DAY_CAP_BDT,
    INTENT_APPROVAL_ESCALATION,
    INTENT_ATTENDANCE_CORRECTION,
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_EXPENSE_STATUS,
    INTENT_HR_POLICY,
    INTENT_LEAVE_BALANCE,
    INTENT_LEAVE_REQUEST,
    INTENT_REQUEST_STATUS,
    INTENT_UNKNOWN,
    INTENT_WFH_REQUEST,
)
from chat.services.expense_incurred_date import (
    expense_submit_date_block_reason,
    infer_expense_incurred_date_iso,
)
from chat.services.leave_approval import resolve_leave_decision
from chat.services.leave_policies import get_company_leave_policy
from chat.services.leave_validation import validate_leave_request
from chat.services.leave_workflow import (
    LEAVE_PAYMENT_LWOP,
    LEAVE_PAYMENT_PAID,
)


class DecisionEngine:
    """
    Rule-based source of truth. LLM must never set final approval outcomes.
    """

    EXPENSE_AUTO_THRESHOLD = float(EXPENSE_DAY_CAP_BDT)

    def evaluate(
        self,
        *,
        intent: str,
        entities: dict[str, Any],
        crm_context: dict[str, Any],
    ) -> dict[str, Any]:
        doc_text = str(entities.get("document_text") or "")
        if entities.get("document_read"):
            if not doc_text.strip():
                return {
                    "outcome": "NEEDS_CLARIFICATION",
                    "reason": "I couldn't extract readable text from that document. If it's a scanned PDF/image, OCR is required. Please upload a text-based PDF or enable OCR.",
                    "rules_applied": ["DOCUMENT_TEXT_EMPTY_OR_UNREADABLE"],
                }
            snippet = doc_text.strip()
            max_chars = 2500
            truncated = ""
            if len(snippet) > max_chars:
                snippet = snippet[:max_chars]
                truncated = "\n\n(Showing the first 2500 characters.)"
            return {
                "outcome": "INFORMATIONAL",
                "reason": f"Here's what I could read from the document:\n\n{snippet}{truncated}",
                "rules_applied": ["DOCUMENT_TEXT_EXTRACTED"],
            }

        if intent == INTENT_UNKNOWN:
            return {
                "outcome": "NEEDS_CLARIFICATION",
                "reason": "Intent could not be determined confidently.",
                "rules_applied": ["UNKNOWN_INTENT"],
            }

        if intent == INTENT_LEAVE_BALANCE:
            return {
                "outcome": "INFORMATIONAL",
                "reason": "Read-only balance query.",
                "rules_applied": ["LEAVE_BALANCE_READ_ONLY"],
            }

        if intent == INTENT_HR_POLICY:
            return {
                "outcome": "INFORMATIONAL",
                "reason": "Policy guidance (non-binding summary).",
                "rules_applied": ["HR_POLICY_INFO"],
            }

        if intent == INTENT_EXPENSE_STATUS or intent == INTENT_REQUEST_STATUS:
            return {
                "outcome": "INFORMATIONAL",
                "reason": "Status lookup via CRM.",
                "rules_applied": ["STATUS_READ_ONLY"],
            }

        if intent == INTENT_EXPENSE_DAY_SUMMARY:
            return {
                "outcome": "INFORMATIONAL",
                "reason": "Expense day summary (read-only).",
                "rules_applied": ["EXPENSE_DAY_SUMMARY_READ_ONLY"],
            }

        if intent == INTENT_EXPENSE_CLAIM:
            # Enterprise workflow submission (collect → confirm → CRM payload).
            if entities.get("expense_workflow_submit"):
                items = list(entities.get("expense_items") or [])
                if not items:
                    return {
                        "outcome": "NEEDS_CLARIFICATION",
                        "reason": "কোনো expense line পাওয়া যায়নি।",
                        "rules_applied": ["EXPENSE_WORKFLOW_EMPTY"],
                    }
                inc_iso = entities.get("expense_incurred_date") or infer_expense_incurred_date_iso(
                    message="", hints=entities, today=date.today()
                )
                date_block = expense_submit_date_block_reason(str(inc_iso))
                if date_block:
                    return {
                        "outcome": "NEEDS_CLARIFICATION",
                        "reason": date_block,
                        "rules_applied": ["EXPENSE_FUTURE_DATE_SUBMIT_LATER"],
                    }
                total = sum(float(r.get("amount") or 0) for r in items)
                return {
                    "outcome": "SUBMITTED",
                    "reason": (
                        f"Expense request queued for CRM review ({len(items)} line(s), "
                        f"total {total:g} BDT). Finance approves in HR system — not in chat."
                    ),
                    "rules_applied": ["EXPENSE_WORKFLOW_SUBMITTED"],
                    # Mock CRM day-summary still keys off AUTO_APPROVED totals until PHP API ships.
                    "_crm_outcome_hint": "AUTO_APPROVED",
                }

            # All expense claims must go through the wizard (collect → review → confirm).
            return {
                "outcome": "NEEDS_CLARIFICATION",
                "reason": (
                    "Expense claims must be collected through the expense wizard "
                    "(category, amount, review, then confirm)."
                ),
                "rules_applied": ["EXPENSE_USE_WIZARD"],
            }

        if intent == INTENT_LEAVE_REQUEST:
            """
            Enterprise leave path: tenant policy, overlap guard, balance split,
            duration-based approval tiers. LLM does not approve.

            Field-level clarification is owned by the leave wizard; once the user
            confirms the draft (``leave_workflow_confirmed``), skip duplicate checks.
            """
            if not entities.get("leave_workflow_confirmed"):
                from chat.services.leave_draft_utils import (
                    WIZARD_LEAVE_TYPES,
                    sync_payment_from_leave_type,
                )

                probe = dict(entities)
                sync_payment_from_leave_type(probe)
                entities["leave_payment_category"] = probe.get("leave_payment_category")

                lt = str(entities.get("leave_type") or "").strip().lower()
                if lt not in WIZARD_LEAVE_TYPES:
                    return {
                        "outcome": "NEEDS_CLARIFICATION",
                        "reason": (
                            "কোন ধরনের ছুটি — **sick leave**, **annual leave**, "
                            "নাকি **leave without pay**?"
                        ),
                        "rules_applied": ["LEAVE_TYPE_UNKNOWN"],
                    }

                pay = str(entities.get("leave_payment_category") or "").strip().lower()
                if pay not in (LEAVE_PAYMENT_PAID, LEAVE_PAYMENT_LWOP):
                    return {
                        "outcome": "NEEDS_CLARIFICATION",
                        "reason": (
                            "এখনও বোঝা যাচ্ছে না ছুটির ধরন। "
                            "sick / annual / unpaid লিখুন।"
                        ),
                        "rules_applied": ["LEAVE_PAYMENT_UNKNOWN"],
                    }

                scope = str(entities.get("day_scope") or "").strip().lower()
                if scope not in ("full", "half", "half_day", "half-day"):
                    return {
                        "outcome": "NEEDS_CLARIFICATION",
                        "reason": (
                            "পুরো দিন নাকি হাফ দিন — একটু স্পষ্ট করে লিখুন "
                            "(যেমন: পুরো দিন / হাফ দিন)।"
                        ),
                        "rules_applied": ["LEAVE_DAY_SCOPE_UNKNOWN"],
                    }

                days_needed = entities.get("days")
                start = entities.get("start_date")
                end = entities.get("end_date")
                if not start and not end and days_needed is None:
                    return {
                        "outcome": "NEEDS_CLARIFICATION",
                        "reason": (
                            "কোন তারিখে ছুটি চান সেটা পুরো হয়নি। এক দিন হলে একটা তারিখ দিন; "
                            "একাধিক দিন হলে শুরু আর শেষ তারিখ দিন "
                            "(যেমন 2026-05-12 থেকে 2026-05-14)।"
                        ),
                        "rules_applied": ["LEAVE_DATES_REQUIRED"],
                    }

                rs = str(entities.get("reason") or "").strip()
                if len(rs) < 4:
                    return {
                        "outcome": "NEEDS_CLARIFICATION",
                        "reason": (
                            "ছুটি **কেন** লাগছে — এক লাইনে লিখুন "
                            "(পরিবার, অসুস্থতা, ভ্রমণ ইত্যাদি)।"
                        ),
                        "rules_applied": ["LEAVE_REASON_REQUIRED"],
                    }

            company_id = str(crm_context.get("company_id") or "default")
            employee_id = str(crm_context.get("employee_id") or "")
            policy = get_company_leave_policy(company_id)
            balance = float(crm_context.get("leave_balance_days") or 0)
            existing = list(crm_context.get("existing_leave_requests") or [])

            validation = validate_leave_request(
                entities,
                policy=policy,
                balance_days=balance,
                existing_records=existing,
                company_id=company_id,
                employee_id=employee_id,
            )
            decision = resolve_leave_decision(
                entities,
                policy=policy,
                validation=validation,
                crm_context=crm_context,
            )
            patches = decision.pop("_entity_patches", None)
            if patches:
                entities.update(patches)
            for k in ("paid_leave_days", "unpaid_leave_days"):
                if entities.get(k) is not None:
                    decision[k] = entities[k]
            return decision

        if intent == INTENT_WFH_REQUEST:
            return {
                "outcome": "PENDING_APPROVAL",
                "reason": "WFH requires manager approval.",
                "rules_applied": ["WFH_PENDING_APPROVAL"],
            }

        if intent == INTENT_ATTENDANCE_CORRECTION:
            return {
                "outcome": "PENDING_REVIEW",
                "reason": "Attendance corrections require HR review.",
                "rules_applied": ["ATTENDANCE_ALWAYS_PENDING_REVIEW"],
            }

        if intent == INTENT_APPROVAL_ESCALATION:
            return {
                "outcome": "PENDING_APPROVAL",
                "reason": "Escalation ticket opened for leadership review.",
                "rules_applied": ["APPROVAL_ESCALATION_PENDING"],
            }

        return {
            "outcome": "NEEDS_CLARIFICATION",
            "reason": "No matching decision path.",
            "rules_applied": ["FALLBACK"],
        }

