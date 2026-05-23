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
from chat.services.intent_detector import (
    IntentDetector,
    _is_cancel_form_request,
    _is_fresh_start_greeting,
    _looks_like_chitchat,
    _message_answers_wizard_step,
    _strong_hr_policy,
)
from chat.services.leave_days import compute_requested_leave_days
from chat.services.leave_confirm import (
    _looks_like_slot_correction,
    is_confirmation_cancel,
    is_confirmation_yes,
    is_awaiting_leave_confirmation,
    parse_edit_slot,
)
from chat.services.leave_draft_sync_service import LeaveDraftSyncService
from chat.services.leave_fsm import is_leave_submission_locked, read_leave_state
from chat.services.leave_submission_service import LeaveSubmissionService
from chat.services.leave_workflow import (
    deactivate_leave_session,
    is_leave_collecting,
    is_leave_in_progress,
    is_leave_paused,
    pause_leave_session,
    pending_question,
    pending_step,
    process_leave_turn,
    resume_leave_session,
)
from chat.services.expense_workflow import (
    deactivate_expense_session,
    is_expense_collecting,
    is_expense_in_progress,
    is_expense_paused,
    pause_expense_session,
    process_expense_turn,
    resume_expense_session,
    save_expense_last_submission,
)
from chat.services.conversational import conversational_reply
from chat.services.memory_store import ConversationMemoryStore
from chat.services.observability import log_step
from chat.services.message_polish import polish_outbound_message
from chat.services.response_formatter import build_user_message
# from chat.services.rules_handbook import (
#     answer_rules_query,
#     is_rules_query,
#     wants_full_handbook,
# )  # disabled — policy text comes only from knowledge-base RAG
from chat.services.policy_intent_helpers import is_rules_query
from chat.services.translator import (
    detect_user_language,
    is_translation_request,
    translate_text,
)
from knowledge_base.services.rag_pipeline import (
    hr_policy_not_found_message,
    try_hr_policy_rag,
)


_RULES_FOOTER_EN_SECTION = (
    "_(Answers come from your uploaded policies; ask using the policy title or topic.)_"
)
_RULES_FOOTER_EN_FULL = (
    "_(Ask about a specific policy by name or section — e.g. \"attendance policy\" or "
    "\"leave policy\" — so retrieval can match your knowledge base.)_"
)
_RULES_FOOTER_BN_SECTION = (
    "_(উত্তর আপনার আপলোড করা পলিসি থেকে আসে; পলিসির নাম বা বিষয় লিখে জিজ্ঞাসা করুন।)_"
)
_RULES_FOOTER_BN_FULL = (
    "_(নির্দিষ্ট পলিসির নাম বা বিষয় লিখে জিজ্ঞাসা করুন — যেমন \"উপস্থিতি পলিসি\" বা "
    "\"ছুটির পলিসি\" — যাতে নলেজ বেসে মিলে।)_"
)


def _is_policy_interrupt_message(message: str) -> bool:
    """True when the user is asking about HR policy/rules (not filling leave slots)."""
    return bool(_strong_hr_policy(message) and is_rules_query(message))


def _should_short_circuit_submitted_leave(message: str) -> bool:
    """
    After a leave is submitted+locked, only block repeat submit / new leave attempts.
    Policy, balance, and general chat must reach the normal pipeline.
    """
    if _is_policy_interrupt_message(message) or _leave_balance_probe(message):
        return False
    if _looks_like_chitchat(message, strict=True) or _is_fresh_start_greeting(message):
        return False
    if is_confirmation_yes(message) or is_confirmation_cancel(message):
        return True
    low = (message or "").lower()
    # New leave phrasing after a prior submit starts a fresh wizard — do not short-circuit here.
    if re.search(
        r"(ছুটি|chuti|chhuti|leave).*(চাই|lagbe|lage|apply|request|নিতে|লাগবে)",
        low,
    ):
        return False
    if re.search(r"\b(submit|confirm)\b.*\bleave\b", low):
        return True
    return False


def _leave_balance_probe(message: str) -> bool:
    low_msg = (message or "").lower()
    return bool(
        re.search(
            r"\b(balance|remaining|left|pto|how\s+many\s+days|vacation\s+left|baki|baaki)\b",
            low_msg,
        )
        or re.search(r"(ছুটি\s*কত|কত\s*দিন|কয়\s*দিন|কতদিন|কয়দিন)", message or "")
        or re.search(r"\b(koto|koy|kon)\s*din\b", low_msg)
        or re.search(r"\b(kotodin|koydin|kondin)\b", low_msg)
    )


