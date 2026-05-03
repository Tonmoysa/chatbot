from datetime import datetime
from typing import Any

from chat.constants import (
    INTENT_APPROVAL_ESCALATION,
    INTENT_ATTENDANCE_CORRECTION,
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_STATUS,
    INTENT_HR_POLICY,
    INTENT_LEAVE_BALANCE,
    INTENT_LEAVE_REQUEST,
    INTENT_REQUEST_STATUS,
    INTENT_UNKNOWN,
    INTENT_WFH_REQUEST,
)


class DecisionEngine:
    """
    Rule-based source of truth. LLM must never set final approval outcomes.
    """

    EXPENSE_AUTO_THRESHOLD = 300.0

    def evaluate(
        self,
        *,
        intent: str,
        entities: dict[str, Any],
        crm_context: dict[str, Any],
    ) -> dict[str, Any]:
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

        if intent == INTENT_EXPENSE_CLAIM:
            amount = entities.get("amount")
            if amount is None:
                return {
                    "outcome": "NEEDS_CLARIFICATION",
                    "reason": "Expense amount is required for routing.",
                    "rules_applied": ["EXPENSE_AMOUNT_REQUIRED"],
                }
            try:
                val = float(amount)
            except (TypeError, ValueError):
                return {
                    "outcome": "NEEDS_CLARIFICATION",
                    "reason": "Expense amount is invalid.",
                    "rules_applied": ["EXPENSE_AMOUNT_INVALID"],
                }
            if val > self.EXPENSE_AUTO_THRESHOLD:
                return {
                    "outcome": "PENDING_APPROVAL",
                    "reason": f"Amount {val} exceeds auto-approve threshold {self.EXPENSE_AUTO_THRESHOLD}.",
                    "rules_applied": ["EXPENSE_GT_THRESHOLD_PENDING"],
                }
            return {
                "outcome": "AUTO_APPROVED",
                "reason": f"Amount {val} within auto-approve threshold.",
                "rules_applied": ["EXPENSE_LTE_THRESHOLD_AUTO"],
            }

        if intent == INTENT_LEAVE_REQUEST:
            days_needed = entities.get("days")
            start = entities.get("start_date")
            end = entities.get("end_date")
            if not start and not end and days_needed is None:
                return {
                    "outcome": "NEEDS_CLARIFICATION",
                    "reason": "Leave dates or duration required.",
                    "rules_applied": ["LEAVE_DATES_REQUIRED"],
                }
            balance = float(crm_context.get("leave_balance_days") or 0)
            requested = float(days_needed or 1)
            if start and end:
                try:
                    s = datetime.fromisoformat(str(start)).date()
                    e = datetime.fromisoformat(str(end)).date()
                    requested = max(1.0, float((e - s).days + 1))
                except Exception:
                    requested = float(days_needed or 1)
            if balance >= requested:
                return {
                    "outcome": "APPROVED",
                    "reason": "Sufficient leave balance per rule engine.",
                    "rules_applied": ["LEAVE_BALANCE_SUFFICIENT"],
                }
            return {
                "outcome": "REJECTED",
                "reason": "Insufficient leave balance per rule engine.",
                "rules_applied": ["LEAVE_BALANCE_INSUFFICIENT"],
            }

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
