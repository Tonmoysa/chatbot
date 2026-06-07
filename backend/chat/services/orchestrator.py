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
import chat.services.expense_incurred_date as expense_incurred_date_mod
from chat.services.intent_detector import (
    IntentDetector,
    _is_cancel_form_request,
    _is_fresh_start_greeting,
    _looks_like_chitchat,
    _message_answers_wizard_step,
    _strong_expense_claim,
    _strong_hr_policy,
    looks_like_wizard_side_question,
)
from chat.services.leave_days import compute_requested_leave_days
from chat.services.leave_confirm import (
    is_confirmation_cancel,
    is_confirmation_yes,
    is_awaiting_leave_confirmation,
    parse_edit_slot,
    wants_defer_leave_for_expense_submit,
    wants_defer_expense_for_leave_submit,
)
from chat.services.turn_classifier import (
    TURN_CHITCHAT,
    TURN_CONFIRM,
    TURN_CORRECTION,
    TURN_NEW_WORKFLOW,
    TURN_POLICY_QUERY,
    TURN_SLOT_ANSWER,
    _canonical_leave_wizard_token,
    classify_workflow_turn,
    is_workflow_continuation_turn,
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
from chat.services.expense.routing import looks_like_expense_wizard_continuation
from chat.services.expense_workflow import (
    _is_confirmation_no,
    _is_confirmation_yes,
    deactivate_expense_session,
    expense_pending_prompt,
    is_expense_collecting,
    is_expense_in_progress,
    is_expense_paused,
    pause_expense_session,
    process_expense_turn,
    resume_expense_session,
    save_expense_last_submission,
    wants_expense_summary,
)
from chat.services.workflow_suspend import (
    clear_suspended_expense,
    clear_suspended_leave,
    has_suspended_expense,
    has_suspended_leave,
    restore_suspended_expense,
    restore_suspended_leave,
    suspend_expense_for_workflow_switch,
    switch_active_expense_to_suspended_leave,
    wants_resume_suspended_leave,
    suspend_leave_for_workflow_switch,
)
from chat.services.intent_detector import (
    _strong_expense_day_summary,
    wants_post_submit_expense_summary,
)
from chat.services.conversational import conversational_reply
from chat.services.memory_store import ConversationMemoryStore
from chat.services.observability import log_step
from chat.services.message_context_clarity import (
    build_context_clarification_message,
    should_ask_context_clarification,
)
from chat.services.message_polish import polish_outbound_message
from chat.services.response_formatter import build_user_message
# from chat.services.rules_handbook import (
#     answer_rules_query,
#     is_rules_query,
#     wants_full_handbook,
# )  # disabled — policy text comes only from knowledge-base RAG
from chat.services.policy_intent_helpers import (
    build_out_of_scope_message,
    is_expense_entitlement_query,
    is_general_knowledge_out_of_scope,
    is_irrelevant_answer_complaint,
    is_leave_wizard_misroute_complaint,
    is_off_topic_for_hr_assistant,
    is_policy_handbook_complaint,
    is_policy_kb_query,
    is_rules_query,
)
from chat.services.translator import (
    align_policy_answer_language,
    detect_reply_language,
    detect_user_language,
    is_translation_request,
    is_weak_language_signal,
    resolve_reply_language,
    strip_policy_footer,
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
_RULES_FOOTER_BANGLISH_SECTION = (
    "_(Answer tomar uploaded policy theke; policy name ba topic diye ask koro.)_"
)
_RULES_FOOTER_BANGLISH_FULL = (
    "_(Specific policy name likhe ask koro — e.g. \"termination policy\" ba \"expense policy\".)_"
)


def _is_policy_interrupt_message(message: str) -> bool:
    """True when the user is asking about HR policy/rules (not filling leave slots)."""
    if is_expense_entitlement_query(message):
        return True
    if _strong_hr_policy(message) and is_rules_query(message):
        return True
    low = (message or "").lower()
    if re.search(
        r"\b(payslip|pay\s*slip|salary\s*slip|payroll|payslips)\b",
        low,
    ):
        return True
    return bool(
        re.search(r"(পেস্লিপ|বেতন\s*স্লিপ|বেতনের\s*স্লিপ)", message or "", re.I)
    )


def _should_short_circuit_submitted_leave(message: str) -> bool:
    """
    After a leave is submitted+locked, only block repeat submit / new leave attempts.
    Policy, balance, expense wizard, and general chat must reach the normal pipeline.
    Bare yes/no is not treated as leave — expense review also uses yes/no.
    """
    if _is_policy_interrupt_message(message) or _leave_balance_probe(message):
        return False
    if _looks_like_chitchat(message, strict=True) or _is_fresh_start_greeting(message):
        return False
    low = (message or "").lower()
    # New leave phrasing after a prior submit starts a fresh wizard — do not short-circuit here.
    if re.search(
        r"(ছুটি|chuti|chhuti|leave).*(চাই|lagbe|lage|apply|request|নিতে|লাগবে|joma|submit)",
        low,
    ):
        return False
    if re.search(r"\b(submit|confirm|joma|জমা)\b.*\bleave\b", low):
        return True
    if re.search(r"\b(leave|chuti|chhuti|ছুটি)\b.*\b(submit|confirm|joma|জমা)\b", low):
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
        if wants_defer_leave_for_expense_submit(message) and has_suspended_expense(
            workflow_state
        ):
            return {
                "intent": INTENT_EXPENSE_CLAIM,
                "confidence": 0.99,
                "source": "leave_workflow_gate+defer_expense_submit",
            }
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
        from chat.services.expense_workflow import wants_resume_or_show_expense

        if has_suspended_expense(workflow_state) and wants_resume_or_show_expense(
            message
        ):
            return {
                "intent": INTENT_EXPENSE_CLAIM,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_expense_resume",
            }
        if _strong_expense_claim(message) or _strong_expense_day_summary(message):
            return {
                "intent": INTENT_EXPENSE_CLAIM
                if _strong_expense_claim(message)
                else INTENT_EXPENSE_DAY_SUMMARY,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_expense_switch",
            }
        # If the user sends free-form text while we're awaiting final confirmation,
        # treat it as a leave workflow update (typically a reason tweak) rather than
        # misclassifying it as chitchat and dropping the workflow.
        t_msg = (message or "").strip()
        looks_like_question = bool(
            re.search(r"^(can\s+i|what|why|how|when|where|which)\b", t_msg, re.I)
            or t_msg.endswith("?")
        )
        if t_msg and len(t_msg) >= 12 and not looks_like_question:
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_freeform",
            }
        if _looks_like_chitchat(message, strict=True) or _is_fresh_start_greeting(message):
            return {
                "intent": INTENT_UNKNOWN,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_interrupt",
            }
        from chat.services.leave_confirm import _looks_like_slot_correction

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
    if _strong_expense_claim(message) or _strong_expense_day_summary(message):
        return {
            "intent": INTENT_EXPENSE_CLAIM
            if _strong_expense_claim(message)
            else INTENT_EXPENSE_DAY_SUMMARY,
            "confidence": 0.99,
            "source": "leave_workflow_gate+expense_switch",
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


def _is_leave_application_message(message: str) -> bool:
    """New leave request phrasing — not the same as asking about leave *policy*."""
    if _is_policy_interrupt_message(message):
        return False
    low = (message or "").lower()
    return bool(
        re.search(
            r"(ছুটি|chuti|chhuti|leave).{0,40}(চাই|lagbe|lage|apply|request|নিতে|লাগবে|nit(e)?\s*chai)",
            low,
        )
        or re.search(r"\b(apply|request)\s+(for\s+)?(a\s+)?leave\b", low)
    )


def _detect_intent_during_expense_workflow(
    message: str,
    workflow_state: dict[str, Any],
    *,
    balance_probe: bool,
) -> dict[str, Any]:
    """Deterministic intent while expense_request is active — LLM must not override."""
    if wants_defer_expense_for_leave_submit(message) and has_suspended_leave(
        workflow_state
    ):
        return {
            "intent": INTENT_LEAVE_REQUEST,
            "confidence": 0.99,
            "source": "expense_workflow_gate+defer_leave_submit",
        }
    if has_suspended_leave(workflow_state) and wants_resume_suspended_leave(message):
        return {
            "intent": INTENT_LEAVE_REQUEST,
            "confidence": 0.99,
            "source": "expense_workflow_gate+resume_leave_nav",
        }
    from chat.services.expense_workflow import wants_resume_or_show_expense

    if wants_resume_or_show_expense(message):
        return {
            "intent": INTENT_EXPENSE_CLAIM,
            "confidence": 0.99,
            "source": "expense_workflow_gate+resume_show",
        }
    if _asks_recent_leave_submission(message):
        return {
            "intent": INTENT_REQUEST_STATUS,
            "confidence": 0.99,
            "source": "expense_workflow_gate+leave_submit_status",
        }
    if _asks_recent_expense_submission(message):
        return {
            "intent": INTENT_EXPENSE_STATUS,
            "confidence": 0.99,
            "source": "expense_workflow_gate+submit_status",
        }
    if _is_confirmation_yes(message) or _is_confirmation_no(message):
        return {
            "intent": INTENT_EXPENSE_CLAIM,
            "confidence": 0.99,
            "source": "expense_workflow_gate+confirm",
        }
    if wants_expense_summary(message):
        return {
            "intent": INTENT_EXPENSE_CLAIM,
            "confidence": 0.99,
            "source": "expense_workflow_gate+summary",
        }
    if wants_post_submit_expense_summary(message) or _strong_expense_day_summary(message):
        return {
            "intent": INTENT_EXPENSE_DAY_SUMMARY,
            "confidence": 0.99,
            "source": "expense_workflow_gate+day_summary",
        }
    if _is_policy_interrupt_message(message):
        return {
            "intent": INTENT_HR_POLICY,
            "confidence": 0.99,
            "source": "expense_workflow_gate+policy",
        }
    if is_expense_entitlement_query(message):
        return {
            "intent": INTENT_HR_POLICY,
            "confidence": 0.99,
            "source": "expense_workflow_gate+entitlement",
        }
    if balance_probe:
        return {
            "intent": INTENT_LEAVE_BALANCE,
            "confidence": 0.99,
            "source": "expense_workflow_gate+balance",
        }
    if _looks_like_chitchat(message, strict=True) or _is_fresh_start_greeting(message):
        return {
            "intent": INTENT_UNKNOWN,
            "confidence": 0.99,
            "source": "expense_workflow_gate+interrupt",
        }
    if _is_leave_application_message(message):
        return {
            "intent": INTENT_LEAVE_REQUEST,
            "confidence": 0.99,
            "source": "expense_workflow_gate+leave_apply",
        }
    if is_general_knowledge_out_of_scope(message):
        return {
            "intent": INTENT_UNKNOWN,
            "confidence": 0.99,
            "source": "expense_workflow_gate+out_of_scope",
        }
    if looks_like_wizard_side_question(message):
        return {
            "intent": INTENT_UNKNOWN,
            "confidence": 0.99,
            "source": "expense_workflow_gate+side_question",
        }
    if looks_like_expense_wizard_continuation(message):
        return {
            "intent": INTENT_EXPENSE_CLAIM,
            "confidence": 0.99,
            "source": "expense_workflow_gate+slot",
        }
    return {
        "intent": INTENT_UNKNOWN,
        "confidence": 0.99,
        "source": "expense_workflow_gate+unrelated",
    }


def _append_leave_workflow_resume(message: str, workflow_state: dict[str, Any]) -> str:
    resume = pending_question(workflow_state)
    if not resume:
        return message
    return message.rstrip() + "\n\n" + resume


def _append_expense_workflow_resume(message: str, workflow_state: dict[str, Any]) -> str:
    resume = expense_pending_prompt(workflow_state)
    if not resume:
        return message
    return message.rstrip() + "\n\n" + resume


def _expense_paused_workflow_hint(user_lang: str) -> str:
    if user_lang == "bn":
        return (
            "The user has a paused expense draft saved. You may add ONE soft line that "
            "they can continue the expense later — do not list items, amounts, or dates."
        )
    return (
        "The user has a paused expense draft saved. You may add ONE soft line that "
        "they can continue the expense later — do not list items, amounts, or dates."
    )


def _append_suspended_leave_resume_after_switch(
    message: str, workflow_state: dict[str, Any], *, user_message: str = ""
) -> str:
    """After another workflow completes, nudge the user back to a parked leave draft."""
    if not is_leave_in_progress(workflow_state):
        return message
    resume = pending_question(workflow_state)
    if not resume:
        return message
    from chat.services.translator import detect_user_language

    lang = detect_user_language(user_message or message)
    if lang == "bn":
        hint = "আপনার ছুটির আবেদনটি এখনো অসম্পূর্ণ — যেখানে থেমেছিলেন সেখান থেকে চালিয়ে যান।"
    else:
        hint = "Your leave request is still in progress — continuing where you left off."
    return message.rstrip() + "\n\n" + hint + "\n\n" + resume


def _append_suspended_expense_resume_after_switch(
    message: str, workflow_state: dict[str, Any], *, user_message: str = ""
) -> str:
    if not is_expense_in_progress(workflow_state):
        return message
    from chat.services.expense_workflow import format_expense_resume_message

    resume = format_expense_resume_message(workflow_state, user_message=user_message or message)
    if not resume:
        return message
    return message.rstrip() + "\n\n" + resume


def _persist_workflow_state(
    session: Any, workflow_state: dict[str, Any]
) -> dict[str, Any]:
    session.workflow_state = workflow_state
    session.save(update_fields=["workflow_state", "updated_at"])
    return session.workflow_state or {}


def _answers_suspended_leave_step(
    message: str, workflow_state: dict[str, Any]
) -> bool:
    from chat.services.workflow_suspend import KEY_SUSPENDED_LEAVE

    sl = (workflow_state or {}).get(KEY_SUSPENDED_LEAVE) or {}
    step = sl.get("step")
    if not step:
        return False
    if _message_answers_wizard_step(message, step):
        return True
    return bool(_canonical_leave_wizard_token(message))


def _rules_footer(*, mode: str, lang: str) -> str:
    if lang == "bn":
        return _RULES_FOOTER_BN_SECTION if mode == "section" else _RULES_FOOTER_BN_FULL
    if lang == "banglish":
        return (
            _RULES_FOOTER_BANGLISH_SECTION
            if mode == "section"
            else _RULES_FOOTER_BANGLISH_FULL
        )
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
            prev_assistant = self._assistant_text_for_translation(
                context_lines, target_lang=translate_to
            )
            if prev_assistant:
                source = strip_policy_footer(prev_assistant)
                log_step(
                    trace_id,
                    "translation_request",
                    {
                        "target_lang": translate_to,
                        "source_chars": len(source),
                    },
                )
                translated, ok = translate_text(
                    source,
                    target_lang=translate_to,
                    trace_id=trace_id,
                )
                if ok:
                    msg = translated.rstrip() + "\n\n" + _rules_footer(
                        mode="section",
                        lang=translate_to,
                    )
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
                wf_state = getattr(session, "workflow_state", None) or {}
                exp_block = wf_state.get("expense_request") or {}
                if is_expense_in_progress(wf_state):
                    exp_block["reply_language"] = translate_to
                    wf_state["expense_request"] = exp_block
                    session.workflow_state = wf_state
                    session.save(update_fields=["workflow_state", "updated_at"])
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
        if (
            is_leave_submission_locked(wf_state)
            and _should_short_circuit_submitted_leave(message)
            and not is_expense_in_progress(wf_state)
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
        expense_side_interrupt = False
        expense_workflow_interrupt = False
        response_finalized = False

        # Resume paused leave on any continuation except another explicit policy lookup.
        if is_leave_paused(wf_state) and not is_cancel_now:
            policy_interrupt = _is_policy_interrupt_message(message)
            if _wants_resume_leave(message) or not policy_interrupt:
                session.workflow_state = resume_leave_session(wf_state)
                session.save(update_fields=["workflow_state", "updated_at"])
                wf_state = session.workflow_state or {}
                log_step(trace_id, "leave_wizard_auto_resumed", {})

        if (
            not is_cancel_now
            and has_suspended_leave(wf_state)
            and wants_resume_suspended_leave(message)
            and is_expense_in_progress(wf_state)
            and not is_leave_in_progress(wf_state)
        ):
            wf_state = _persist_workflow_state(
                session, switch_active_expense_to_suspended_leave(wf_state)
            )
            log_step(trace_id, "expense_suspended_resume_leave_nav", {})

        if is_expense_paused(wf_state) and not is_cancel_now:
            policy_interrupt = _is_policy_interrupt_message(message)
            balance_interrupt = balance_probe
            if wants_resume_suspended_leave(message) and has_suspended_leave(wf_state):
                wf_state = _persist_workflow_state(
                    session, switch_active_expense_to_suspended_leave(wf_state)
                )
                log_step(trace_id, "expense_paused_resume_leave_nav", {})
            elif (
                _wants_resume_expense(message)
                or wants_expense_summary(message)
                or looks_like_expense_wizard_continuation(message)
            ):
                wf_state = _persist_workflow_state(
                    session, resume_expense_session(wf_state)
                )
                log_step(trace_id, "expense_wizard_auto_resumed", {})

        # Restore a workflow parked while the user completed another task.
        if (
            has_suspended_leave(wf_state)
            and not is_leave_in_progress(wf_state)
            and not is_expense_in_progress(wf_state)
            and not is_cancel_now
            and (
                wants_resume_suspended_leave(message)
                or _is_leave_application_message(message)
                or _answers_suspended_leave_step(message, wf_state)
            )
        ):
            wf_state = _persist_workflow_state(
                session, restore_suspended_leave(wf_state, force_active=True)
            )
            log_step(trace_id, "suspended_leave_restored", {})

        if (
            has_suspended_expense(wf_state)
            and not is_expense_in_progress(wf_state)
            and not is_leave_in_progress(wf_state)
            and not is_cancel_now
            and (
                _wants_resume_expense(message)
                or wants_expense_summary(message)
                or _strong_expense_claim(message)
            )
        ):
            wf_state = _persist_workflow_state(
                session, restore_suspended_expense(wf_state)
            )
            log_step(trace_id, "suspended_expense_restored", {})

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
        elif is_expense_in_progress(wf_state):
            intent_result = _detect_intent_during_expense_workflow(
                message, wf_state, balance_probe=balance_probe
            )
            log_step(trace_id, "intent_detection_skipped_expense_active", intent_result)
        else:
            intent_result = self.intents.detect(message, trace_id)
        intent = intent_result["intent"]
        if is_expense_entitlement_query(message):
            intent = INTENT_HR_POLICY
            intent_result = {
                **intent_result,
                "intent": INTENT_HR_POLICY,
                "source": (intent_result.get("source") or "intent")
                + "+entitlement_policy",
            }
        wizard_misroute_complaint = is_leave_wizard_misroute_complaint(message)
        policy_complaint = (
            is_irrelevant_answer_complaint(message)
            or is_policy_handbook_complaint(message)
            or wizard_misroute_complaint
        )
        wizard_active_gate = is_leave_in_progress(wf_state) or is_expense_in_progress(
            wf_state
        )
        general_out_of_scope = is_off_topic_for_hr_assistant(
            message,
            wizard_active=wizard_active_gate,
        ) or (
            intent == INTENT_HR_POLICY
            and not is_policy_kb_query(message)
            and not policy_complaint
        )
        if policy_complaint or general_out_of_scope:
            intent = INTENT_UNKNOWN
            intent_result = {
                **intent_result,
                "intent": INTENT_UNKNOWN,
                "source": (intent_result.get("source") or "intent")
                + ("+policy_complaint_gate" if policy_complaint else "+out_of_scope_gate"),
            }
            log_step(
                trace_id,
                "intent_gated",
                {
                    "policy_complaint": policy_complaint,
                    "general_out_of_scope": general_out_of_scope,
                },
            )

        workflow_turn: str | None = None
        if is_leave_in_progress(wf_state) or is_expense_in_progress(wf_state):
            workflow_turn = classify_workflow_turn(
                message,
                leave_active=is_leave_in_progress(wf_state),
                expense_active=is_expense_in_progress(wf_state),
                pending_leave_step=(
                    pending_step(wf_state) if is_leave_in_progress(wf_state) else None
                ),
                balance_probe=balance_probe,
            )
            log_step(
                trace_id,
                "workflow_turn_classified",
                {"turn_type": workflow_turn},
            )

        # Explicit cancel drops the leave draft; greetings keep the draft alive.
        if is_leave_in_progress(wf_state) and is_cancel_now:
            wf_state = deactivate_leave_session(wf_state)
            wf_state = clear_suspended_leave(wf_state)
            wf_state = _persist_workflow_state(session, wf_state)
            wizard_dismissed_reason = "cancel"
            intent = INTENT_UNKNOWN
            intent_result = {
                **intent_result,
                "intent": INTENT_UNKNOWN,
                "source": (intent_result.get("source") or "intent")
                + "+wizard_dismissed_cancel",
            }
            log_step(trace_id, "leave_wizard_dismissed", {"reason": "cancel"})
        elif is_expense_in_progress(wf_state) and is_cancel_now:
            wf_state = deactivate_expense_session(wf_state)
            wf_state = _persist_workflow_state(session, wf_state)
            wizard_dismissed_reason = "cancel"
            intent = INTENT_UNKNOWN
            intent_result = {
                **intent_result,
                "intent": INTENT_UNKNOWN,
                "source": (intent_result.get("source") or "intent")
                + "+wizard_dismissed_cancel",
            }
            log_step(trace_id, "expense_wizard_dismissed", {"reason": "cancel"})
        elif is_cancel_now and has_suspended_leave(wf_state):
            wf_state = _persist_workflow_state(session, clear_suspended_leave(wf_state))
            wizard_dismissed_reason = "cancel"
            intent = INTENT_UNKNOWN
            intent_result = {
                **intent_result,
                "intent": INTENT_UNKNOWN,
                "source": (intent_result.get("source") or "intent")
                + "+suspended_leave_dismissed_cancel",
            }
            log_step(trace_id, "suspended_leave_dismissed", {"reason": "cancel"})
        elif is_cancel_now and has_suspended_expense(wf_state):
            wf_state = _persist_workflow_state(session, clear_suspended_expense(wf_state))
            wizard_dismissed_reason = "cancel"
            intent = INTENT_UNKNOWN
            intent_result = {
                **intent_result,
                "intent": INTENT_UNKNOWN,
                "source": (intent_result.get("source") or "intent")
                + "+suspended_expense_dismissed_cancel",
            }
            log_step(trace_id, "suspended_expense_dismissed", {"reason": "cancel"})

        if is_leave_in_progress(wf_state):
            hard_switch = intent in (
                INTENT_EXPENSE_CLAIM,
                INTENT_EXPENSE_DAY_SUMMARY,
                INTENT_WFH_REQUEST,
                INTENT_ATTENDANCE_CORRECTION,
                INTENT_APPROVAL_ESCALATION,
            )
            if hard_switch:
                wf_state = _persist_workflow_state(
                    session, suspend_leave_for_workflow_switch(wf_state)
                )
                log_step(trace_id, "leave_wizard_suspended_for_switch", {"intent": intent})
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
                if not policy_complaint and (
                    workflow_turn == TURN_CHITCHAT or general_out_of_scope
                ):
                    if (
                        (
                            is_leave_collecting(wf_state)
                            or is_awaiting_leave_confirmation(wf_state)
                        )
                        and not is_leave_paused(wf_state)
                        and not is_greeting_now
                    ):
                        wf_state = _persist_workflow_state(
                            session, pause_leave_session(wf_state)
                        )
                        log_step(trace_id, "leave_wizard_paused_for_side_question", {})
                    leave_side_interrupt = True
                elif workflow_turn == TURN_POLICY_QUERY:
                    leave_workflow_interrupt = True
        elif is_expense_in_progress(wf_state):
            if intent in (
                INTENT_LEAVE_REQUEST,
                INTENT_WFH_REQUEST,
                INTENT_ATTENDANCE_CORRECTION,
                INTENT_APPROVAL_ESCALATION,
            ):
                wf_state = _persist_workflow_state(
                    session, suspend_expense_for_workflow_switch(wf_state)
                )
                log_step(
                    trace_id, "expense_wizard_suspended_for_switch", {"intent": intent}
                )
            elif intent in (
                INTENT_EXPENSE_DAY_SUMMARY,
                INTENT_EXPENSE_STATUS,
                INTENT_REQUEST_STATUS,
            ):
                pass
            elif intent == INTENT_HR_POLICY:
                if not is_expense_paused(wf_state):
                    session.workflow_state = pause_expense_session(wf_state)
                    session.save(update_fields=["workflow_state", "updated_at"])
                    wf_state = session.workflow_state or {}
                    log_step(trace_id, "expense_wizard_paused_for_policy", {})
                expense_workflow_interrupt = True
            elif intent == INTENT_LEAVE_BALANCE:
                if not is_expense_paused(wf_state):
                    session.workflow_state = pause_expense_session(wf_state)
                    session.save(update_fields=["workflow_state", "updated_at"])
                    wf_state = session.workflow_state or {}
                    log_step(trace_id, "expense_wizard_paused_for_balance", {})
                expense_workflow_interrupt = True
            elif intent == INTENT_UNKNOWN:
                if not policy_complaint and (
                    workflow_turn == TURN_CHITCHAT or general_out_of_scope
                ):
                    if not is_expense_paused(wf_state) and not is_greeting_now:
                        wf_state = _persist_workflow_state(
                            session, pause_expense_session(wf_state)
                        )
                        log_step(trace_id, "expense_wizard_paused_for_side_question", {})
                    expense_side_interrupt = True
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
            and (
                intent == INTENT_UNKNOWN
                or (
                    workflow_turn is not None
                    and is_workflow_continuation_turn(workflow_turn)
                )
            )
            and not leave_side_interrupt
            and not leave_workflow_interrupt
        ):
            if intent == INTENT_UNKNOWN or workflow_turn in (
                TURN_SLOT_ANSWER,
                TURN_CORRECTION,
                TURN_CONFIRM,
            ):
                intent = INTENT_LEAVE_REQUEST
                intent_result = {
                    **intent_result,
                    "intent": INTENT_LEAVE_REQUEST,
                    "source": (intent_result.get("source") or "intent")
                    + "+leave_workflow_lock",
                }
        if (
            wizard_dismissed_reason is None
            and is_expense_in_progress(wf_gate)
            and (
                intent == INTENT_UNKNOWN
                or (
                    workflow_turn is not None
                    and is_workflow_continuation_turn(workflow_turn)
                )
                or (
                    (_is_confirmation_yes(message) or _is_confirmation_no(message) or wants_expense_summary(message))
                    and intent != INTENT_HR_POLICY
                    and intent != INTENT_LEAVE_BALANCE
                    and intent != INTENT_EXPENSE_DAY_SUMMARY
                    and intent != INTENT_EXPENSE_STATUS
                    and intent != INTENT_REQUEST_STATUS
                )
            )
            and not expense_side_interrupt
            and not expense_workflow_interrupt
            and not general_out_of_scope
        ):
            intent = INTENT_EXPENSE_CLAIM
            intent_result = {
                **intent_result,
                "intent": INTENT_EXPENSE_CLAIM,
                "source": (intent_result.get("source") or "intent") + "+expense_workflow_lock",
            }
        if (
            wizard_dismissed_reason is None
            and (
                wants_post_submit_expense_summary(message)
                or (
                    not is_expense_in_progress(wf_gate)
                    and wants_expense_summary(message)
                )
            )
        ):
            intent = INTENT_EXPENSE_DAY_SUMMARY
            intent_result = {
                **intent_result,
                "intent": INTENT_EXPENSE_DAY_SUMMARY,
                "source": (intent_result.get("source") or "intent") + "+expense_day_summary_heuristic",
            }
        elif (
            wizard_dismissed_reason is None
            and not is_expense_in_progress(wf_gate)
            and intent == INTENT_UNKNOWN
            and _strong_expense_claim(message)
        ):
            intent = INTENT_EXPENSE_CLAIM
            intent_result = {
                **intent_result,
                "intent": INTENT_EXPENSE_CLAIM,
                "source": (intent_result.get("source") or "intent") + "+expense_start_heuristic",
            }
        elif wizard_dismissed_reason is None and _asks_recent_leave_submission(message):
            intent = INTENT_REQUEST_STATUS
            intent_result = {
                **intent_result,
                "intent": INTENT_REQUEST_STATUS,
                "source": (intent_result.get("source") or "intent") + "+leave_submit_status_heuristic",
            }
        elif wizard_dismissed_reason is None and _asks_recent_expense_submission(message):
            intent = INTENT_EXPENSE_STATUS
            intent_result = {
                **intent_result,
                "intent": INTENT_EXPENSE_STATUS,
                "source": (intent_result.get("source") or "intent") + "+expense_submit_status_heuristic",
            }

        context_clarification_msg: str | None = None
        if wizard_dismissed_reason is None and not general_out_of_scope:
            wf_clar = getattr(session, "workflow_state", None) or {}
            if should_ask_context_clarification(
                message,
                context_lines,
                intent=intent,
                balance_probe=balance_probe,
                leave_active=is_leave_in_progress(wf_clar),
                expense_active=is_expense_in_progress(wf_clar),
                workflow_continuation=(
                    workflow_turn is not None
                    and is_workflow_continuation_turn(workflow_turn)
                ),
            ):
                context_clarification_msg = build_context_clarification_message(
                    message, context_lines
                )
                intent = INTENT_UNKNOWN
                intent_result = {
                    **intent_result,
                    "intent": INTENT_UNKNOWN,
                    "source": (intent_result.get("source") or "intent")
                    + "+context_clarification",
                }
                log_step(trace_id, "context_clarification_asked", {})

        log_step(trace_id, "intent_detection_done", {"intent": intent})

        log_step(trace_id, "entity_extraction_start", {})
        wf_ent = getattr(session, "workflow_state", None) or {}
        use_rules_entities = (
            is_leave_in_progress(wf_ent)
            and workflow_turn is not None
            and is_workflow_continuation_turn(workflow_turn)
            and not _is_policy_interrupt_message(message)
        )
        from chat.services.expense.entity_pipeline import ExpenseEntityPipeline
        from chat.services.expense.llm_gate import expense_wizard_should_use_llm
        from chat.services.leave.entity_pipeline import LeaveEntityPipeline
        from chat.services.leave.llm_gate import leave_wizard_should_use_llm

        expense_pipe_result = None
        leave_pipeline_active = (
            wizard_dismissed_reason is None
            and intent
            not in (
                INTENT_HR_POLICY,
                INTENT_LEAVE_BALANCE,
                INTENT_EXPENSE_CLAIM,
                INTENT_EXPENSE_DAY_SUMMARY,
            )
            and (
                intent == INTENT_LEAVE_REQUEST
                or (
                    is_leave_in_progress(wf_ent)
                    and workflow_turn
                    not in (TURN_POLICY_QUERY, TURN_CHITCHAT, TURN_NEW_WORKFLOW)
                )
            )
        )
        expense_pipeline_active = (
            wizard_dismissed_reason is None
            and intent
            not in (
                INTENT_HR_POLICY,
                INTENT_LEAVE_BALANCE,
                INTENT_LEAVE_REQUEST,
                INTENT_EXPENSE_DAY_SUMMARY,
            )
            and (
                intent == INTENT_EXPENSE_CLAIM
                or (
                    is_expense_in_progress(wf_ent)
                    and workflow_turn
                    not in (TURN_POLICY_QUERY, TURN_CHITCHAT, TURN_NEW_WORKFLOW)
                )
            )
        )
        if leave_pipeline_active:
            use_llm = not (
                is_leave_in_progress(wf_ent)
                and workflow_turn is not None
                and not leave_wizard_should_use_llm(
                    message, workflow_turn=workflow_turn
                )
            )
            pipe_result = LeaveEntityPipeline(self.entities).extract(
                message,
                intent=intent,
                context_lines=context_lines,
                trace_id=trace_id,
                use_llm=use_llm,
            )
            entities = pipe_result.entities
            entity_result = {
                "entities": entities,
                "source": pipe_result.source,
                "field_sources": pipe_result.field_sources,
            }
            log_step(
                trace_id,
                "leave_entity_pipeline",
                {"use_llm": use_llm, "field_sources": pipe_result.field_sources},
            )
        elif expense_pipeline_active:
            use_llm = not (
                is_expense_in_progress(wf_ent)
                and workflow_turn is not None
                and not expense_wizard_should_use_llm(
                    message, workflow_turn=workflow_turn
                )
            )
            expense_pipe_result = ExpenseEntityPipeline(self.entities).extract(
                message,
                intent=intent,
                context_lines=context_lines,
                trace_id=trace_id,
                use_llm=use_llm,
            )
            entities = expense_pipe_result.entities
            entity_result = {
                "entities": entities,
                "source": expense_pipe_result.source,
                "field_sources": expense_pipe_result.field_sources,
            }
            log_step(
                trace_id,
                "expense_entity_pipeline",
                {"use_llm": use_llm, "field_sources": expense_pipe_result.field_sources},
            )
        elif use_rules_entities:
            entities = self.entities.extract_rules_only(message, intent=intent)
            entity_result = {"entities": entities, "source": "rules_wizard"}
        else:
            entity_result = self.entities.extract(
                message, intent, context_lines, trace_id
            )
            entities = entity_result.get("entities") or {}
        if document_text:
            # Carry document text into the rule engine (LLM must not decide outcomes).
            entities["document_text"] = document_text
        log_step(trace_id, "entity_extraction_done", {"keys": list(entities.keys())})

        if wizard_dismissed_reason is None and intent == INTENT_LEAVE_REQUEST:
            wf_resume = getattr(session, "workflow_state", None) or {}
            if has_suspended_leave(wf_resume) and not is_leave_in_progress(wf_resume):
                wf_resume = _persist_workflow_state(
                    session, restore_suspended_leave(wf_resume, force_active=True)
                )
                log_step(trace_id, "suspended_leave_restored_for_intent", {})

        if wizard_dismissed_reason is None and intent == INTENT_EXPENSE_CLAIM:
            wf_resume = getattr(session, "workflow_state", None) or {}
            if has_suspended_expense(wf_resume) and not is_expense_in_progress(wf_resume):
                wf_resume = _persist_workflow_state(
                    session, restore_suspended_expense(wf_resume)
                )
                log_step(trace_id, "suspended_expense_restored_for_intent", {})

        lv_pack: dict[str, Any] = {}
        exp_pack: dict[str, Any] = {}
        leave_collecting_blocked = False
        expense_collecting_blocked = False
        wf_leave = getattr(session, "workflow_state", None) or {}
        run_leave_turn = (
            intent == INTENT_LEAVE_REQUEST and not leave_workflow_interrupt
        ) or (
            is_leave_in_progress(wf_leave)
            and workflow_turn is not None
            and is_workflow_continuation_turn(workflow_turn)
            and not leave_workflow_interrupt
            and not leave_side_interrupt
        )
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

        wf_exp_gate = getattr(session, "workflow_state", None) or {}
        run_expense_turn = not expense_workflow_interrupt and not expense_side_interrupt and not general_out_of_scope and (
            intent == INTENT_EXPENSE_CLAIM
            or (
                is_expense_in_progress(wf_exp_gate)
                and intent
                not in (
                    INTENT_HR_POLICY,
                    INTENT_LEAVE_BALANCE,
                    INTENT_EXPENSE_DAY_SUMMARY,
                    INTENT_EXPENSE_STATUS,
                    INTENT_REQUEST_STATUS,
                )
            )
        )
        if run_expense_turn:
            wf_exp = wf_exp_gate
            day_logged = 0.0
            inc_hint = infer_expense_incurred_date_iso(
                message=message,
                hints=entities,
                today=expense_incurred_date_mod.date.today(),
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

            expense_turn_message = message
            if wants_defer_leave_for_expense_submit(message):
                exp_block = wf_exp.get("expense_request") or {}
                stage_def = str(exp_block.get("stage") or "")
                if stage_def in ("review", "submit_confirm"):
                    expense_turn_message = "yes"
            if wants_expense_summary(message):
                exp_block = wf_exp.get("expense_request") or {}
                exp_items = list(exp_block.get("items") or [])
                pending = exp_block.get("pending_line")
                has_pending = isinstance(pending, dict) and pending.get("amount")
                if exp_items and not has_pending:
                    expense_turn_message = "শেষ"

            exp_pack = process_expense_turn(
                workflow_state=wf_exp,
                message=expense_turn_message,
                company_id=company_id,
                employee_id=employee_id,
                session_id=session.session_id,
                day_logged_total=day_logged,
                daily_cap=float(EXPENSE_DAY_CAP_BDT),
                pipeline_result=expense_pipe_result,
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
            if exp_pack.get("message_facts"):
                entities["expense_message_facts"] = exp_pack["message_facts"]
        elif is_expense_in_progress(wf_exp_gate) and (
            expense_workflow_interrupt or expense_side_interrupt
        ):
            expense_collecting_blocked = True
        log_step(
            trace_id,
            "expense_workflow_gate",
            {
                "blocked": expense_collecting_blocked,
                "intent": intent,
                "side_interrupt": expense_side_interrupt,
                "workflow_interrupt": expense_workflow_interrupt,
            },
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
                rid_s = str(rid or "").strip()
                # Avoid false positives like parsing "request" → "uest".
                if rid_s and not re.search(r"\d", rid_s):
                    rid_s = ""
                if rid_s:
                    st = self.crm.get_request_status(
                        rid_s,
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                    )
                    crm_payload.update(st)
                elif _asks_recent_leave_submission(message):
                    lv_st = read_leave_state(
                        getattr(session, "workflow_state", None) or {}
                    )
                    if lv_st.get("submission_id"):
                        crm_payload["leave_last_submission"] = {
                            "submission_id": str(lv_st.get("submission_id") or ""),
                            "submitted_at": str(lv_st.get("submitted_at") or ""),
                            "draft": dict(lv_st.get("draft") or {}),
                        }
                    else:
                        crm_payload["leave_not_submitted"] = True
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
                        wf_st = getattr(session, "workflow_state", None) or {}
                        exp_blk = wf_st.get("expense_request") or {}
                        if exp_blk.get("active"):
                            crm_payload["expense_wizard_active"] = True
                            crm_payload["expense_wizard_stage"] = str(
                                exp_blk.get("stage") or "collecting"
                            )
                            if exp_blk.get("submit_blocked_reason"):
                                crm_payload["expense_submit_blocked"] = str(
                                    exp_blk.get("submit_blocked_reason")
                                )
                        else:
                            crm_payload["expense_not_submitted"] = True
                else:
                    crm_payload["detail"] = "Missing request_id for status lookup."

            # LEGACY / OBSOLETE: per-turn single-amount day cap for auto-approve path.
            # Enterprise workflow uses expense_validation warnings + SUBMITTED outcome.
            if intent == INTENT_EXPENSE_CLAIM and not expense_collecting_blocked:
                if not entities.get("expense_workflow_submit"):
                    inc_iso = (entities.get("expense_incurred_date") or "").strip() or infer_expense_incurred_date_iso(
                        message=message,
                        hints=entities,
                        today=expense_incurred_date_mod.date.today(),
                    )
                    day_tot = self.crm.get_expense_day_approved_total(
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                        incurred_date_iso=inc_iso,
                    )
                    crm_context.update(day_tot)

            if (
                intent == INTENT_HR_POLICY
                and is_policy_kb_query(message)
                and not general_out_of_scope
                and not policy_complaint
            ):
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
                    message=message,
                    hints=entities,
                    today=expense_incurred_date_mod.date.today(),
                )
                breakdown = self.crm.get_expense_day_breakdown(
                    company_id=company_id,
                    employee_id=employee_id,
                    session_id=session.session_id,
                    incurred_date_iso=inc_iso,
                )
                day_items = list(breakdown.get("expense_day_items") or [])
                if not day_items:
                    last = (
                        (getattr(session, "workflow_state", None) or {}).get(
                            "expense_last_submission"
                        )
                        or {}
                    )
                    last_date = str(last.get("incurred_date_iso") or "").strip()
                    last_items = list(last.get("items") or [])
                    if last_items and (not last_date or last_date == inc_iso):
                        breakdown["expense_day_items"] = last_items
                        breakdown["expense_summary_reference_id"] = str(
                            last.get("reference_id") or ""
                        )
                        if not breakdown.get("expense_day_logged_total"):
                            breakdown["expense_day_logged_total"] = sum(
                                float(x.get("amount") or 0) for x in last_items
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
            elif expense_workflow_interrupt and intent == INTENT_HR_POLICY:
                decision = {
                    "outcome": "INFORMATIONAL",
                    "reason": (crm_payload.get("rules_answer") or "").strip()
                    or hr_policy_not_found_message(),
                    "rules_applied": ["EXPENSE_WORKFLOW_POLICY_INTERRUPT"],
                }
            elif expense_workflow_interrupt and intent == INTENT_LEAVE_BALANCE:
                decision = self.engine.evaluate(
                    intent=intent, entities=entities, crm_context=crm_context
                )
            elif (
                expense_collecting_blocked
                and not expense_workflow_interrupt
                and not expense_side_interrupt
            ):
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
                wf_exp_dec = getattr(session, "workflow_state", None) or {}
                q_exp = (
                    exp_pack.get("question")
                    or expense_pending_prompt(wf_exp_dec)
                    or "আজকের খরচের বিস্তারিত লিখুন (যেমন: lunch 100, bus 50)।"
                )
                decision = {
                    "outcome": "NEEDS_CLARIFICATION",
                    "reason": q_exp,
                    "rules_applied": rules_exp,
                }
            else:
                if intent == INTENT_EXPENSE_CLAIM and exp_pack.get("submitted"):
                    entities["expense_workflow_submit"] = True
                decision = self.engine.evaluate(
                    intent=intent, entities=entities, crm_context=crm_context
                )
            if general_out_of_scope:
                decision = {
                    "outcome": "INFORMATIONAL",
                    "reason": build_out_of_scope_message(
                        message,
                        context_lines=context_lines,
                        trace_id=trace_id,
                    ),
                    "rules_applied": ["OUT_OF_SCOPE_GENERAL"],
                }
            if context_clarification_msg:
                decision = {
                    "outcome": "NEEDS_CLARIFICATION",
                    "reason": context_clarification_msg,
                    "rules_applied": ["CONTEXT_CLARIFICATION"],
                }
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
                    wf_leave_sub = sub_result.workflow_state
                    if has_suspended_expense(wf_leave_sub):
                        wf_leave_sub = restore_suspended_expense(wf_leave_sub)
                        log_step(
                            trace_id,
                            "suspended_expense_restored_after_leave_submit",
                            {},
                        )
                    session.workflow_state = wf_leave_sub
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
            wf_post_msg = getattr(session, "workflow_state", None) or {}
            if (
                intent == INTENT_LEAVE_REQUEST
                and decision.get("outcome") == "SUBMITTED"
                and entities.get("leave_workflow_confirmed")
                and is_expense_in_progress(wf_post_msg)
            ):
                msg = _append_suspended_expense_resume_after_switch(
                    msg, wf_post_msg, user_message=message
                )
            if (
                intent == INTENT_EXPENSE_CLAIM
                and expense_collecting_blocked
                and not expense_workflow_interrupt
                and not expense_side_interrupt
                and not general_out_of_scope
                and exp_pack.get("question")
            ):
                msg = str(exp_pack["question"])
                rstatus = "needs_input"
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
                    lang=(
                        ((getattr(session, "workflow_state", None) or {}).get("expense_request") or {}).get(
                            "reply_language"
                        )
                    ),
                )
                rstatus = "success"
                wf_submitted = save_expense_last_submission(
                    getattr(session, "workflow_state", None) or {},
                    reference_id=sub_ref,
                    items=list(entities.get("expense_items") or []),
                    incurred_date_iso=str(entities.get("expense_incurred_date") or ""),
                )
                if has_suspended_leave(wf_submitted):
                    wf_submitted = restore_suspended_leave(
                        wf_submitted, force_active=True
                    )
                    log_step(trace_id, "suspended_leave_restored_after_expense_submit", {})
                wf_submitted = _persist_workflow_state(session, wf_submitted)
                if is_leave_in_progress(wf_submitted):
                    msg = _append_suspended_leave_resume_after_switch(
                        msg, wf_submitted, user_message=message
                    )
            # Friendly, human-toned LLM fallback for cases where the rules /
            # intent pipeline could not produce a specific answer:
            #   1. The user message did not match any HR intent (UNKNOWN).
            #   2. The user asked about a rule we don't have a section for
            #      (HR_POLICY with rules_mode == "no_match"), but not rag_no_hit
            #      (explicit KB miss copy — do not overwrite with chit-chat).
            # When the LLM is unavailable we keep the existing degraded text
            # so the user always gets *something*.
            used_conversational = False
            if policy_complaint and not response_finalized:
                user_lang = detect_user_language(message)
                if wizard_misroute_complaint and not is_irrelevant_answer_complaint(
                    message
                ):
                    if user_lang == "bn":
                        msg = (
                            "বুঝতে পারছি — আপনি ছুটির ফর্মের মধ্যে আলাদা একটা প্রশ্ন করেছিলেন, "
                            "আর সেটাকে ছুটির «কারণ» হিসেবে ধরে ফেলা ঠিক হয়নি। "
                            "এ ধরনের প্রশ্নের উত্তর আমাদের আপলোড করা পলিসিতে না থাকলে "
                            "HR-কে জিজ্ঞেস করুন; আমি শুধু ছুটি, খরচ, attendance ও "
                            "কোম্পানি নীতি নিয়ে সাহায্য করি।"
                        )
                    else:
                        msg = (
                            "I understand — you asked a separate question while the leave "
                            "form was open, and it should not have been saved as your leave reason. "
                            "If our uploaded policies do not cover your question, please check "
                            "with HR. I can help with leave, expenses, attendance, and company policy."
                        )
                elif user_lang == "bn":
                    msg = (
                        "দুঃখিত — আগের উত্তরটি আপনার প্রশ্নের সাথে মিলছিল না। "
                        "আপলোড করা পলিসিতে এই বিষয়ে স্পষ্ট তথ্য খুঁজে পাইনি। "
                        "নির্দিষ্ট পলিসির নাম বা বিষয় লিখে আবার জিজ্ঞাসা করুন, "
                        "অথবা HR-এর সাথে যোগাযোগ করুন।"
                    )
                else:
                    msg = (
                        "Sorry — my previous reply did not match your question. "
                        + hr_policy_not_found_message()
                    )
                rstatus = "success"
                crm_payload["rules_mode"] = "rag_no_hit"
                response_finalized = True
                used_conversational = True
                log_step(trace_id, "policy_complaint_acknowledged", {})

            if (
                intent == INTENT_UNKNOWN
                and decision.get("outcome") == "NEEDS_CLARIFICATION"
                and is_policy_kb_query(message)
                and not response_finalized
                and not policy_complaint
                and not general_out_of_scope
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
                and not context_clarification_msg
                and not general_out_of_scope
                and not rag_unknown_hit
                and not (
                    is_expense_in_progress(wf_for_conv)
                    and intent == INTENT_EXPENSE_CLAIM
                )
                and not (
                    is_expense_in_progress(wf_for_conv)
                    and not expense_side_interrupt
                )
                and not (
                    leave_workflow_interrupt
                    and intent in (INTENT_HR_POLICY, INTENT_LEAVE_BALANCE)
                )
                and not (
                    expense_workflow_interrupt
                    and intent == INTENT_HR_POLICY
                )
                and not (
                    is_leave_collecting(wf_for_conv) and not leave_side_interrupt
                )
                and not (
                    is_leave_in_progress(wf_for_conv)
                    and not leave_side_interrupt
                    and not leave_workflow_interrupt
                )
                and not (
                    is_expense_in_progress(wf_for_conv)
                    and not expense_side_interrupt
                    and not expense_workflow_interrupt
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
            expense_wizard_msg = any(
                str(r or "").startswith("EXPENSE_WORKFLOW_")
                and str(r) not in ("EXPENSE_WORKFLOW_POLICY_INTERRUPT",)
                for r in (decision.get("rules_applied") or [])
            )
            if (
                msg
                and not used_conversational
                and not expense_wizard_msg
                and (
                    (
                        intent == INTENT_HR_POLICY
                        and crm_payload.get("rules_mode") in ("full", "section", "rag", "rag_no_hit")
                    )
                    or rag_unknown_hit
                )
            ):
                reply_lang = detect_reply_language(message)
                aligned = align_policy_answer_language(
                    msg,
                    user_message=message,
                    trace_id=trace_id,
                )
                if aligned != msg:
                    log_step(
                        trace_id,
                        "rules_language_aligned",
                        {
                            "target_lang": reply_lang,
                            "chars": len(aligned),
                        },
                    )
                    msg = aligned
                rules_mode = str(crm_payload.get("rules_mode") or "")
                if rules_mode in ("full", "section", "rag", "rag_no_hit"):
                    footer_mode = "section" if rules_mode in ("rag", "rag_no_hit") else rules_mode
                    msg = msg.rstrip() + "\n\n" + _rules_footer(
                        mode=footer_mode, lang=reply_lang
                    )

            wf_policy_resume = getattr(session, "workflow_state", None) or {}
            if (
                msg
                and is_expense_paused(wf_policy_resume)
                and expense_workflow_interrupt
                and intent == INTENT_LEAVE_BALANCE
            ):
                msg = _append_expense_workflow_resume(msg, wf_policy_resume)

            wf_leave_resume = getattr(session, "workflow_state", None) or {}
            if (
                msg
                and is_leave_paused(wf_leave_resume)
                and leave_workflow_interrupt
                and intent == INTENT_LEAVE_BALANCE
            ):
                msg = _append_leave_workflow_resume(msg, wf_leave_resume)

            if (
                expense_side_interrupt
                and not response_finalized
                and not general_out_of_scope
                and is_expense_in_progress(getattr(session, "workflow_state", None) or {})
                and not (_is_confirmation_yes(message) or _is_confirmation_no(message))
                and (
                    _looks_like_chitchat(message, strict=True)
                    or _is_fresh_start_greeting(message)
                )
            ):
                wf_exp_side = getattr(session, "workflow_state", None) or {}
                expense_hint = (
                    _expense_paused_workflow_hint(detect_user_language(message))
                    if is_expense_paused(wf_exp_side)
                    else None
                )
                reply = conversational_reply(
                    message=message,
                    context_lines=context_lines,
                    trace_id=trace_id,
                    workflow_hint=expense_hint,
                )
                if reply:
                    wf_active = is_leave_in_progress(
                        getattr(session, "workflow_state", None) or {}
                    ) or is_expense_in_progress(
                        getattr(session, "workflow_state", None) or {}
                    )
                    if is_greeting_now and not wf_active:
                        user_lang = detect_user_language(message)
                        prefix = "হ্যালো! " if user_lang == "bn" else "Hi! "
                        msg = prefix + reply
                    else:
                        msg = reply
                    rstatus = "success"
                    response_finalized = True

            if (
                msg
                and expense_side_interrupt
                and is_expense_in_progress(getattr(session, "workflow_state", None) or {})
                and not (_is_confirmation_yes(message) or _is_confirmation_no(message))
            ):
                from chat.services.expense_workflow import wants_resume_or_show_expense

                if wants_resume_or_show_expense(message):
                    msg = _append_expense_workflow_resume(
                        msg, getattr(session, "workflow_state", None) or {}
                    )
                response_finalized = True

            leave_terminal_turn = (
                intent == INTENT_LEAVE_REQUEST
                and decision.get("outcome") == "SUBMITTED"
                and (
                    crm_payload.get("_deduped")
                    or entities.get("leave_workflow_confirmed")
                )
            )

            if (
                leave_side_interrupt
                and not response_finalized
                and not leave_terminal_turn
                and not policy_complaint
                and not general_out_of_scope
                and (
                    _looks_like_chitchat(message, strict=True)
                    or _is_fresh_start_greeting(message)
                )
            ):
                reply = conversational_reply(
                    message=message,
                    context_lines=context_lines,
                    trace_id=trace_id,
                )
                if reply:
                    wf_active = is_leave_in_progress(
                        getattr(session, "workflow_state", None) or {}
                    ) or is_expense_in_progress(
                        getattr(session, "workflow_state", None) or {}
                    )
                    if is_greeting_now and not wf_active:
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
                and not policy_complaint
                and is_leave_in_progress(getattr(session, "workflow_state", None) or {})
                and (
                    not leave_workflow_interrupt
                    or is_awaiting_leave_confirmation(
                        getattr(session, "workflow_state", None) or {}
                    )
                )
            ):
                if wants_resume_suspended_leave(message):
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
            trace_id=trace_id,
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

    @staticmethod
    def _assistant_text_for_translation(
        context_lines: list[str], *, target_lang: str
    ) -> str | None:
        """
        Pick the best prior assistant message to translate.
        When the user asks for Bangla, prefer the last policy-style answer over
        a short chit-chat line that happened to be the latest turn.
        """
        recent: list[str] = []
        for line in reversed(context_lines or []):
            if not line.startswith("Assistant:"):
                continue
            content = line[len("Assistant:") :].strip()
            if content:
                recent.append(content)
            if len(recent) >= 6:
                break
        if not recent:
            return None
        if target_lang != "bn":
            return recent[0]
        chitchat_markers = (
            "এই তো, এখানেই আছি",
            "just here whenever",
            "How can I help",
            "কিছু লাগলে বলুন",
        )
        for text in recent:
            low = text.lower()
            if any(m in text for m in chitchat_markers):
                continue
            if (
                "policy" in low
                or "termination" in low
                or "misconduct" in low
                or "gross" in low
                or re.search(r"^[-*#]", text, re.M)
            ):
                return text
        return recent[0]

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
        if is_irrelevant_answer_complaint(msg):
            return None
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


def _asks_recent_leave_submission(message: str) -> bool:
    low = (message or "").lower()
    raw = message or ""
    if not re.search(r"\b(leave|chuti|chhuti|request)\b", low) and "ছুটি" not in raw:
        return False
    return bool(
        re.search(
            r"(submit|জমা|joma|submitted).*(হয়েছে|হয়েছে|hoyeche|hoise|done|করা|করেছি|করেছ)",
            low,
        )
        or re.search(
            r"(হয়েছে|হয়েছে|hoyeche|hoise|done).*(submit|জমা|joma)",
            low,
        )
        or re.search(r"\bki\s+(submit|joma)\b", low)
        or re.search(r"(leave|chuti|chhuti|ছুটি|request).{0,35}(submit|joma)\s+hoyeche", low)
        or re.search(r"(submit|joma)\s+hoyeche", low)
    )


def _asks_recent_expense_submission(message: str) -> bool:
    low = (message or "").lower()
    raw = message or ""
    if not re.search(r"\b(expense|খরচ|খরচের)\b", low) and "খরচ" not in raw:
        return False
    return bool(
        re.search(
            r"(submit|জমা|joma|submitted).*(হয়েছে|হয়েছে|hoyeche|hoise|done|করা|করেছি|করেছ)",
            low,
        )
        or re.search(
            r"(হয়েছে|হয়েছে|hoyeche|hoise|done).*(submit|জমা|joma)",
            low,
        )
        or re.search(r"\bki\s+(submit|joma)\b", low)
        or re.search(r"(expense|খরচ).{0,25}(submit|joma)\s+hoyeche", low)
        or re.search(r"(submit|joma)\s+hoyeche", low)
    )


def _wants_resume_expense(message: str) -> bool:
    from chat.services.expense_workflow import (
        _is_confirmation_yes,
        wants_resume_or_show_expense,
    )

    if wants_resume_or_show_expense(message):
        return True
    if _is_confirmation_yes(message):
        return True
    low = (message or "").lower()
    if re.search(r"\b(submit|জমা)\s*(koro|kor|কর|দাও|দিন|করো)\b", low):
        return True
    if re.search(r"submit\s*koro", low):
        return True
    if re.search(r"(খরচ|expense).*(continue|চালু|শেষ|আবার)", low):
        return True
    if wants_expense_summary(message):
        return True
    return False


def _wants_resume_leave(message: str) -> bool:
    return wants_resume_suspended_leave(message)


def new_trace_id() -> str:
    return str(uuid.uuid4())
