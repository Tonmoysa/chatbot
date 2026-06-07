from typing import Any

from chat.constants import (
    EXPENSE_DAY_CAP_BDT,
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_EXPENSE_STATUS,
    INTENT_HR_POLICY,
    INTENT_LEAVE_BALANCE,
    INTENT_LEAVE_REQUEST,
    INTENT_REQUEST_STATUS,
    INTENT_UNKNOWN,
)
from chat.services.expense_workflow import format_expense_day_summary_readonly


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

    if intent == INTENT_EXPENSE_DAY_SUMMARY:
        if crm_payload.get("expense_history_view"):
            ledger = crm_payload.get("session_expense_ledger")
            if isinstance(ledger, dict):
                from chat.services.expense.session_action_memory import (
                    format_expense_history_message,
                )

                wf_stub = {
                    "bot_action_log": list(crm_payload.get("bot_action_log") or [])
                }
                return (
                    format_expense_history_message(ledger, wf_stub),
                    "success",
                )
        ledger = crm_payload.get("session_expense_ledger")
        if isinstance(ledger, dict):
            from chat.services.expense.session_ledger import (
                format_session_expense_ledger_message,
            )

            return (format_session_expense_ledger_message(ledger), "success")
        items = list(crm_payload.get("expense_day_items") or [])
        target = str(crm_payload.get("expense_incurred_date") or "").strip()
        ref = str(crm_payload.get("expense_summary_reference_id") or "").strip()
        if items:
            return (
                format_expense_day_summary_readonly(
                    items,
                    incurred_date_iso=target,
                    reference_id=ref,
                ),
                "success",
            )
        entries = list(crm_payload.get("expense_day_entries") or [])
        logged = crm_payload.get("expense_day_logged_total")
        if logged is None:
            logged = sum(float(e.get("amount") or 0) for e in entries)
        else:
            logged = float(logged)
        approved = float(crm_payload.get("expense_day_approved_total") or 0)
        cap = float(crm_payload.get("expense_daily_cap_bdt") or EXPENSE_DAY_CAP_BDT)
        remaining = max(0.0, cap - approved)
        if not entries and logged <= 0:
            return (
                format_expense_day_summary_readonly([], incurred_date_iso=target),
                "success",
            )
        lines: list[str] = []
        for e in entries:
            rid = str(e.get("request_id") or "")
            amt = float(e.get("amount") or 0)
            oc = str(e.get("outcome") or "")
            st = str(e.get("status") or "")
            tail = f" ({st})" if st else ""
            lines.append(f"• {rid} — {amt:g} BDT — {oc}{tail}")
        head = (
            f"For **{target or 'that day'}** you have **{logged:g}** BDT logged across "
            f"**{len(entries)}** expense line(s).\n"
            f"Your **{cap:g}** BDT same-day auto-approve budget already has **{approved:g}** BDT used — "
            f"**{remaining:g}** BDT remaining."
        )
        if lines:
            return (head + "\n\nLines:\n" + "\n".join(lines), "success")
        return (
            head + "\n\nNo individual lines were returned; totals above are from the HR system.",
            "success",
        )

    if intent in (INTENT_EXPENSE_STATUS, INTENT_REQUEST_STATUS):
        total_chk = str(crm_payload.get("expense_total_check") or "").strip()
        if total_chk:
            return (total_chk, "success")
        meta = str(crm_payload.get("expense_meta_answer") or "").strip()
        if meta:
            return (meta, "success")
        leave_last = crm_payload.get("leave_last_submission") or {}
        leave_ref = str(leave_last.get("submission_id") or "").strip()
        if leave_ref:
            draft = dict(leave_last.get("draft") or {})
            lt = str(draft.get("leave_type") or "").strip()
            start = str(draft.get("start_date") or "").strip()
            detail = f" · {lt}" if lt else ""
            date_part = f" · {start}" if start else ""
            return (
                f"হ্যাঁ — এই চ্যাট সেশনে আপনার leave request **জমা হয়েছে**।\n"
                f"- **রেফারেন্স:** `{leave_ref}`{detail}{date_part}\n\n"
                "চূড়ান্ত অনুমোদন আপনার কোম্পানির HR সিস্টেমে হবে।",
                "success",
            )
        if crm_payload.get("leave_not_submitted"):
            return (
                "না — এই চ্যাট সেশনে এখনো কোনো leave request **জমা হয়নি**।\n"
                "ছুটি নিতে চাইলে তারিখ ও কারণ লিখে আবার শুরু করুন।",
                "needs_input",
            )
        last = crm_payload.get("expense_last_submission") or {}
        ref = str(last.get("reference_id") or "").strip()
        if ref:
            inc = str(last.get("incurred_date_iso") or "").strip()
            items = list(last.get("items") or [])
            total = sum(float(x.get("amount") or 0) for x in items)
            return (
                f"হ্যাঁ — এই চ্যাট সেশনে আপনার expense **জমা হয়েছে**।\n"
                f"- **রেফারেন্স:** `{ref}`\n"
                f"- **তারিখ:** {inc or 'আজ'}\n"
                f"- **লাইন:** {len(items)} টি · **মোট:** {total:g} Tk\n\n"
                "চূড়ান্ত অনুমোদন CRM/Finance-এ হবে।",
                "success",
            )
        if crm_payload.get("expense_submit_blocked"):
            return (
                "না — **এখনো CRM-এ জমা হয়নি**।\n"
                f"{crm_payload.get('expense_submit_blocked')}\n\n"
                "আজকের খরচ হলে তারিখ **ajke** দিয়ে আবার লিখুন; নাহলে ওই দিনে/পরে submit করুন।",
                "needs_input",
            )
        if crm_payload.get("expense_wizard_active"):
            stage = str(crm_payload.get("expense_wizard_stage") or "")
            if stage == "submit_confirm":
                return (
                    "না — **এখনো জমা হয়নি**। উপরের প্রশ্নে **Yes** লিখে CRM-এ submit করুন।",
                    "needs_input",
                )
            return (
                "না — **এখনো জমা হয়নি**। খরচের লাইন শেষ করে summary দেখে confirm করুন।",
                "needs_input",
            )
        if crm_payload.get("expense_not_submitted"):
            return (
                "না — এই চ্যাট সেশনে এখনো কোনো expense **CRM-এ জমা হয়নি**।\n"
                "আজকের খরচ লিখতে পারেন (যেমন: lunch 100, bus 50 office to home)।",
                "needs_input",
            )
        st = crm_payload.get("status")
        if st and st != "NOT_FOUND":
            rid = (
                str(crm_payload.get("request_id") or "")
                or str(entities.get("request_id") or "")
            ).strip()
            intent_hint = str(crm_payload.get("intent") or "").strip()
            extra = ""
            if rid:
                extra = f"**`{rid}`**"
                if intent_hint:
                    extra += f" ({intent_hint})"
                return (f"Request {extra} status: **{st}**.", "success")
            return (f"Current request status: **{st}**.", "success")
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
                reason
                or "আপনার বার্তাটা একটু স্পষ্ট করবেন? ছুটি, খরচ, attendance বা "
                "কোম্পানির নীতি — যেটা লাগে সেটা লিখুন।",
                "needs_input",
            )
        return (reason or "Could you share a bit more detail?", "needs_input")

    if outcome == "INFORMATIONAL":
        if intent == INTENT_HR_POLICY:
            rules_answer = (crm_payload.get("rules_answer") or "").strip()
            if rules_answer:
                # Footer (hint to the user) is appended by the orchestrator
                # *after* any language translation, so the hint always matches
                # the reply language.
                return (rules_answer, "success")
            topic = entities.get("policy_topic") or "general HR policy"
            return (
                f"Regarding {topic}: refer to the official employee handbook. "
                "This assistant provides guidance only; decisions follow company policy.",
                "success",
            )
        return (reason or "Here is the information you requested.", "success")

    if outcome == "ERROR":
        return (reason or "An error occurred.", "error")

    if outcome == "SUBMITTED" and intent == INTENT_EXPENSE_CLAIM:
        sub = crm_payload.get("expense_submission") or {}
        ref = str(sub.get("reference_id") or crm_payload.get("request_id") or "").strip()
        msg = "আপনার expense request submit করা হয়েছে।"
        if ref:
            msg += f"\nReference ID: **{ref}**"
        msg += (
            "\n\nচূড়ান্ত অনুমোদন/প্রতিদান আপনার কোম্পানির CRM/Finance সিস্টেমে হবে — "
            "এই চ্যাটবট শুধু জমা নেয়।"
        )
        return (msg, "success")

    if outcome == "AUTO_APPROVED":
        # LEGACY / OBSOLETE: single-line auto-approve copy (orchestrator uses SUBMITTED now).
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
            msg = "এই ছুটির আবেদন আগেই জমা হয়েছে — নতুন আবেদন আর তৈরি হয়নি।"
        else:
            msg = "আপনার ছুটির আবেদন অনুমোদন হয়েছে। ব্যালান্স ঠিক আছে।"
        if rid:
            msg += f" ট্র্যাকিং নম্বর: {rid}।"
        return (msg, "success")

    if outcome == "SUBMITTED" and intent == INTENT_LEAVE_REQUEST:
        # Structured card is built in message_polish.polish_outbound_message (orchestrator).
        rid = str(crm_payload.get("request_id") or "").strip()
        sub_ref = str((crm_payload.get("leave_submission") or {}).get("reference_id") or "").strip()
        ref = rid or sub_ref
        stub = (decision or {}).get("reason") or "Leave request submitted."
        if ref and ref not in stub:
            stub = f"{stub}\n\nReference: {ref}"
        return (stub, "success")

    if outcome == "REJECTED":
        rid = crm_payload.get("request_id", "")
        if crm_payload.get("_deduped") and intent == INTENT_LEAVE_REQUEST:
            msg = "এই ছুটির আবেদন আগেই জমা হয়েছে — নতুন আবেদন আর তৈরি হয়নি।"
        else:
            msg = reason or "The request could not be approved."
        if rid:
            msg += f" ট্র্যাকিং নম্বর: {rid}।"
        return (msg, "rejected")

    if outcome in ("PENDING_APPROVAL", "PENDING_REVIEW"):
        rid = crm_payload.get("request_id", "")
        route_to = (decision or {}).get("route_to")
        if crm_payload.get("_deduped") and rid:
            msg = "You already submitted this request earlier. No new request was created."
        else:
            msg = reason or "Your request is submitted for review."
        if route_to == "HR" and "HR" not in msg and "এইচআর" not in msg:
            msg = msg.rstrip("।.") + "। HR টিম একবার দেখবে।"
        if route_to == "MANAGER" and "manager" not in msg.lower() and "ম্যানেজার" not in msg:
            msg = msg.rstrip("।.") + "। ম্যানেজার বা HR-এর সাথে একবার কথা বলে নিন।"
        if rid:
            msg += f" ট্র্যাকিং নম্বর: {rid}।"
        return (msg, "pending")

    return ("Request processed.", "success")
