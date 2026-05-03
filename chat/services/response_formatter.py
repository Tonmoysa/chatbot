from typing import Any

from chat.constants import (
    INTENT_EXPENSE_STATUS,
    INTENT_HR_POLICY,
    INTENT_LEAVE_BALANCE,
    INTENT_REQUEST_STATUS,
)


def build_user_message(
    *,
    intent: str,
    entities: dict[str, Any],
    decision: dict[str, Any],
    crm_payload: dict[str, Any],
) -> tuple[str, str]:
    """
    Returns (message, status) for response envelope.
    status is a coarse workflow status string (not HTTP).
    """
    outcome = (decision or {}).get("outcome", "")
    reason = (decision or {}).get("reason", "")

    if intent in (INTENT_EXPENSE_STATUS, INTENT_REQUEST_STATUS):
        st = crm_payload.get("status")
        if st and st != "NOT_FOUND":
            return (f"Current request status: {st}.", "success")
        if crm_payload.get("detail"):
            return (str(crm_payload["detail"]), "needs_input")
        return ("Please provide a request reference to look up status.", "needs_input")

    if intent == INTENT_LEAVE_BALANCE:
        bal = crm_payload.get("leave_balance_days")
        if bal is not None:
            return (
                f"Your current leave balance is approximately {bal} day(s).",
                "success",
            )
        return ("Leave balance is temporarily unavailable.", "degraded")

    if outcome == "NEEDS_CLARIFICATION":
        return (reason or "Could you share a bit more detail?", "needs_input")

    if outcome == "INFORMATIONAL":
        if intent == INTENT_HR_POLICY:
            topic = entities.get("policy_topic") or "general HR policy"
            return (
                f"Regarding {topic}: refer to the official employee handbook. "
                "This assistant provides guidance only; decisions follow company policy.",
                "success",
            )
        return (reason or "Here is the information you requested.", "success")

    if outcome == "ERROR":
        return (reason or "An error occurred.", "error")

    if outcome == "AUTO_APPROVED":
        rid = crm_payload.get("request_id", "")
        msg = "Your expense claim was auto-approved under policy thresholds."
        if rid:
            msg += f" Reference: {rid}."
        return (msg, "success")

    if outcome == "APPROVED":
        rid = crm_payload.get("request_id", "")
        msg = "Your leave request is approved under current balance rules."
        if rid:
            msg += f" Reference: {rid}."
        return (msg, "success")

    if outcome == "REJECTED":
        rid = crm_payload.get("request_id", "")
        msg = reason or "The request could not be approved."
        if rid:
            msg += f" Reference: {rid}."
        return (msg, "rejected")

    if outcome in ("PENDING_APPROVAL", "PENDING_REVIEW"):
        rid = crm_payload.get("request_id", "")
        msg = reason or "Your request is submitted for review."
        if rid:
            msg += f" Reference: {rid}."
        return (msg, "pending")

    return ("Request processed.", "success")
