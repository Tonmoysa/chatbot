import uuid
import re
from datetime import date
from typing import Any

from chat.constants import (
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
from chat.services.crm.base import CRMError
from chat.services.crm.factory import get_crm_adapter
from chat.services.decision_engine import DecisionEngine
from chat.services.entity_extractor import EntityExtractor
from chat.services.expense_incurred_date import infer_expense_incurred_date_iso
from chat.services.intent_detector import IntentDetector
from chat.services.leave_days import compute_requested_leave_days
from chat.services.leave_workflow import (
    deactivate_leave_session,
    is_leave_collecting,
    process_leave_turn,
)
from chat.services.memory_store import ConversationMemoryStore
from chat.services.observability import log_step
from chat.services.response_formatter import build_user_message


class ChatOrchestrator:
    """
    Central pipeline controller.
    User Input → Intent → Entities → Context merge → Decision → CRM → Formatter.
    """

    def __init__(self) -> None:
        self.memory = ConversationMemoryStore()
        self.intents = IntentDetector()
        self.entities = EntityExtractor()
        self.engine = DecisionEngine()
        self.crm = get_crm_adapter()

    def run_chat(
        self,
        *,
        message: str,
        session_id: str | None,
        employee_id: str,
        trace_id: str,
        document_text: str | None = None,
    ) -> dict[str, Any]:
        session = self.memory.get_or_create_session(session_id or "", employee_id)
        context_lines = self.memory.recent_context_lines(session)
        log_step(
            trace_id,
            "intent_detection_start",
            {"user_message": message, "session_id": session.session_id},
        )

        intent_result = self.intents.detect(message, trace_id)
        intent = intent_result["intent"]

        wf_state = getattr(session, "workflow_state", None) or {}
        low_msg = (message or "").lower()
        balance_probe = bool(
            re.search(
                r"\b(balance|remaining|left|pto|how many\s+days|ছুটি\s+কত|কত\s+দিন)\b",
                low_msg,
            )
        )
        if is_leave_collecting(wf_state):
            # Do not cancel on EXPENSE_STATUS / REQUEST_STATUS alone — short replies like
            # "paid" are often misclassified and would wipe the leave draft.
            hard_switch = intent in (
                INTENT_EXPENSE_CLAIM,
                INTENT_EXPENSE_DAY_SUMMARY,
                INTENT_WFH_REQUEST,
                INTENT_ATTENDANCE_CORRECTION,
                INTENT_APPROVAL_ESCALATION,
            )
            if hard_switch:
                session.workflow_state = deactivate_leave_session(wf_state)
                session.save(update_fields=["workflow_state", "updated_at"])
            elif intent == INTENT_LEAVE_BALANCE and balance_probe:
                pass
            else:
                intent = INTENT_LEAVE_REQUEST
                intent_result = {
                    **intent_result,
                    "intent": INTENT_LEAVE_REQUEST,
                    "source": (intent_result.get("source") or "intent") + "+leave_wizard",
                }
        else:
            forced_intent = self._infer_followup_intent(context_lines, message)
            if forced_intent:
                intent = forced_intent
                intent_result = {"intent": forced_intent, "confidence": 1.0, "source": "followup"}

        log_step(trace_id, "intent_detection_done", {"intent": intent})

        log_step(trace_id, "entity_extraction_start", {})
        entity_result = self.entities.extract(
            message, intent, context_lines, trace_id
        )
        entities = entity_result.get("entities") or {}
        if document_text:
            # Carry document text into the rule engine (LLM must not decide outcomes).
            entities["document_text"] = document_text
        log_step(trace_id, "entity_extraction_done", {"keys": list(entities.keys())})

        lv_pack: dict[str, Any] = {}
        leave_collecting_blocked = False
        if intent == INTENT_LEAVE_REQUEST:
            lv_pack = process_leave_turn(
                workflow_state=getattr(session, "workflow_state", None) or {},
                message=message,
                entities=dict(entities),
            )
            session.workflow_state = lv_pack["workflow_state"]
            session.save(update_fields=["workflow_state", "updated_at"])
            merged = lv_pack["merged_entities"] or {}
            entities.clear()
            entities.update(merged)
            leave_collecting_blocked = not bool(lv_pack.get("complete"))
        log_step(
            trace_id,
            "leave_workflow_gate",
            {"blocked": leave_collecting_blocked, "intent": intent},
        )

        crm_context: dict[str, Any] = {}
        crm_payload: dict[str, Any] = {}
        status = "success"
        request_id = ""
        decision: dict[str, Any] = {}
        msg = ""
        rstatus = ""

        try:
            if intent in (
                INTENT_LEAVE_BALANCE,
                INTENT_LEAVE_REQUEST,
                INTENT_WFH_REQUEST,
            ):
                bal = self.crm.get_leave_balance(employee_id or session.employee_id)
                crm_context.update(bal)
                if intent == INTENT_LEAVE_BALANCE:
                    crm_payload.update(bal)

            if intent in (INTENT_EXPENSE_STATUS, INTENT_REQUEST_STATUS):
                rid = entities.get("request_id")
                if rid:
                    st = self.crm.get_request_status(str(rid))
                    crm_payload.update(st)
                else:
                    crm_payload["detail"] = "Missing request_id for status lookup."

            if intent == INTENT_EXPENSE_CLAIM:
                emp_ctx = employee_id or session.employee_id
                inc_iso = (entities.get("expense_incurred_date") or "").strip() or infer_expense_incurred_date_iso(
                    message=message, hints=entities, today=date.today()
                )
                day_tot = self.crm.get_expense_day_approved_total(emp_ctx, inc_iso)
                crm_context.update(day_tot)

            if intent == INTENT_EXPENSE_DAY_SUMMARY:
                emp_ctx = employee_id or session.employee_id
                inc_iso = (entities.get("expense_incurred_date") or "").strip() or infer_expense_incurred_date_iso(
                    message=message, hints=entities, today=date.today()
                )
                breakdown = self.crm.get_expense_day_breakdown(emp_ctx, inc_iso)
                crm_payload.update(breakdown)
                crm_context.update(breakdown)

            if leave_collecting_blocked:
                decision = {
                    "outcome": "NEEDS_CLARIFICATION",
                    "reason": lv_pack.get("question")
                    or "আর একটু জানতে হবে — উপরের প্রশ্নের উত্তরটা নিচে লিখে পাঠান।",
                    "rules_applied": ["LEAVE_WORKFLOW_COLLECTING"],
                }
            else:
                decision = self.engine.evaluate(
                    intent=intent, entities=entities, crm_context=crm_context
                )
            log_step(trace_id, "decision", {"outcome": decision.get("outcome")})

            dedup_request_id = self._recent_duplicate_request_id(
                session=session,
                context_lines=context_lines,
                intent=intent,
                entities=entities,
                decision=decision,
                user_message=message,
            )

            if dedup_request_id:
                request_id = dedup_request_id
                crm_payload.update({"request_id": dedup_request_id, "_deduped": True})
            elif self._should_mutate_crm(intent, decision):
                exec_result = self.crm.create_request(
                    employee_id or session.employee_id,
                    intent,
                    entities,
                    decision,
                )
                request_id = str(exec_result.get("request_id") or "")
                crm_payload.update(exec_result)

            if (
                intent == INTENT_LEAVE_REQUEST
                and request_id
                and not crm_payload.get("_deduped")
                and status == "success"
            ):
                wf_post = getattr(session, "workflow_state", None) or {}
                wf_post["last_leave_fingerprint"] = {
                    "sig": list(self._leave_booking_signature(entities)),
                    "request_id": request_id,
                }
                session.workflow_state = wf_post
                session.save(update_fields=["workflow_state", "updated_at"])

            msg, rstatus = build_user_message(
                intent=intent,
                entities=entities,
                decision=decision,
                crm_payload=crm_payload,
            )

        except CRMError:
            log_step(trace_id, "crm_error", {"error": "CRMError"})
            status = "failed"
            decision = {
                "outcome": "ERROR",
                "reason": "CRM integration error.",
                "rules_applied": ["CRM_FAILURE"],
            }
            msg = "The HR system is temporarily unavailable. Please try again shortly."
            rstatus = "error"
        except Exception as exc:
            log_step(trace_id, "unexpected_error", {"error": type(exc).__name__})
            status = "failed"
            decision = {
                "outcome": "ERROR",
                "reason": "Unexpected processing error.",
                "rules_applied": ["UNHANDLED_EXCEPTION"],
            }
            msg = "Something went wrong processing your request."
            rstatus = "error"

        self.memory.append(session, "user", message)
        self.memory.append(session, "assistant", msg)

        return {
            "trace_id": trace_id,
            "intent": intent,
            "entities": entities,
            "decision": decision,
            "response": {
                "message": msg,
                "status": rstatus if status == "success" else "error",
                "request_id": request_id or str(crm_payload.get("request_id", "") or ""),
            },
            "status": status,
            "_session_id": session.session_id,
        }

    def _infer_followup_intent(self, context_lines: list[str], message: str) -> str | None:
        """
        Heuristic: if the assistant just asked for missing fields, treat short user replies
        (dates/days/etc) as a follow-up for the same workflow instead of re-classifying intent.
        """
        if not context_lines:
            return None
        last_assistant = ""
        for line in reversed(context_lines):
            if line.startswith("Assistant:"):
                last_assistant = line[len("Assistant:") :].strip()
                break
        if not last_assistant:
            return None

        msg = (message or "").strip()
        # if user replies with just a date-like token, it's very likely a continuation
        is_dateish = bool(
            re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", msg)
            or re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", msg)
            or re.search(r"\b\d{1,2}-\d{1,2}-\d{2,4}\b", msg)
        )

        # Leave flow follow-up (legacy copy + wizard copy)
        if (
            "Leave dates or duration required" in last_assistant
            or "ছুটি ফর্ম" in last_assistant
            or "**Step " in last_assistant
            or "Step 3 of 5" in last_assistant
        ):
            if len(msg) <= 180 or is_dateish:
                return INTENT_LEAVE_REQUEST
        return None

    def _should_mutate_crm(self, intent: str, decision: dict[str, Any]) -> bool:
        if decision.get("outcome") == "NEEDS_CLARIFICATION":
            return False
        if intent == INTENT_UNKNOWN:
            return False
        if intent in (INTENT_LEAVE_BALANCE, INTENT_HR_POLICY):
            return False
        if intent in (INTENT_EXPENSE_STATUS, INTENT_REQUEST_STATUS):
            return False
        if intent == INTENT_LEAVE_REQUEST and decision.get("outcome") in (
            "APPROVED",
            "REJECTED",
            "PENDING_APPROVAL",
        ):
            return True
        if intent == INTENT_WFH_REQUEST:
            return decision.get("outcome") == "PENDING_APPROVAL"
        if intent == INTENT_EXPENSE_CLAIM:
            return decision.get("outcome") in ("AUTO_APPROVED", "PENDING_APPROVAL")
        if intent == INTENT_ATTENDANCE_CORRECTION:
            return decision.get("outcome") == "PENDING_REVIEW"
        if intent == INTENT_APPROVAL_ESCALATION:
            return decision.get("outcome") == "PENDING_APPROVAL"
        return False

    @staticmethod
    def _norm_user_message(s: str) -> str:
        return " ".join((s or "").strip().lower().split())

    def _recent_duplicate_request_id(
        self,
        *,
        session: Any,
        context_lines: list[str],
        intent: str,
        entities: dict[str, Any],
        decision: dict[str, Any],
        user_message: str,
    ) -> str | None:
        """
        Lightweight duplicate-submission guard.
        If the user repeats the same request in the same session (common with chat UIs),
        do not create a new CRM record; return the previously created request id.
        """
        if intent == INTENT_LEAVE_REQUEST:
            if decision.get("outcome") not in (
                "APPROVED",
                "REJECTED",
                "PENDING_APPROVAL",
            ):
                return None
            cur = self._leave_booking_signature(entities)
            fp = (getattr(session, "workflow_state", None) or {}).get(
                "last_leave_fingerprint"
            ) or {}
            stored = fp.get("sig")
            if stored and len(stored) == 5:
                prev = (
                    str(stored[0]),
                    str(stored[1]),
                    float(stored[2]),
                    str(stored[3]),
                    str(stored[4]),
                )
                rid = str(fp.get("request_id") or "")
                if (
                    rid
                    and cur[0] == prev[0]
                    and cur[1] == prev[1]
                    and abs(cur[2] - prev[2]) < 1e-6
                    and cur[3] == prev[3]
                    and cur[4] == prev[4]
                ):
                    return rid
            return None

        last_ref, prior_user = self._last_reference_and_prior_user(context_lines)
        if not last_ref or not prior_user:
            return None

        if intent == INTENT_EXPENSE_CLAIM:
            if decision.get("outcome") not in ("AUTO_APPROVED", "PENDING_APPROVAL"):
                return None
            # Same calendar day + same amount can be separate line items; only dedupe
            # accidental repeat of the *same* user message (double-send / tap).
            if self._norm_user_message(user_message) != self._norm_user_message(prior_user):
                return None
            try:
                amount_val = float(entities.get("amount"))
            except Exception:
                return None
            cur_date = str(entities.get("expense_incurred_date") or "").strip()
            prev_e = self.entities.extract_rules_only(
                prior_user, intent=INTENT_EXPENSE_CLAIM
            )
            prev_date = str(prev_e.get("expense_incurred_date") or "").strip()
            if not cur_date or not prev_date or cur_date != prev_date:
                return None
            m2 = re.search(r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?(?!\d)", prior_user)
            if not m2:
                return None
            whole, frac = m2.group(1), m2.group(2) or ""
            try:
                prev_amount = float(f"{whole}.{frac}" if frac else whole)
            except Exception:
                return None
            if abs(float(prev_amount) - float(amount_val)) <= 0.01:
                return last_ref
            return None

        return None

    @staticmethod
    def _last_reference_and_prior_user(
        context_lines: list[str],
    ) -> tuple[str | None, str | None]:
        """
        Most recent Assistant message that included a Reference id, paired with the
        User message that immediately preceded that Assistant turn in time order.
        """
        lines = context_lines or []
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            if not line.startswith("Assistant:"):
                continue
            content = line[len("Assistant:") :].strip()
            m = re.search(
                r"\b(?:ref|reference)\b\s*[:#-]?\s*([A-Za-z0-9-]+)\b", content, re.I
            )
            if not m:
                continue
            ref = m.group(1)
            for j in range(i - 1, -1, -1):
                if lines[j].startswith("User:"):
                    return ref, lines[j][len("User:") :].strip()
            # If ordering is skewed (same-timestamp turns), fall back to nearest user after.
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("User:"):
                    return ref, lines[j][len("User:") :].strip()
            return ref, None
        return None, None

    @staticmethod
    def _leave_booking_signature(entities: dict[str, Any]) -> tuple[str, str, float, str, str]:
        """Paid/LWOP leave duplicate comparison using ledger totals + anchors."""
        from datetime import datetime

        start_raw = entities.get("start_date") or entities.get("date")
        end_raw = entities.get("end_date")
        start_s = ""
        end_s = ""
        if start_raw:
            try:
                start_s = str(datetime.fromisoformat(str(start_raw).split("T")[0]).date())
            except Exception:
                start_s = str(start_raw).split("T")[0]
        if end_raw:
            try:
                end_s = str(datetime.fromisoformat(str(end_raw).split("T")[0]).date())
            except Exception:
                end_s = str(end_raw).split("T")[0]
        ledger = compute_requested_leave_days(entities or {})
        pay = str((entities or {}).get("leave_payment_category") or "")
        scope = str((entities or {}).get("day_scope") or "")
        return (start_s, end_s, float(ledger), pay, scope)


def new_trace_id() -> str:
    return str(uuid.uuid4())