def _detect_intent_during_leave_workflow(
    message: str,
    workflow_state: dict[str, Any],
    *,
    balance_probe: bool,
) -> dict[str, Any]:
    """
    Deterministic intent while leave_request draft exists — LLM must not override.
    """
    if is_awaiting_leave_confirmation(workflow_state):
        if (
            is_confirmation_cancel(message)
            or parse_edit_slot(message)
            or is_confirmation_yes(message)
        ):
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm",
            }
        if balance_probe:
            return {
                "intent": INTENT_LEAVE_BALANCE,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_balance",
            }
        if _is_policy_interrupt_message(message):
            return {
                "intent": INTENT_HR_POLICY,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_policy",
            }
        if _looks_like_chitchat(message, strict=True) or _is_fresh_start_greeting(message):
            return {
                "intent": INTENT_UNKNOWN,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_interrupt",
            }
        if _looks_like_slot_correction(message):
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_patch",
            }
        return {
            "intent": INTENT_UNKNOWN,
            "confidence": 0.99,
            "source": "leave_workflow_gate+confirm_interrupt",
        }
    if balance_probe:
        return {
            "intent": INTENT_LEAVE_BALANCE,
            "confidence": 0.99,
            "source": "leave_workflow_gate+balance",
        }
    if _looks_like_chitchat(message, strict=True) or _is_fresh_start_greeting(message):
        return {
            "intent": INTENT_UNKNOWN,
            "confidence": 0.99,
            "source": "leave_workflow_gate+interrupt",
        }
    if _is_policy_interrupt_message(message):
        return {
            "intent": INTENT_HR_POLICY,
            "confidence": 0.99,
            "source": "leave_workflow_gate+policy",
        }
    if _message_answers_wizard_step(
        message, pending_step(workflow_state)
    ) or _canonical_leave_wizard_token(message):
        return {
            "intent": INTENT_LEAVE_REQUEST,
            "confidence": 0.99,
            "source": "leave_workflow_gate+slot",
        }
    return {
        "intent": INTENT_UNKNOWN,
        "confidence": 0.99,
        "source": "leave_workflow_gate+interrupt",
    }


def _append_leave_workflow_resume(message: str, workflow_state: dict[str, Any]) -> str:
    resume = pending_question(workflow_state)
    if not resume:
        return message
    return message.rstrip() + "\n\n" + resume


def _canonical_leave_wizard_token(message: str) -> bool:
    """
    Short replies that should stay in the leave wizard even when the step
    guard or LLM intent mis-fires (e.g. "paid" while dates are pending).
    """
    t = (message or "").strip().lower()
    if not t or len(t) > 48:
        return False
    if re.match(
        r"^(paid|unpaid|lwop|full|half|sick|casual|annual|emergency|maternity|paternity)(\s+day)?s?$",
        t,
    ):
        return True
    if re.match(r"^(full|half)\s*day$", t):
        return True
    return bool(re.search(r"(বেতনসহ|বেতন\s*ছাড়া)", message or ""))


