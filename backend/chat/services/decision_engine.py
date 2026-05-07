import re
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
from chat.services.leave_days import compute_requested_leave_days

_AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?(?!\d)")


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
            doc_text = str(entities.get("document_text") or "")
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
            # If user claims an amount but uploads a receipt, try to validate basic consistency.
            doc_amount = _extract_reasonable_amount(doc_text) if doc_text else None
            is_uber = bool(re.search(r"\buber\b", doc_text, re.I)) if doc_text else False
            if doc_text and doc_amount is not None:
                if abs(float(doc_amount) - float(val)) > 5.0:
                    return {
                        "outcome": "PENDING_APPROVAL",
                        "reason": f"Receipt amount ({doc_amount}) does not match claimed amount ({val}); routed to HR for review.",
                        "rules_applied": ["EXPENSE_RECEIPT_AMOUNT_MISMATCH_PENDING_HR"],
                        "route_to": "HR",
                    }
            if val > self.EXPENSE_AUTO_THRESHOLD:
                if not doc_text:
                    return {
                        "outcome": "NEEDS_CLARIFICATION",
                        "reason": "Amount exceeds auto-approve limit. Please upload a receipt/document for HR review.",
                        "rules_applied": ["EXPENSE_GT_THRESHOLD_RECEIPT_REQUIRED"],
                    }
                return {
                    "outcome": "PENDING_APPROVAL",
                    "reason": f"Amount {val} exceeds auto-approve threshold {self.EXPENSE_AUTO_THRESHOLD}; sent to HR for approval.",
                    "rules_applied": ["EXPENSE_GT_THRESHOLD_PENDING", "EXPENSE_ROUTED_TO_HR"],
                    "route_to": "HR",
                    "receipt": {"merchant_hint": "UBER" if is_uber else None, "doc_amount": doc_amount},
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
            requested = compute_requested_leave_days(entities)
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


def _extract_reasonable_amount(text: str) -> float | None:
    """
    Heuristic: pick the largest 1-6 digit amount found, ignoring tiny numbers.
    This is intentionally conservative and only used for basic mismatch detection.
    """
    if not text:
        return None
    candidates: list[float] = []
    for m in _AMOUNT_RE.finditer(text):
        whole = m.group(1)
        frac = m.group(2) or ""
        try:
            n = float(f"{whole}.{frac}" if frac else whole)
        except Exception:
            continue
        if 10 <= n <= 500_000:
            candidates.append(n)
    if not candidates:
        return None
    return float(max(candidates))
