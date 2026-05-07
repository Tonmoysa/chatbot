from typing import Any

from chat.constants import (
    INTENT_EXPENSE_STATUS,
    INTENT_HR_POLICY,
    INTENT_LEAVE_BALANCE,
    INTENT_LEAVE_REQUEST,
    INTENT_REQUEST_STATUS,
    INTENT_UNKNOWN,
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
        if st == "NOT_FOUND":
            rid = (
                (crm_payload.get("request_id") or "")
                or str(entities.get("request_id") or "")
            ).strip()
            rid_part = f" (`{rid}`)" if rid else ""
            return (
                f"I couldn't find any request{rid_part} in the system. "
                "Please double‑check the reference ID and try again.",
                "needs_input",
            )
        if crm_payload.get("detail"):
            # Avoid leaking low-quality backend phrasing like "Unknown request"
            return ("I couldn't find that request. Please re-check the reference ID.", "needs_input")
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
        if intent == INTENT_UNKNOWN:
            return (
                "Hi! আমি আপনার HR assistant. আপনি কী নিয়ে সাহায্য চান?\n"
                "উদাহরণ: leave balance, leave request, WFH request, expense claim/status, "
                "attendance correction, HR policy, বা request status.",
                "needs_input",
            )
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
        if crm_payload.get("_deduped"):
            msg = "You already submitted this expense claim earlier. No new request was created."
        else:
            msg = "Your expense claim was auto-approved under policy thresholds."
        if rid:
            msg += f" Reference: {rid}."
        return (msg, "success")

    if outcome == "APPROVED":
        rid = crm_payload.get("request_id", "")
        if crm_payload.get("_deduped") and intent == INTENT_LEAVE_REQUEST:
            msg = (
                "You already submitted this leave request earlier. "
                "No new request was created."
            )
        else:
            msg = "Your leave request is approved under current balance rules."
        if rid:
            msg += f" Reference: {rid}."
        return (msg, "success")

    if outcome == "REJECTED":
        rid = crm_payload.get("request_id", "")
        if crm_payload.get("_deduped") and intent == INTENT_LEAVE_REQUEST:
            msg = (
                "You already submitted this leave request earlier. "
                "No new request was created."
            )
        else:
            msg = reason or "The request could not be approved."
        if rid:
            msg += f" Reference: {rid}."
        return (msg, "rejected")

    if outcome in ("PENDING_APPROVAL", "PENDING_REVIEW"):
        rid = crm_payload.get("request_id", "")
        route_to = (decision or {}).get("route_to")
        if crm_payload.get("_deduped") and rid:
            msg = "You already submitted this request earlier. No new request was created."
        else:
            msg = reason or "Your request is submitted for review."
        if route_to == "HR" and "HR" not in msg:
            msg = msg.rstrip(".") + " It has been sent to HR for review."
        if rid:
            msg += f" Reference: {rid}."
        return (msg, "pending")

    return ("Request processed.", "success")
