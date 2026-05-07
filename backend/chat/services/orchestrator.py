import uuid
import re
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
from chat.services.crm.base import CRMError
from chat.services.crm.factory import get_crm_adapter
from chat.services.decision_engine import DecisionEngine
from chat.services.entity_extractor import EntityExtractor
from chat.services.intent_detector import IntentDetector
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

        forced_intent = self._infer_followup_intent(context_lines, message)
        if forced_intent:
            intent_result = {"intent": forced_intent, "confidence": 1.0, "source": "followup"}
        else:
            intent_result = self.intents.detect(message, trace_id)
        intent = intent_result["intent"]
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

            decision = self.engine.evaluate(
                intent=intent, entities=entities, crm_context=crm_context
            )
            log_step(trace_id, "decision", {"outcome": decision.get("outcome")})

            dedup_request_id = self._recent_duplicate_request_id(
                context_lines=context_lines,
                intent=intent,
                entities=entities,
                decision=decision,
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

        # Leave flow follow-up
        if "Leave dates or duration required" in last_assistant:
            if len(msg) <= 60 or is_dateish:
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
        if intent == INTENT_LEAVE_REQUEST and decision.get("outcome") == "REJECTED":
            return True
        if intent == INTENT_LEAVE_REQUEST and decision.get("outcome") == "APPROVED":
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

    def _recent_duplicate_request_id(
        self,
        *,
        context_lines: list[str],
        intent: str,
        entities: dict[str, Any],
        decision: dict[str, Any],
    ) -> str | None:
        """
        Lightweight duplicate-submission guard.
        If the user repeats the same request in the same session (common with chat UIs),
        do not create a new CRM record; return the previously created request id.
        """
        last_ref, prior_user = self._last_reference_and_prior_user(context_lines)
        if not last_ref or not prior_user:
            return None

        if intent == INTENT_EXPENSE_CLAIM:
            if decision.get("outcome") not in ("AUTO_APPROVED", "PENDING_APPROVAL"):
                return None
            try:
                amount_val = float(entities.get("amount"))
            except Exception:
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

        if intent == INTENT_LEAVE_REQUEST:
            if decision.get("outcome") not in ("APPROVED", "REJECTED"):
                return None
            cur = self._leave_booking_signature(entities)
            prev_e = self.entities.extract_rules_only(prior_user)
            prev = self._leave_booking_signature(prev_e)
            if (
                cur[0] == prev[0]
                and cur[1] == prev[1]
                and abs(cur[2] - prev[2]) < 1e-6
            ):
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
            m = re.search(r"\bReference:\s*([A-Z0-9-]+)\b", content)
            if not m:
                continue
            ref = m.group(1)
            for j in range(i - 1, -1, -1):
                if lines[j].startswith("User:"):
                    return ref, lines[j][len("User:") :].strip()
            return ref, None
        return None, None

    @staticmethod
    def _leave_booking_signature(entities: dict[str, Any]) -> tuple[str, str, float]:
        """Match DecisionEngine.LEAVE_REQUEST day span for duplicate comparison."""
        from datetime import datetime

        days_needed = entities.get("days")
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
        requested = float(days_needed or 1)
        if start_s and end_s:
            try:
                s_d = datetime.fromisoformat(start_s).date()
                e_d = datetime.fromisoformat(end_s).date()
                requested = max(1.0, float((e_d - s_d).days + 1))
            except Exception:
                requested = float(days_needed or 1)
        elif start_s and not end_s:
            requested = float(days_needed or 1)
        return (start_s, end_s, requested)


def new_trace_id() -> str:
    return str(uuid.uuid4())