def _rules_footer(*, mode: str, lang: str) -> str:
    if lang == "bn":
        return _RULES_FOOTER_BN_SECTION if mode == "section" else _RULES_FOOTER_BN_FULL
    return _RULES_FOOTER_EN_SECTION if mode == "section" else _RULES_FOOTER_EN_FULL


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
        company_id: str,
        employee_id: str,
        trace_id: str,
        document_text: str | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        session = self.memory.get_or_create_session(
            company_id=company_id,
            employee_id=employee_id,
            session_id=session_id or "",
        )
        context_lines = self.memory.recent_context_lines(session)

        # Translation follow-up — if the user is asking to translate the previous
        # assistant turn, do that directly so the message never falls into the
        # generic "I didn't understand" greeting. Workflow state is preserved
        # so any in-progress wizard simply resumes on the next turn.
        translate_to = is_translation_request(message)
        if translate_to:
            prev_assistant = self._last_assistant_text(context_lines)
            if prev_assistant:
                log_step(
                    trace_id,
                    "translation_request",
                    {"target_lang": translate_to, "source_chars": len(prev_assistant)},
                )
                translated, ok = translate_text(
                    prev_assistant,
                    target_lang=translate_to,
                    trace_id=trace_id,
                )
                if ok:
                    msg = translated
                    status_str = "success"
                else:
                    msg = (
                        "এই মুহূর্তে অনুবাদ চালু হচ্ছে না — একটু পরে আবার চেষ্টা করুন। "
                        "নিচে আগের উত্তরটি আবার পাঠানো হলো:\n\n" + prev_assistant
                        if translate_to == "bn"
                        else (
                            "Translation is briefly unavailable — please try again. "
                            "Re-posting the previous answer below:\n\n" + prev_assistant
                        )
                    )
                    status_str = "degraded"
                self.memory.append(session, "user", message)
                self.memory.append(session, "assistant", msg)
                return {
                    "trace_id": trace_id,
                    "intent": "HR_POLICY",
                    "entities": {"translation_target_lang": translate_to},
                    "decision": {
                        "outcome": "INFORMATIONAL",
                        "reason": "Translated the previous assistant turn.",
                        "rules_applied": ["TRANSLATION_FOLLOWUP"],
                    },
                    "response": {
                        "message": msg,
                        "status": status_str,
                        "request_id": "",
                    },
                    "status": "success",
                    "_session_id": session.session_id,
                }

        wf_state = getattr(session, "workflow_state", None) or {}
        if is_leave_submission_locked(wf_state) and _should_short_circuit_submitted_leave(
            message
        ):
            st_locked = read_leave_state(wf_state)
            dedup_rid = str(st_locked.get("submission_id") or "")
            if dedup_rid:
                msg_locked = (
                    "This leave request was already submitted. "
                    f"Reference: **{dedup_rid}**."
                )
            else:
                msg_locked = "This leave request was already submitted."
            self.memory.append(session, "user", message)
            self.memory.append(session, "assistant", msg_locked)
            return {
                "trace_id": trace_id,
                "intent": INTENT_LEAVE_REQUEST,
                "entities": {},
                "decision": {
                    "outcome": "SUBMITTED",
                    "reason": msg_locked,
                    "rules_applied": ["LEAVE_SUBMISSION_LOCKED"],
                },
                "response": {
                    "message": msg_locked,
                    "status": "success",
                    "request_id": dedup_rid,
                },
                "status": "success",
                "_session_id": session.session_id,
            }

        balance_probe = _leave_balance_probe(message)
        is_greeting_now = _is_fresh_start_greeting(message)
        is_cancel_now = _is_cancel_form_request(message)
        wizard_dismissed_reason: str | None = None
        leave_side_interrupt = False
        leave_workflow_interrupt = False
        response_finalized = False

        # Resume paused leave on any continuation except another explicit policy lookup.
        if is_leave_paused(wf_state) and not is_cancel_now:
            policy_interrupt = _is_policy_interrupt_message(message)
            if _wants_resume_leave(message) or not policy_interrupt:
                session.workflow_state = resume_leave_session(wf_state)
                session.save(update_fields=["workflow_state", "updated_at"])
                wf_state = session.workflow_state or {}
                log_step(trace_id, "leave_wizard_auto_resumed", {})

        if is_expense_paused(wf_state) and not is_cancel_now:
            policy_interrupt = _is_policy_interrupt_message(message)
            if _wants_resume_expense(message) or not policy_interrupt:
                session.workflow_state = resume_expense_session(wf_state)
                session.save(update_fields=["workflow_state", "updated_at"])
                wf_state = session.workflow_state or {}
                log_step(trace_id, "expense_wizard_auto_resumed", {})

        log_step(
            trace_id,
            "intent_detection_start",
            {"user_message": message, "session_id": session.session_id},
        )

        if is_leave_in_progress(wf_state):
            intent_result = _detect_intent_during_leave_workflow(
                message, wf_state, balance_probe=balance_probe
            )
            log_step(trace_id, "intent_detection_skipped_leave_active", intent_result)
        else:
            intent_result = self.intents.detect(message, trace_id)
        intent = intent_result["intent"]

        # Explicit cancel drops the leave draft; greetings keep the draft alive.
        if is_leave_in_progress(wf_state) and is_cancel_now:
            wf_state = deactivate_leave_session(wf_state)
            session.workflow_state = wf_state
            session.save(update_fields=["workflow_state", "updated_at"])
            wf_state = session.workflow_state or {}
            wizard_dismissed_reason = "cancel"
            intent = INTENT_UNKNOWN
            intent_result = {
                **intent_result,
                "intent": INTENT_UNKNOWN,
                "source": (intent_result.get("source") or "intent")
                + "+wizard_dismissed_cancel",
            }
            log_step(trace_id, "leave_wizard_dismissed", {"reason": "cancel"})
        elif is_expense_in_progress(wf_state) and (is_greeting_now or is_cancel_now):
            if is_expense_in_progress(wf_state):
                wf_state = deactivate_expense_session(wf_state)
            session.workflow_state = wf_state
            session.save(update_fields=["workflow_state", "updated_at"])
            wf_state = session.workflow_state or {}
            wizard_dismissed_reason = "greeting" if is_greeting_now else "cancel"
            intent = INTENT_UNKNOWN
            intent_result = {
                **intent_result,
                "intent": INTENT_UNKNOWN,
                "source": (intent_result.get("source") or "intent")
                + f"+wizard_dismissed_{wizard_dismissed_reason}",
            }

        if is_leave_in_progress(wf_state):
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
                wf_state = session.workflow_state or {}
            elif intent == INTENT_HR_POLICY:
                if is_leave_collecting(wf_state) or is_awaiting_leave_confirmation(
                    wf_state
                ):
                    session.workflow_state = pause_leave_session(wf_state)
                    session.save(update_fields=["workflow_state", "updated_at"])
                    wf_state = session.workflow_state or {}
                    log_step(trace_id, "leave_wizard_paused_for_policy", {})
                leave_workflow_interrupt = True
            elif intent == INTENT_LEAVE_BALANCE:
                leave_workflow_interrupt = True
            elif intent == INTENT_UNKNOWN:
                leave_side_interrupt = True
                if not is_awaiting_leave_confirmation(wf_state) and not (
                    _looks_like_chitchat(message, strict=True)
                    or _is_fresh_start_greeting(message)
                ):
                    leave_workflow_interrupt = True
            elif is_awaiting_leave_confirmation(wf_state):
                leave_side_interrupt = _looks_like_chitchat(message, strict=True) or (
                    _is_fresh_start_greeting(message)
                )
        elif is_expense_in_progress(wf_state):
            if intent in (
                INTENT_LEAVE_REQUEST,
                INTENT_WFH_REQUEST,
                INTENT_ATTENDANCE_CORRECTION,
                INTENT_APPROVAL_ESCALATION,
            ):
                session.workflow_state = deactivate_expense_session(wf_state)
                session.save(update_fields=["workflow_state", "updated_at"])
                wf_state = session.workflow_state or {}
            elif intent in (INTENT_EXPENSE_DAY_SUMMARY, INTENT_EXPENSE_STATUS):
                pass
            elif _looks_like_chitchat(message, strict=True):
                intent = INTENT_EXPENSE_CLAIM
                intent_result = {
                    **intent_result,
                    "intent": INTENT_EXPENSE_CLAIM,
                    "source": (intent_result.get("source") or "intent") + "+chitchat_in_expense_wizard",
                }
            elif _is_policy_interrupt_message(message):
                if not is_expense_paused(wf_state):
                    session.workflow_state = pause_expense_session(wf_state)
                    session.save(update_fields=["workflow_state", "updated_at"])
                    wf_state = session.workflow_state or {}
                    log_step(trace_id, "expense_wizard_paused_for_policy", {})
                intent = INTENT_HR_POLICY
                intent_result = {
                    **intent_result,
                    "intent": INTENT_HR_POLICY,
                    "source": (intent_result.get("source") or "intent") + "+policy_pause_expense_wizard",
                }
            else:
                intent = INTENT_EXPENSE_CLAIM
                intent_result = {
                    **intent_result,
                    "intent": INTENT_EXPENSE_CLAIM,
                    "source": (intent_result.get("source") or "intent") + "+expense_wizard",
                }
        else:
            # Skip follow-up inference when the user just dismissed the wizard
            # (greeting / cancel) — otherwise the recent assistant turn (which
            # contained "ছুটি ফর্ম" or "Step …") would drag the intent back to
            # LEAVE_REQUEST and re-open the form.
            if wizard_dismissed_reason is None and not is_leave_submission_locked(
                wf_state
            ):
                forced_intent = self._infer_followup_intent(context_lines, message)
                if forced_intent:
                    intent = forced_intent
                    intent_result = {"intent": forced_intent, "confidence": 1.0, "source": "followup"}

        wf_gate = getattr(session, "workflow_state", None) or {}
        if (
            wizard_dismissed_reason is None
            and is_leave_collecting(wf_gate)
            and intent == INTENT_UNKNOWN
            and not leave_side_interrupt
            and not leave_workflow_interrupt
        ):
            intent = INTENT_LEAVE_REQUEST
            intent_result = {
                **intent_result,
                "intent": INTENT_LEAVE_REQUEST,
                "source": (intent_result.get("source") or "intent") + "+leave_workflow_lock",
            }
        if (
            wizard_dismissed_reason is None
            and is_expense_in_progress(wf_gate)
            and intent == INTENT_UNKNOWN
        ):
            intent = INTENT_EXPENSE_CLAIM
            intent_result = {
                **intent_result,
                "intent": INTENT_EXPENSE_CLAIM,
                "source": (intent_result.get("source") or "intent") + "+expense_workflow_lock",
            }

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
        exp_pack: dict[str, Any] = {}
        leave_collecting_blocked = False
        expense_collecting_blocked = False
        wf_leave = getattr(session, "workflow_state", None) or {}
        run_leave_turn = intent == INTENT_LEAVE_REQUEST and not leave_workflow_interrupt
        if (
            not run_leave_turn
            and is_awaiting_leave_confirmation(wf_leave)
            and intent == INTENT_UNKNOWN
            and leave_side_interrupt
        ):
            run_leave_turn = True
        if (
            not run_leave_turn
            and is_awaiting_leave_confirmation(wf_leave)
            and intent == INTENT_LEAVE_REQUEST
            and (intent_result.get("source") or "").endswith("+confirm")
        ):
            run_leave_turn = True
        if run_leave_turn:
            lv_pack = process_leave_turn(
                workflow_state=wf_leave,
                message=message,
                entities=dict(entities),
                company_id=company_id,
            )
            wf_after_leave = lv_pack["workflow_state"]
            if not lv_pack.get("confirmed_submit"):
                syncer = LeaveDraftSyncService()
                draft_for_sync = dict(
                    read_leave_state(wf_after_leave).get("draft") or {}
                )
                wf_after_leave, _ = syncer.sync_draft(
                    wf_after_leave,
                    company_id=company_id,
                    employee_id=employee_id,
                    session_id=session.session_id,
                    draft=draft_for_sync,
                    trace_id=trace_id,
                )
            session.workflow_state = wf_after_leave
            session.save(update_fields=["workflow_state", "updated_at"])
            merged = lv_pack["merged_entities"] or {}
            entities.clear()
            entities.update(merged)
            leave_collecting_blocked = not bool(lv_pack.get("confirmed_submit"))
            if lv_pack.get("confirmed_submit"):
                entities["leave_workflow_confirmed"] = True
        elif is_leave_in_progress(wf_leave) and not leave_workflow_interrupt:
            leave_collecting_blocked = True
        log_step(
            trace_id,
            "leave_workflow_gate",
            {"blocked": leave_collecting_blocked, "intent": intent},
        )

        if intent == INTENT_EXPENSE_CLAIM or (
            is_expense_in_progress(getattr(session, "workflow_state", None) or {})
            and intent != INTENT_HR_POLICY
        ):
            wf_exp = getattr(session, "workflow_state", None) or {}
            day_logged = 0.0
            inc_hint = infer_expense_incurred_date_iso(
                message=message, hints=entities, today=date.today()
            )
            try:
                br = self.crm.get_expense_day_breakdown(
                    company_id=company_id,
                    employee_id=employee_id,
                    session_id=session.session_id,
                    incurred_date_iso=inc_hint,
                )
                day_logged = float(br.get("expense_day_logged_total") or 0)
            except Exception:
                day_logged = 0.0
            from chat.constants import EXPENSE_DAY_CAP_BDT

            exp_pack = process_expense_turn(
                workflow_state=wf_exp,
                message=message,
                company_id=company_id,
                employee_id=employee_id,
                session_id=session.session_id,
                day_logged_total=day_logged,
                daily_cap=float(EXPENSE_DAY_CAP_BDT),
            )
            session.workflow_state = exp_pack["workflow_state"]
            session.save(update_fields=["workflow_state", "updated_at"])
            expense_collecting_blocked = not bool(exp_pack.get("complete"))
            if exp_pack.get("items"):
                entities["expense_items"] = list(exp_pack["items"])
            if exp_pack.get("incurred_date_iso"):
                entities["expense_incurred_date"] = exp_pack["incurred_date_iso"]
            if exp_pack.get("warnings"):
                entities["expense_warnings"] = list(exp_pack["warnings"])
        log_step(
            trace_id,
            "expense_workflow_gate",
            {"blocked": expense_collecting_blocked, "intent": intent},
        )

        crm_context: dict[str, Any] = {}
        crm_payload: dict[str, Any] = {}
        status = "success"
        request_id = ""
        decision: dict[str, Any] = {}
        msg = ""
        rstatus = ""
        sources_out: list[dict[str, Any]] = []
        rag_unknown_hit = False

        try:
            if intent in (
                INTENT_LEAVE_BALANCE,
                INTENT_LEAVE_REQUEST,
                INTENT_WFH_REQUEST,
            ):
                bal = self.crm.get_leave_balance(
                    company_id=company_id,
                    employee_id=employee_id,
                    session_id=session.session_id,
                )
                crm_context.update(bal)
                crm_context["company_id"] = company_id
                crm_context["employee_id"] = employee_id
                if intent == INTENT_LEAVE_REQUEST and not leave_collecting_blocked:
                    lr_pack = self.crm.list_employee_leave_requests(
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                    )
                    crm_context["existing_leave_requests"] = list(
                        lr_pack.get("leave_requests") or []
                    )
                if intent == INTENT_LEAVE_BALANCE:
                    crm_payload.update(bal)

            if intent in (INTENT_EXPENSE_STATUS, INTENT_REQUEST_STATUS):
                rid = entities.get("request_id")
                if rid:
                    st = self.crm.get_request_status(
                        str(rid),
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                    )
                    crm_payload.update(st)
                elif _asks_recent_expense_submission(message):
                    last = (
                        (getattr(session, "workflow_state", None) or {}).get(
                            "expense_last_submission"
                        )
                        or {}
                    )
                    if last.get("reference_id"):
                        crm_payload["expense_last_submission"] = last
                    else:
                        crm_payload["detail"] = "No recent expense submission in this chat session."
                else:
                    crm_payload["detail"] = "Missing request_id for status lookup."

            # LEGACY / OBSOLETE: per-turn single-amount day cap for auto-approve path.
            # Enterprise workflow uses expense_validation warnings + SUBMITTED outcome.
            if intent == INTENT_EXPENSE_CLAIM and not expense_collecting_blocked:
                if not entities.get("expense_workflow_submit"):
                    inc_iso = (entities.get("expense_incurred_date") or "").strip() or infer_expense_incurred_date_iso(
                        message=message, hints=entities, today=date.today()
                    )
                    day_tot = self.crm.get_expense_day_approved_total(
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                        incurred_date_iso=inc_iso,
                    )
                    crm_context.update(day_tot)

            if intent == INTENT_HR_POLICY:
                dept = entities.get("department")
                rag = try_hr_policy_rag(
                    message,
                    trace_id,
                    company_id=company_id,
                    department=str(dept).strip() if dept else None,
                )
                if rag and rag.get("hit"):
                    crm_payload["rules_answer"] = rag.get("text") or ""
                    crm_payload["rules_mode"] = rag.get("mode") or "rag"
                    crm_payload["rules_matched_sections"] = []
                    crm_payload["rag_sources"] = rag.get("sources") or []
                    sources_out = list(crm_payload["rag_sources"])
                    log_step(
                        trace_id,
                        "rag_hr_policy_hit",
                        {"sources": len(sources_out)},
                    )
                else:
                    # `rules_handbook.py` is not used for runtime answers (RAG / KB only).
                    # if wants_full_handbook(message):
                    #     rules_pack = answer_rules_query(message)
                    #     ...
                    crm_payload["rules_answer"] = hr_policy_not_found_message()
                    crm_payload["rules_mode"] = "rag_no_hit"
                    crm_payload["rules_matched_sections"] = []
                    log_step(trace_id, "rag_no_hit_kb_only_no_static_handbook", {})

            if intent == INTENT_EXPENSE_DAY_SUMMARY:
                inc_iso = (entities.get("expense_incurred_date") or "").strip() or infer_expense_incurred_date_iso(
                    message=message, hints=entities, today=date.today()
                )
                breakdown = self.crm.get_expense_day_breakdown(
                    company_id=company_id,
                    employee_id=employee_id,
                    session_id=session.session_id,
                    incurred_date_iso=inc_iso,
                )
                crm_payload.update(breakdown)
                crm_context.update(breakdown)

            if leave_collecting_blocked and not leave_workflow_interrupt:
                q = lv_pack.get("question") or ""
                rules = ["LEAVE_WORKFLOW_COLLECTING"]
                if is_awaiting_leave_confirmation(
                    getattr(session, "workflow_state", None) or {}
                ):
                    rules = ["LEAVE_WORKFLOW_AWAITING_CONFIRMATION"]
                decision = {
                    "outcome": "NEEDS_CLARIFICATION",
                    "reason": q
                    or "আর একটু জানতে হবে — উপরের প্রশ্নের উত্তরটা নিচে লিখে পাঠান।",
                    "rules_applied": rules,
                }
            elif expense_collecting_blocked:
                stage_exp = str(
                    ((getattr(session, "workflow_state", None) or {}).get("expense_request") or {}).get(
                        "stage"
                    )
                    or ""
                )
                rules_exp = ["EXPENSE_WORKFLOW_COLLECTING"]
                if stage_exp in ("review", "confirming"):
                    rules_exp = ["EXPENSE_WORKFLOW_REVIEW"]
                elif stage_exp == "submit_confirm":
                    rules_exp = ["EXPENSE_WORKFLOW_SUBMIT_CONFIRM"]
                decision = {
                    "outcome": "NEEDS_CLARIFICATION",
                    "reason": exp_pack.get("question")
                    or "আজকের খরচের বিস্তারিত লিখুন (যেমন: lunch 100, bus 50)।",
                    "rules_applied": rules_exp,
                }
            else:
                if intent == INTENT_EXPENSE_CLAIM and exp_pack.get("submitted"):
                    entities["expense_workflow_submit"] = True
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
                if (
                    intent == INTENT_LEAVE_REQUEST
                    and decision.get("outcome") == "SUBMITTED"
                    and not leave_collecting_blocked
                    and entities.get("leave_workflow_confirmed")
                ):
                    sub_svc = LeaveSubmissionService(self.crm)
                    sub_result = sub_svc.submit_confirmed_leave(
                        workflow_state=getattr(session, "workflow_state", None) or {},
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                        entities=dict(entities),
                        decision=dict(decision),
                        trace_id=trace_id,
                        idempotency_key=idempotency_key,
                    )
                    session.workflow_state = sub_result.workflow_state
                    session.save(update_fields=["workflow_state", "updated_at"])
                    crm_payload["leave_submission"] = sub_result.crm_response
                    entities["leave_submission_payload"] = sub_result.payload
                    request_id = sub_result.submission_id
                    if sub_result.deduped:
                        crm_payload["_deduped"] = True
                if (
                    intent == INTENT_EXPENSE_CLAIM
                    and decision.get("outcome") == "SUBMITTED"
                    and not expense_collecting_blocked
                ):
                    from chat.services.expense_payload import build_expense_submission_payload
                    from chat.services.expense_submission_service import submit_expense_request

                    pl_exp = build_expense_submission_payload(
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                        items=list(entities.get("expense_items") or []),
                        incurred_date_iso=str(entities.get("expense_incurred_date") or ""),
                        trace_id=trace_id,
                        warnings=list(entities.get("expense_warnings") or []),
                    )
                    sub_exp = submit_expense_request(pl_exp)
                    crm_payload["expense_submission"] = sub_exp
                    entities["expense_submission_payload"] = pl_exp
                    request_id = str(sub_exp.get("reference_id") or "")
                crm_entities = dict(entities)
                if (
                    intent == INTENT_EXPENSE_CLAIM
                    and decision.get("outcome") == "SUBMITTED"
                    and crm_entities.get("expense_items")
                ):
                    total = sum(
                        float(x.get("amount") or 0)
                        for x in crm_entities.get("expense_items") or []
                    )
                    crm_entities["amount"] = total
                    crm_entities["expense_line_count"] = len(
                        crm_entities.get("expense_items") or []
                    )
                leave_already_submitted = bool(
                    intent == INTENT_LEAVE_REQUEST
                    and decision.get("outcome") == "SUBMITTED"
                    and request_id
                )
                if not leave_already_submitted:
                    exec_result = self.crm.create_request(
                        employee_id=employee_id,
                        intent=intent,
                        entities=crm_entities,
                        decision=decision,
                        company_id=company_id,
                        session_id=session.session_id,
                        idempotency_key=idempotency_key,
                    )
                    crm_rid = str(exec_result.get("request_id") or "")
                    if intent == INTENT_EXPENSE_CLAIM and decision.get("outcome") == "SUBMITTED":
                        sub_ref = str(
                            (crm_payload.get("expense_submission") or {}).get("reference_id")
                            or ""
                        )
                        request_id = sub_ref or crm_rid
                    elif not request_id:
                        request_id = crm_rid
                    crm_payload.update(exec_result)

            msg, rstatus = build_user_message(
                intent=intent,
                entities=entities,
                decision=decision,
                crm_payload=crm_payload,
            )
            if (
                intent == INTENT_EXPENSE_CLAIM
                and decision.get("outcome") == "SUBMITTED"
                and exp_pack.get("submitted")
            ):
                from chat.services.expense_workflow import format_expense_submitted_message

                sub_ref = str(
                    (crm_payload.get("expense_submission") or {}).get("reference_id")
                    or request_id
                    or ""
                )
                msg = format_expense_submitted_message(
                    items=list(entities.get("expense_items") or []),
                    reference_id=sub_ref,
                    incurred_date_iso=str(entities.get("expense_incurred_date") or ""),
                )
                rstatus = "success"
                session.workflow_state = save_expense_last_submission(
                    getattr(session, "workflow_state", None) or {},
                    reference_id=sub_ref,
                    items=list(entities.get("expense_items") or []),
                    incurred_date_iso=str(entities.get("expense_incurred_date") or ""),
                )
                session.save(update_fields=["workflow_state", "updated_at"])
            # Friendly, human-toned LLM fallback for cases where the rules /
            # intent pipeline could not produce a specific answer:
            #   1. The user message did not match any HR intent (UNKNOWN).
            #   2. The user asked about a rule we don't have a section for
            #      (HR_POLICY with rules_mode == "no_match"), but not rag_no_hit
            #      (explicit KB miss copy — do not overwrite with chit-chat).
            # When the LLM is unavailable we keep the existing degraded text
            # so the user always gets *something*.
            used_conversational = False
            if (
                intent == INTENT_UNKNOWN
                and decision.get("outcome") == "NEEDS_CLARIFICATION"
                and (is_rules_query(message) or _strong_hr_policy(message))
                and not response_finalized
            ):
                dept = entities.get("department")
                rag_u = try_hr_policy_rag(
                    message,
                    trace_id,
                    company_id=company_id,
                    department=str(dept).strip() if dept else None,
                )
                if rag_u and rag_u.get("hit"):
                    msg = rag_u.get("text") or msg
                    rstatus = "success"
                    sources_out = list(rag_u.get("sources") or [])
                    rag_unknown_hit = True
                    crm_payload["rules_mode"] = "rag"
                    crm_payload["rules_answer"] = msg
                    response_finalized = True
                    log_step(
                        trace_id,
                        "rag_unknown_policy_hit",
                        {"sources": len(sources_out)},
                    )

            wf_for_conv = getattr(session, "workflow_state", None) or {}
            needs_conversational = (
                not response_finalized
                and not rag_unknown_hit
                and not is_expense_in_progress(wf_for_conv)
                and not (
                    leave_workflow_interrupt
                    and intent in (INTENT_HR_POLICY, INTENT_LEAVE_BALANCE)
                )
                and not (
                    is_leave_collecting(wf_for_conv) and not leave_side_interrupt
                )
                and (
                    (
                        intent == INTENT_UNKNOWN
                        and decision.get("outcome") == "NEEDS_CLARIFICATION"
                    )
                    or (
                        intent == INTENT_HR_POLICY
                        and crm_payload.get("rules_mode") == "no_match"
                    )
                )
            )
            if needs_conversational:
                log_step(
                    trace_id,
                    "conversational_fallback_start",
                    {"intent": intent, "reason": "no_match_or_unknown"},
                )
                reply = conversational_reply(
                    message=message,
                    context_lines=context_lines,
                    trace_id=trace_id,
                )
                if reply:
                    msg = reply
                    rstatus = "success"
                    used_conversational = True
                    response_finalized = True
                    log_step(
                        trace_id,
                        "conversational_fallback_done",
                        {"chars": len(reply)},
                    )

            # Explicit "cancel the form" requests get a clear, deterministic
            # confirmation so the user knows the wizard is gone. We do this
            # AFTER the conversational fallback so an LLM reply never
            # contradicts the cancellation.
            if wizard_dismissed_reason == "cancel":
                user_lang = detect_user_language(message)
                msg = (
                    "ঠিক আছে, ছুটি ফর্মটি বাদ দিলাম। অন্য কিছু লাগলে জানাবেন।"
                    if user_lang == "bn"
                    else "Got it — I've cancelled the leave form. Let me know if you need anything else."
                )
                rstatus = "success"
                used_conversational = True

            # Language-aware reply for matched rules: rules content is authored
            # in English, so if the user wrote in Bangla / Banglish we translate
            # the answer to Bangla. Markdown structure is preserved. The
            # localized footer/hint is appended after translation so it never
            # gets dropped or mistranslated by the LLM.
            if (
                msg
                and not used_conversational
                and (
                    (
                        intent == INTENT_HR_POLICY
                        and crm_payload.get("rules_mode") in ("full", "section", "rag", "rag_no_hit")
                    )
                    or rag_unknown_hit
                )
            ):
                user_lang = detect_user_language(message)
                if user_lang == "bn":
                    log_step(
                        trace_id,
                        "rules_translate",
                        {"target_lang": "bn", "chars": len(msg)},
                    )
                    translated, ok = translate_text(
                        msg, target_lang="bn", trace_id=trace_id
                    )
                    if ok:
                        msg = translated
                rules_mode = str(crm_payload.get("rules_mode") or "")
                if rules_mode in ("full", "section", "rag", "rag_no_hit"):
                    footer_mode = "section" if rules_mode in ("rag", "rag_no_hit") else rules_mode
                    msg = msg.rstrip() + "\n\n" + _rules_footer(
                        mode=footer_mode, lang=user_lang
                    )

            leave_terminal_turn = (
                intent == INTENT_LEAVE_REQUEST
                and decision.get("outcome") == "SUBMITTED"
                and (
                    crm_payload.get("_deduped")
                    or entities.get("leave_workflow_confirmed")
                )
            )

            if leave_side_interrupt and not response_finalized and not leave_terminal_turn:
                reply = conversational_reply(
                    message=message,
                    context_lines=context_lines,
                    trace_id=trace_id,
                )
                if reply:
                    if is_greeting_now:
                        user_lang = detect_user_language(message)
                        prefix = "হ্যালো! " if user_lang == "bn" else "Hi! "
                        msg = prefix + reply
                    else:
                        msg = reply
                    rstatus = "success"
                    response_finalized = True

            if (
                not leave_terminal_turn
                and leave_side_interrupt
                and is_leave_in_progress(getattr(session, "workflow_state", None) or {})
                and (
                    not leave_workflow_interrupt
                    or is_awaiting_leave_confirmation(
                        getattr(session, "workflow_state", None) or {}
                    )
                )
            ):
                msg = _append_leave_workflow_resume(
                    msg, getattr(session, "workflow_state", None) or {}
                )
                response_finalized = True

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

        msg = polish_outbound_message(
            msg,
            intent=intent,
            outcome=str(decision.get("outcome") or ""),
            user_message=message,
            entities=entities,
            decision=decision,
            crm_payload=crm_payload,
        )

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
            "sources": sources_out,
            "status": status,
            "_session_id": session.session_id,
        }

    @staticmethod
    def _last_assistant_text(context_lines: list[str]) -> str | None:
        """Most recent assistant turn from the rolling conversation context."""
        for line in reversed(context_lines or []):
            if line.startswith("Assistant:"):
                content = line[len("Assistant:"):].strip()
                if content:
                    return content
        return None

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
            or "ছুটি আবেদন" in last_assistant
            or "**Step " in last_assistant
            or "Step 3 of 5" in last_assistant
        ):
            if len(msg) <= 180 or is_dateish:
                # Do not hijack policy / handbook questions (e.g. after a leave prompt
                # in the same session, or when the wizard is paused and the last turn
                # still mentioned leave). Those must stay HR_POLICY for RAG.
                if _strong_hr_policy(msg) or is_rules_query(msg):
                    return None
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
            "SUBMITTED",
            "REJECTED",
        ):
            return True
        if intent == INTENT_WFH_REQUEST:
            return decision.get("outcome") == "PENDING_APPROVAL"
        if intent == INTENT_EXPENSE_CLAIM:
            return decision.get("outcome") in (
                "SUBMITTED",
                "AUTO_APPROVED",
                "PENDING_APPROVAL",
            )
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
                "SUBMITTED",
                "REJECTED",
            ):
                return None
            wf_dup = getattr(session, "workflow_state", None) or {}
            st_dup = read_leave_state(wf_dup)
            rid = str(st_dup.get("submission_id") or "")
            if rid and is_leave_submission_locked(wf_dup):
                return rid
            cur = self._leave_booking_signature(entities)
            prev_draft = st_dup.get("draft") or {}
            if rid and prev_draft:
                prev_sig = self._leave_booking_signature(prev_draft)
                if (
                    cur[0] == prev_sig[0]
                    and cur[1] == prev_sig[1]
                    and abs(cur[2] - prev_sig[2]) < 1e-6
                    and cur[3] == prev_sig[3]
                    and cur[4] == prev_sig[4]
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


def _asks_recent_expense_submission(message: str) -> bool:
    low = (message or "").lower()
    if not re.search(r"\b(expense|খরচ|খরচের)\b", low) and "খরচ" not in (message or ""):
        return False
    return bool(
        re.search(
            r"(submit|জমা|submitted).*(হয়েছে|হয়েছে|hoyeche|hoise|done|করা|করেছি|করেছ)",
            low,
        )
        or re.search(
            r"(হয়েছে|হয়েছে|hoyeche|hoise|done).*(submit|জমা)",
            low,
        )
        or re.search(r"\bki\s+submit\b", low)
    )


def _wants_resume_expense(message: str) -> bool:
    from chat.services.expense_workflow import _is_confirmation_yes

    if _is_confirmation_yes(message):
        return True
    low = (message or "").lower()
    if re.search(r"\b(submit|জমা)\s*(koro|কর|দাও|দিন|করো)\b", low):
        return True
    if re.search(r"(খরচ|expense).*(continue|চালু|শেষ|আবার)", low):
        return True
    return False


def _wants_resume_leave(message: str) -> bool:
    low = (message or "").lower()
    raw = message or ""
    if re.search(r"\b(continue|resume|finish)\b.*\bleave\b", low):
        return True
    if re.search(r"\bleave\b.*\b(form|application|request)\b", low) and re.search(
        r"\b(continue|resume|back)\b", low
    ):
        return True
    if re.search(r"(ছুটি\s*(ফর্ম|আবেদন).*(চালু|শেষ|আবার)|continue\s*ছুটি)", raw, re.I):
        return True
    return False


def new_trace_id() -> str:
    return str(uuid.uuid4())
