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
from chat.services.ui_actions import build_ui_actions
from chat.services.session_turn_bridge import (
    intent_result_from_router_decision,
    is_decisive_router_decision,
    legacy_wizard_intent_fallback,
    pipeline_effects_from_router_decision,
    router_locked_intent,
    router_overrides_cold_start_intent,
    run_session_turn_router,
    should_override_wizard_intent,
)
from chat.services.session_turn_router import TurnKind
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
from chat.services.expense.expense_fsm import is_expense_review, read_expense_block
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
    wants_expense_spend_recap_query,
    wants_expense_summary,
    wants_resume_or_show_expense,
)
from chat.services.workflow_suspend import (
    clear_restore_leave_after_expense_submit,
    clear_suspended_expense,
    clear_suspended_leave,
    has_suspended_expense,
    has_suspended_leave,
    mark_restore_leave_after_expense_submit,
    restore_suspended_expense,
    restore_suspended_leave,
    should_restore_leave_after_expense_submit,
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
    format_today_date_reply,
    is_expense_entitlement_query,
    is_general_knowledge_out_of_scope,
    is_hr_today_date_query,
    is_irrelevant_answer_complaint,
    is_leave_wizard_misroute_complaint,
    is_off_topic_for_hr_assistant,
    is_policy_handbook_complaint,
    is_policy_interrupt_message as _is_policy_interrupt_message,
    is_policy_kb_query,
    is_rules_query,
)
from chat.services.legacy_wizard_intent import (
    detect_intent_during_expense_workflow as _detect_intent_during_expense_workflow,
    detect_intent_during_leave_workflow as _detect_intent_during_leave_workflow,
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
    from chat.services.leave_balance_intent import is_leave_balance_query

    return is_leave_balance_query(message)


def _any_wizard_active(workflow_state: dict[str, Any] | None) -> bool:
    return is_leave_in_progress(workflow_state) or is_expense_in_progress(
        workflow_state
    )


def _wizard_deterministic_fallback(
    message: str,
    workflow_state: dict[str, Any] | None,
    *,
    general_out_of_scope: bool,
) -> str | None:
    """Phase 1: never use conversational LLM while a wizard is active."""
    if not _any_wizard_active(workflow_state):
        return None
    lang = detect_user_language(message)
    if general_out_of_scope:
        return build_out_of_scope_message(message, lang=lang)
    if _looks_like_chitchat(message, strict=True) or _is_fresh_start_greeting(message):
        return None
    if is_expense_in_progress(workflow_state):
        from chat.services.expense.wizard_commands import expense_wizard_help_hint

        return expense_wizard_help_hint(lang)
    from chat.services.workflow_priority import leave_wizard_help_hint

    return leave_wizard_help_hint(lang)


def _expense_chitchat_workflow_hint(
    workflow_state: dict[str, Any] | None, *, user_message: str = ""
) -> str | None:
    """Soft LLM hint when user greets during an active/paused expense draft."""
    wf = workflow_state or {}
    if not is_expense_in_progress(wf):
        return None
    lang = detect_user_language(user_message)
    if is_expense_paused(wf):
        return _expense_paused_workflow_hint(lang)
    block = read_expense_block(wf)
    stage = str(block.get("stage") or "collecting")
    if lang == "en":
        return (
            "The user has an active expense draft (not submitted). "
            f"Current stage: {stage}. Reply naturally to their greeting — "
            "do NOT list expense commands or say 'draft is saved'. "
            "At most one short optional line that they can continue the expense when ready."
        )
    return (
        "User er expense draft active ache (submit hoyni). "
        f"Stage: {stage}. Greeting e naturally reply din — "
        "expense command list ba 'draft save ache' bolen na. "
        "Dorkar hole ek line e continue korte parben bole soft hint din."
    )


def _is_leave_application_message(message: str) -> bool:
    """New leave request phrasing — not the same as asking about leave *policy*."""
    if _is_policy_interrupt_message(message):
        return False
    from chat.services.workflow_navigation import is_leave_application_message

    return is_leave_application_message(message)


def _should_restore_suspended_leave_for_intent(
    message: str, workflow_state: dict[str, Any]
) -> bool:
    """Skip auto-restore when the user starts a fresh leave on a different date."""
    if not has_suspended_leave(workflow_state):
        return False
    if not _is_leave_application_message(message):
        return True
    from chat.services.leave_slot_extraction import extract_leave_slots

    ex = extract_leave_slots(message, skip_leave_phrase_gate=True)
    new_start = (
        str(ex.start_date.value)
        if ex.start_date.confidence == "high" and ex.start_date.value
        else ""
    )
    if not new_start:
        return True
    sl = dict(workflow_state.get("suspended_leave") or {})
    old_start = str((sl.get("draft") or {}).get("start_date") or "")
    if old_start and new_start != old_start:
        return False
    return True


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


def _soft_expense_pause_footer(
    workflow_state: dict[str, Any], *, user_message: str = ""
) -> str:
    """One-line reminder after a side answer — not the full wizard prompt."""
    from chat.services.expense.expense_fsm import is_expense_paused, read_expense_block

    if not is_expense_paused(workflow_state):
        return ""
    block = read_expense_block(workflow_state)
    if not list(block.get("items") or []):
        return ""
    lang = detect_user_language(user_message)
    if lang == "en":
        return (
            "\n\n_(Your expense draft is paused — you can continue it when ready.)_"
        )
    if lang == "banglish":
        return (
            "\n\n_(Expense draft pause ache — ready hole continue korte parben.)_"
        )
    return (
        "\n\n_(খরচের draft সাময়িক pause আছে — পরে চালিয়ে যেতে পারবেন।)_"
    )


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


def _apply_pre_router_navigation(
    session: Any,
    message: str,
    wf_state: dict[str, Any],
    *,
    is_cancel_now: bool,
    trace_id: str,
) -> dict[str, Any]:
    """Execute the router-owned pre-router navigation plan (rows N50–N56).

    Pattern matching + priority live in
    ``session_turn_router.plan_pre_router_navigation`` — this seam only
    persists each planned step and emits the observability log, so the
    orchestrator stays execution-only (TURN_ROUTER_SPEC §2 rule).
    """
    from chat.services.session_turn_router import plan_pre_router_navigation

    for step in plan_pre_router_navigation(message, wf_state, is_cancel=is_cancel_now):
        wf_state = _persist_workflow_state(session, step.state)
        log_step(trace_id, step.log_step, {"rule": step.rule})

    return wf_state


def _attach_ui_actions(
    envelope: dict[str, Any],
    workflow_state: dict[str, Any] | None,
    *,
    suppress_wizard_actions: bool = False,
) -> dict[str, Any]:
    resp = envelope.setdefault("response", {})
    if suppress_wizard_actions:
        resp["actions"] = []
    else:
        resp["actions"] = build_ui_actions(workflow_state)
    return envelope


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

    def _try_workflow_meta_short_circuit(
        self,
        *,
        session: Any,
        message: str,
        intent_result: dict[str, Any],
        trace_id: str,
    ) -> dict[str, Any] | None:
        """Handle suspended-leave corrections and read-only meta without wizard turns."""
        src = str(intent_result.get("source") or "")
        wf = dict(getattr(session, "workflow_state", None) or {})
        sid = session.session_id

        if intent_result.get("_block_message"):
            msg = str(intent_result["_block_message"])
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "decision": {"outcome": "NEEDS_CLARIFICATION", "reason": msg},
                "response": {"message": msg, "status": "success", "request_id": ""},
                "status": "success",
                "_session_id": sid,
            }

        from chat.services.leave.duplicate_choice import (
            handle_duplicate_leave_choice_turn,
            is_duplicate_leave_choice_pending,
        )

        if is_duplicate_leave_choice_pending(wf):
            dup_pack = handle_duplicate_leave_choice_turn(wf, message)
            if dup_pack and dup_pack.get("duplicate_choice") == "continue":
                wf = dup_pack.get("workflow_state") or wf
                session.workflow_state = wf
                session.save(update_fields=["workflow_state", "updated_at"])
                msg = str(dup_pack.get("question") or "")
                return {
                    "intent": INTENT_LEAVE_REQUEST,
                    "decision": {"outcome": "INFORMATIONAL", "reason": msg},
                    "response": {"message": msg, "status": "success", "request_id": ""},
                    "status": "success",
                    "_session_id": sid,
                }

        router_reason = str(intent_result.get("router_reason") or "")
        leave_correction_reasons = frozenset(
            {
                "P11_suspended_leave_correction",
                "P12a_reason_correction",
                "P12b_date_correction",
                "P12_leave_review_correction",
            }
        )
        if (
            "suspended_leave_correction" in src
            or "leave_draft_correction" in src
            or router_reason in leave_correction_reasons
        ) and str(intent_result.get("intent") or "") != INTENT_EXPENSE_CLAIM:
            from chat.services.suspended_leave_correction import apply_leave_draft_correction

            wf, body, changed = apply_leave_draft_correction(wf, message)
            if not changed:
                return None
            session.workflow_state = wf
            session.save(update_fields=["workflow_state", "updated_at"])
            log_step(trace_id, "leave_draft_correction_applied", {})
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "decision": {"outcome": "NEEDS_CLARIFICATION", "reason": body},
                "response": {"message": body, "status": "success", "request_id": ""},
                "status": "success",
                "_session_id": sid,
            }

        if "pending_leave_show" in src:
            from chat.services.leave_meta_queries import build_pending_leave_show_message

            msg = build_pending_leave_show_message(wf)
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "decision": {"outcome": "INFORMATIONAL", "reason": msg},
                "response": {"message": msg, "status": "success", "request_id": ""},
                "status": "success",
                "_session_id": sid,
            }

        if "leave_summary" in src:
            from chat.services.leave_meta_queries import build_leave_session_summary_message

            msg = build_leave_session_summary_message(wf)
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "decision": {"outcome": "INFORMATIONAL", "reason": msg},
                "response": {"message": msg, "status": "success", "request_id": ""},
                "status": "success",
                "_session_id": sid,
            }

        if "post_submit_leave_nav" in src:
            from chat.services.workflow_navigation import format_post_submit_leave_locked_message

            msg = format_post_submit_leave_locked_message(wf)
            return {
                "intent": INTENT_REQUEST_STATUS,
                "decision": {"outcome": "INFORMATIONAL", "reason": msg},
                "response": {"message": msg, "status": "success", "request_id": ""},
                "status": "success",
                "_session_id": sid,
            }

        if "leave_meta" in src:
            from chat.services.leave.session_action_memory import format_leave_meta_answer

            msg = format_leave_meta_answer(wf, message)
            return {
                "intent": INTENT_REQUEST_STATUS,
                "decision": {"outcome": "INFORMATIONAL", "reason": msg},
                "response": {"message": msg, "status": "success", "request_id": ""},
                "status": "success",
                "_session_id": sid,
            }

        if "expense_meta" in src:
            from chat.services.expense.session_action_memory import format_meta_question_answer
            from chat.services.translator import detect_user_language

            msg = format_meta_question_answer(
                wf, message, lang=detect_user_language(message)
            )
            if msg:
                return {
                    "intent": INTENT_EXPENSE_STATUS,
                    "decision": {"outcome": "INFORMATIONAL", "reason": msg},
                    "response": {"message": msg, "status": "success", "request_id": ""},
                    "status": "success",
                    "_session_id": sid,
                }

        if "cancel_expense_verify" in src:
            from chat.services.expense.expense_confirm import is_confirmation_yes

            block = read_expense_block(wf)
            if not is_expense_in_progress(wf) and not block.get("items"):
                msg = (
                    "এই session-এ **জমা দেওয়া** expense আছে — active draft **নেই**, "
                    "তাই cancel করা যাবে না।"
                )
            elif is_confirmation_yes(message):
                wf = deactivate_expense_session(wf)
                session.workflow_state = wf
                session.save(update_fields=["workflow_state", "updated_at"])
                msg = "Expense draft **বাতিল** করা হয়েছে।"
            else:
                msg = (
                    "আপনি কি নিশ্চিত যে **expense draft বাতিল** করতে চান? "
                    "নিশ্চিত হলে **হ্যাঁ** বা **yes** লিখুন।"
                )
            return {
                "intent": INTENT_EXPENSE_CLAIM,
                "decision": {
                    "outcome": "NEEDS_CLARIFICATION" if not is_confirmation_yes(message) else "INFORMATIONAL",
                    "reason": msg,
                },
                "response": {"message": msg, "status": "success", "request_id": ""},
                "status": "success",
                "_session_id": sid,
            }

        if "cancel_leave_verify" in src:
            from chat.services.leave_confirm import is_confirmation_yes
            from chat.services.leave_meta_queries import (
                clear_leave_cancel_verify_pending,
                is_leave_cancel_verify_pending,
                mark_leave_cancel_verify_pending,
            )
            from chat.services.workflow_suspend import clear_suspended_leave

            if is_leave_cancel_verify_pending(wf) and is_confirmation_yes(message):
                wf = clear_suspended_leave(clear_leave_cancel_verify_pending(wf))
                wf = deactivate_leave_session(wf)
                session.workflow_state = wf
                session.save(update_fields=["workflow_state", "updated_at"])
                msg = "ছুটির আবেদন **বাতিল** করা হয়েছে। আর কিছু লাগলে জানাবেন।"
                return {
                    "intent": INTENT_LEAVE_REQUEST,
                    "decision": {"outcome": "INFORMATIONAL", "reason": msg},
                    "response": {"message": msg, "status": "success", "request_id": ""},
                    "status": "success",
                    "_session_id": sid,
                }
            if not is_leave_cancel_verify_pending(wf):
                wf = mark_leave_cancel_verify_pending(wf)
                session.workflow_state = wf
                session.save(update_fields=["workflow_state", "updated_at"])
            msg = (
                "আপনি কি নিশ্চিত যে **ছুটির আবেদন বাতিল** করতে চান? "
                "নিশ্চিত হলে **হ্যাঁ** বা **yes** লিখুন; না হলে অন্য কিছু লিখুন।"
            )
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "decision": {"outcome": "NEEDS_CLARIFICATION", "reason": msg},
                "response": {"message": msg, "status": "success", "request_id": ""},
                "status": "success",
                "_session_id": sid,
            }

        if "submit_command" in src and is_leave_in_progress(wf):
            from chat.services.leave_confirm import (
                build_deferred_leave_return_prompt,
                is_confirmation_yes,
            )
            from chat.services.leave.normalization import normalize_leave_draft
            from chat.services.leave_draft_utils import apply_leave_draft_defaults
            from chat.services.leave_fsm import (
                STATUS_ACTIVE,
                apply_leave_state,
                mark_review_pending,
                read_leave_state,
            )
            from chat.services.leave_policies import get_company_leave_policy
            from chat.services.leave_slots import generate_question, get_missing_slots

            st = read_leave_state(wf)
            draft = dict(st.get("draft") or {})
            policy = get_company_leave_policy(
                getattr(session, "company_id", None) or "default"
            )
            normalize_leave_draft(draft)
            apply_leave_draft_defaults(draft, policy)
            missing = get_missing_slots(draft, policy=policy)
            if missing:
                slot = missing[0]
                wf = apply_leave_state(
                    wf,
                    draft=draft,
                    step=slot,
                    status=STATUS_ACTIVE,
                    review_pending=False,
                )
                session.workflow_state = wf
                session.save(update_fields=["workflow_state", "updated_at"])
                msg = generate_question(
                    slot, draft, remaining=len(missing), missing=missing
                )
                return {
                    "intent": INTENT_LEAVE_REQUEST,
                    "decision": {"outcome": "NEEDS_CLARIFICATION", "reason": msg},
                    "response": {"message": msg, "status": "success", "request_id": ""},
                    "status": "success",
                    "_session_id": sid,
                }
            wf = mark_review_pending(wf, draft)
            session.workflow_state = wf
            session.save(update_fields=["workflow_state", "updated_at"])
            if is_confirmation_yes(message):
                return None
            msg = build_deferred_leave_return_prompt(draft, message=message)
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "decision": {"outcome": "NEEDS_CLARIFICATION", "reason": msg},
                "response": {"message": msg, "status": "success", "request_id": ""},
                "status": "success",
                "_session_id": sid,
            }

        if "defer_leave_submit" in src or (
            wants_defer_expense_for_leave_submit(message) and has_suspended_leave(wf)
        ):
            from chat.services.leave_fsm import STATUS_ACTIVE, apply_leave_state, read_leave_state

            wf = switch_active_expense_to_suspended_leave(wf)
            st = read_leave_state(wf)
            draft = dict(st.get("draft") or {})
            wf = apply_leave_state(
                wf,
                draft=draft,
                step=st.get("step"),
                review_pending=True,
                status=STATUS_ACTIVE,
            )
            session.workflow_state = wf
            session.save(update_fields=["workflow_state", "updated_at"])
            from chat.services.leave_confirm import build_deferred_leave_return_prompt

            msg = build_deferred_leave_return_prompt(draft, message=message)
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "decision": {"outcome": "NEEDS_CLARIFICATION", "reason": msg},
                "response": {"message": msg, "status": "success", "request_id": ""},
                "status": "success",
                "_session_id": sid,
            }

        return None

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
        from chat.services.expense.done_intent_llm import clear_done_intent_cache
        from chat.services.llm_client import clear_llm_trace_state

        clear_done_intent_cache(trace_id)
        clear_llm_trace_state(trace_id)

        session = self.memory.get_or_create_session(
            company_id=company_id,
            employee_id=employee_id,
            session_id=session_id or "",
        )
        context_lines = self.memory.recent_context_lines(session)

        if is_hr_today_date_query(message):
            lang = detect_user_language(message)
            today_iso = expense_incurred_date_mod.date.today().isoformat()
            msg = format_today_date_reply(today_iso=today_iso, lang=lang)
            self.memory.append(session, "user", message)
            self.memory.append(session, "assistant", msg)
            wf_state = getattr(session, "workflow_state", None) or {}
            return _attach_ui_actions(
                {
                    "trace_id": trace_id,
                    "intent": INTENT_HR_POLICY,
                    "entities": {"calendar_date": today_iso},
                    "decision": {
                        "outcome": "INFORMATIONAL",
                        "reason": "Today's calendar date.",
                        "rules_applied": ["HR_TODAY_DATE_QUERY"],
                    },
                    "response": {
                        "message": msg,
                        "status": "success",
                        "request_id": "",
                    },
                    "status": "success",
                    "_session_id": session.session_id,
                },
                wf_state,
            )

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
                return _attach_ui_actions(
                    {
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
                    },
                    wf_state,
                )

        wf_state = getattr(session, "workflow_state", None) or {}
        from chat.services.leave.duplicate_choice import (
            handle_duplicate_leave_choice_turn,
            is_duplicate_leave_choice_pending,
        )
        from chat.services.leave_meta_queries import wants_cancel_leave_command

        from chat.services.expense.wizard_commands import wants_cancel_expense_command

        if is_duplicate_leave_choice_pending(wf_state):
            dup_pack = handle_duplicate_leave_choice_turn(wf_state, message)
            if dup_pack and dup_pack.get("duplicate_choice") == "continue":
                wf_state = dup_pack.get("workflow_state") or wf_state
                session.workflow_state = wf_state
                session.save(update_fields=["workflow_state", "updated_at"])
                msg_dup = str(dup_pack.get("question") or "")
                self.memory.append(session, "user", message)
                self.memory.append(session, "assistant", msg_dup)
                return _attach_ui_actions(
                    {
                        "trace_id": trace_id,
                        "intent": INTENT_LEAVE_REQUEST,
                        "entities": {},
                        "decision": {
                            "outcome": "INFORMATIONAL",
                            "reason": msg_dup,
                            "rules_applied": ["DUPLICATE_LEAVE_CONTINUE"],
                        },
                        "response": {
                            "message": msg_dup,
                            "status": "success",
                            "request_id": "",
                        },
                        "status": "success",
                        "_session_id": session.session_id,
                    },
                    wf_state,
                )

        from chat.services.leave_confirm import is_confirmation_yes as leave_confirm_yes_early

        if is_awaiting_leave_confirmation(wf_state) and leave_confirm_yes_early(message):
            from chat.services.leave_workflow import process_leave_turn as _process_leave_turn_early

            lv_early = _process_leave_turn_early(
                workflow_state=wf_state,
                message=message,
                entities={},
                company_id=company_id,
                trace_id=trace_id,
            )
            wf_state = lv_early.get("workflow_state") or wf_state
            if lv_early.get("confirmed_submit"):
                from chat.services.leave_submission_service import LeaveSubmissionService

                sub_svc = LeaveSubmissionService(self.crm)
                sub_result = sub_svc.submit_confirmed_leave(
                    workflow_state=wf_state,
                    company_id=company_id,
                    employee_id=employee_id,
                    session_id=session.session_id,
                    entities=dict(lv_early.get("merged_entities") or {}),
                    decision={"outcome": "SUBMITTED"},
                    trace_id=trace_id,
                    idempotency_key=idempotency_key,
                )
                wf_state = sub_result.workflow_state
            session.workflow_state = wf_state
            session.save(update_fields=["workflow_state", "updated_at"])
            q_early = lv_early.get("question") or ""
            if lv_early.get("confirmed_submit"):
                msg_early = "ছুটির আবেদন **জমা** হয়েছে।"
            else:
                msg_early = q_early or "আরও একটু তথ্য দরকার।"
            self.memory.append(session, "user", message)
            self.memory.append(session, "assistant", msg_early)
            return _attach_ui_actions(
                {
                    "trace_id": trace_id,
                    "intent": INTENT_LEAVE_REQUEST,
                    "entities": dict(lv_early.get("merged_entities") or {}),
                    "decision": {
                        "outcome": "SUBMITTED"
                        if lv_early.get("confirmed_submit")
                        else "NEEDS_CLARIFICATION",
                        "reason": msg_early,
                        "rules_applied": ["LEAVE_CONFIRM_EARLY"],
                    },
                    "response": {
                        "message": msg_early,
                        "status": "success",
                        "request_id": "",
                    },
                    "status": "success",
                    "_session_id": session.session_id,
                },
                wf_state,
            )

        if wants_cancel_expense_command(message):
            from chat.services.expense.expense_confirm import is_confirmation_yes

            block = read_expense_block(wf_state)
            if not is_expense_in_progress(wf_state) and not block.get("items"):
                msg_ce = (
                    "এই session-এ **জমা দেওয়া** expense আছে — active draft **নেই**, "
                    "তাই cancel করা যাবে না।"
                )
            elif is_confirmation_yes(message):
                wf_state = deactivate_expense_session(wf_state)
                session.workflow_state = wf_state
                session.save(update_fields=["workflow_state", "updated_at"])
                msg_ce = "Expense draft **বাতিল** করা হয়েছে।"
            else:
                msg_ce = (
                    "আপনি কি নিশ্চিত যে **expense draft বাতিল** করতে চান? "
                    "নিশ্চিত হলে **হ্যাঁ** বা **yes** লিখুন।"
                )
            self.memory.append(session, "user", message)
            self.memory.append(session, "assistant", msg_ce)
            return _attach_ui_actions(
                {
                    "trace_id": trace_id,
                    "intent": INTENT_EXPENSE_CLAIM,
                    "entities": {},
                    "decision": {
                        "outcome": (
                            "INFORMATIONAL"
                            if is_confirmation_yes(message)
                            or (
                                not is_expense_in_progress(wf_state)
                                and not block.get("items")
                            )
                            else "NEEDS_CLARIFICATION"
                        ),
                        "reason": msg_ce,
                    },
                    "response": {
                        "message": msg_ce,
                        "status": "success",
                        "request_id": "",
                    },
                    "status": "success",
                    "_session_id": session.session_id,
                },
                wf_state,
            )

        if wants_cancel_leave_command(message) and is_leave_submission_locked(
            wf_state
        ):
            st_sub = read_leave_state(wf_state)
            ref = str(st_sub.get("submission_id") or "")
            msg_cancel = (
                "এই leave request **ইতিমধ্যে জমা** হয়েছে"
                + (f" (ref: **{ref}**)" if ref else "")
                + " — chat থেকে **cancel** করা যাবে না।"
            )
            self.memory.append(session, "user", message)
            self.memory.append(session, "assistant", msg_cancel)
            return _attach_ui_actions(
                {
                    "trace_id": trace_id,
                    "intent": INTENT_LEAVE_REQUEST,
                    "entities": {},
                    "decision": {
                        "outcome": "INFORMATIONAL",
                        "reason": msg_cancel,
                        "rules_applied": ["LEAVE_ALREADY_SUBMITTED_NO_CANCEL"],
                    },
                    "response": {
                        "message": msg_cancel,
                        "status": "success",
                        "request_id": ref,
                    },
                    "status": "success",
                    "_session_id": session.session_id,
                },
                wf_state,
            )

        from chat.services.workflow_navigation import is_leave_navigation_phrase

        if (
            is_leave_submission_locked(wf_state)
            and (
                _should_short_circuit_submitted_leave(message)
                or is_leave_navigation_phrase(message)
            )
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
            return _attach_ui_actions(
                {
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
                },
                wf_state,
            )

        balance_probe = _leave_balance_probe(message)
        is_greeting_now = _is_fresh_start_greeting(message)
        is_cancel_now = _is_cancel_form_request(message)
        wizard_dismissed_reason: str | None = None
        leave_nav_no_session = False
        leave_side_interrupt = False
        leave_workflow_interrupt = False
        expense_side_interrupt = False
        expense_workflow_interrupt = False
        response_finalized = False

        # Deterministic navigation phase (resume / restore / switch) — runs before
        # the session router so the router classifies against an up-to-date snapshot.
        wf_state = _apply_pre_router_navigation(
            session,
            message,
            wf_state,
            is_cancel_now=is_cancel_now,
            trace_id=trace_id,
        )

        from chat.services.workflow_priority import (
            expense_query_should_suspend_leave,
        )

        log_step(
            trace_id,
            "intent_detection_start",
            {"user_message": message, "session_id": session.session_id},
        )

        _router_snap, session_router_decision = run_session_turn_router(
            message,
            workflow_state=wf_state,
            context_lines=context_lines,
            balance_probe=balance_probe,
            is_greeting=is_greeting_now,
            is_cancel=is_cancel_now,
            trace_id=trace_id,
        )
        session_router_effects = pipeline_effects_from_router_decision(
            session_router_decision,
            message=message,
            context_lines=context_lines,
        )
        log_step(
            trace_id,
            "session_turn_routed",
            {
                "turn_kind": session_router_decision.turn_kind.value,
                "intent": session_router_decision.intent,
                "handler_id": session_router_decision.handler_id,
                "reason": session_router_decision.reason,
                "matched_predicate": session_router_decision.matched_predicate,
            },
        )

        wizard_active_for_router = is_leave_in_progress(wf_state) or is_expense_in_progress(
            wf_state
        )
        if should_override_wizard_intent(
            session_router_decision, wizard_active=wizard_active_for_router
        ):
            intent_result = intent_result_from_router_decision(session_router_decision)
            log_step(trace_id, "intent_detection_session_router", intent_result)
        elif wizard_active_for_router:
            intent_result = legacy_wizard_intent_fallback(
                message,
                wf_state,
                balance_probe=balance_probe,
                trace_id=trace_id,
            )
            log_step(trace_id, "intent_detection_session_router_legacy_fallback", intent_result)
        elif is_leave_in_progress(wf_state):
            intent_result = _detect_intent_during_leave_workflow(
                message, wf_state, balance_probe=balance_probe, trace_id=trace_id
            )
            log_step(trace_id, "intent_detection_skipped_leave_active", intent_result)
        elif is_expense_in_progress(wf_state):
            intent_result = _detect_intent_during_expense_workflow(
                message, wf_state, balance_probe=balance_probe, trace_id=trace_id
            )
            log_step(trace_id, "intent_detection_skipped_expense_active", intent_result)
        else:
            intent_result = self.intents.detect(message, trace_id)
            if router_overrides_cold_start_intent(session_router_decision):
                intent_result = intent_result_from_router_decision(session_router_decision)
                log_step(trace_id, "intent_detection_session_router_cold", intent_result)
        intent = intent_result["intent"]
        # When the session router decisively locked an intent (non-P99 wizard turn),
        # skip the legacy hard-coded post-gates below — they only duplicate router
        # P-rows and re-introduce the parallel-layer conflicts the router replaces.
        router_locked = router_locked_intent(intent_result)
        if (
            "leave_nav_no_session" in str(intent_result.get("source") or "")
            or intent_result.get("leave_nav_no_session")
        ):
            leave_nav_no_session = True
        if (
            not router_locked
            and expense_query_should_suspend_leave(message)
            and not is_expense_in_progress(wf_state)
        ):
            from chat.services.expense.expense_total_dispute import (
                is_expense_total_check_query,
            )
            from chat.services.expense_extraction import message_contains_expense_claim_lines
            from chat.services.expense.session_action_memory import (
                wants_expense_meta_question as _wants_expense_meta,
            )

            if message_contains_expense_claim_lines(message):
                forced = INTENT_EXPENSE_CLAIM
            elif is_expense_total_check_query(message) or _wants_expense_meta(message):
                forced = INTENT_EXPENSE_STATUS
            else:
                forced = INTENT_EXPENSE_DAY_SUMMARY
            intent = forced
            intent_result = {
                **intent_result,
                "intent": forced,
                "source": (intent_result.get("source") or "intent")
                + "+expense_query_priority",
            }
        if is_expense_entitlement_query(message) and not router_locked:
            intent = INTENT_HR_POLICY
            intent_result = {
                **intent_result,
                "intent": INTENT_HR_POLICY,
                "source": (intent_result.get("source") or "intent")
                + "+entitlement_policy",
            }
        from chat.services.expense.session_action_memory import (
            wants_expense_meta_question,
            wants_expense_pre_submit_review,
        )

        if wants_expense_meta_question(message) and not router_locked:
            intent = INTENT_EXPENSE_STATUS
            intent_result = {
                **intent_result,
                "intent": INTENT_EXPENSE_STATUS,
                "source": (intent_result.get("source") or "intent") + "+expense_meta",
            }
        from chat.services.hr_query_classifier import (
            QUERY_CHITCHAT,
            apply_hr_query_to_intent,
            build_hr_query_context,
            classify_hr_query,
            clear_hr_query_cache,
            decision_suppresses_out_of_scope,
            hr_query_llm_allowed_during_wizard,
        )

        wizard_active_for_hr = is_leave_in_progress(wf_state) or is_expense_in_progress(
            wf_state
        )
        clear_hr_query_cache()
        hr_query_decision = classify_hr_query(
            message,
            context=build_hr_query_context(wf_state),
            trace_id=trace_id,
            use_llm=True,
            wizard_side_llm=wizard_active_for_hr
            and hr_query_llm_allowed_during_wizard(message, wf_state),
        )
        intent, intent_result = apply_hr_query_to_intent(
            intent,
            intent_result,
            hr_query_decision,
            message=message,
            router_locked=router_locked,
        )
        if wants_expense_pre_submit_review(message) and not router_locked:
            intent = INTENT_EXPENSE_STATUS
            intent_result = {
                **intent_result,
                "intent": INTENT_EXPENSE_STATUS,
                "source": (intent_result.get("source") or "intent")
                + "+expense_pre_submit_review",
            }
        log_step(
            trace_id,
            "hr_query_classified",
            {
                "query_kind": hr_query_decision.query_kind,
                "source": hr_query_decision.source,
                "in_hr_scope": hr_query_decision.in_hr_scope,
                "date_reference": hr_query_decision.date_reference,
            },
        )
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
            and not _is_policy_interrupt_message(message)
            and not policy_complaint
        )
        if decision_suppresses_out_of_scope(hr_query_decision):
            general_out_of_scope = False
        if wizard_active_gate and (
            hr_query_decision.query_kind == QUERY_CHITCHAT
            or _looks_like_chitchat(message, strict=True)
        ):
            from chat.services.policy_intent_helpers import (
                is_general_knowledge_out_of_scope,
            )

            if not is_general_knowledge_out_of_scope(message):
                general_out_of_scope = False
        if session_router_effects.force_general_out_of_scope:
            general_out_of_scope = True
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
        if (
            is_decisive_router_decision(
                session_router_decision, wizard_active=wizard_active_for_router
            )
            and session_router_effects.workflow_turn
        ):
            workflow_turn = session_router_effects.workflow_turn
            log_step(
                trace_id,
                "workflow_turn_classified",
                {"turn_type": workflow_turn, "source": "session_turn_router"},
            )
        elif is_leave_in_progress(wf_state) or is_expense_in_progress(wf_state):
            exp_block = read_expense_block(wf_state) if is_expense_in_progress(wf_state) else {}
            exp_draft_block = read_expense_block(wf_state)
            workflow_turn = classify_workflow_turn(
                message,
                leave_active=is_leave_in_progress(wf_state),
                expense_active=is_expense_in_progress(wf_state),
                pending_leave_step=(
                    pending_step(wf_state) if is_leave_in_progress(wf_state) else None
                ),
                pending_expense_step=str(exp_block.get("pending_step") or ""),
                leave_review_pending=is_awaiting_leave_confirmation(wf_state),
                expense_review_pending=bool(
                    exp_block.get("active") and is_expense_review(exp_block)
                ),
                balance_probe=balance_probe,
                has_suspended_expense=has_suspended_expense(wf_state),
                has_expense_draft=bool(exp_draft_block.get("items")),
            )
            log_step(
                trace_id,
                "workflow_turn_classified",
                {"turn_type": workflow_turn},
            )

        from chat.services.leave_meta_queries import wants_cancel_leave_command
        from chat.services.expense.wizard_commands import wants_cancel_expense_command

        if wants_cancel_leave_command(message) or wants_cancel_expense_command(message):
            is_cancel_now = False

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
                if (
                    is_leave_collecting(wf_state)
                    or is_awaiting_leave_confirmation(wf_state)
                ) and not is_leave_paused(wf_state):
                    session.workflow_state = pause_leave_session(wf_state)
                    session.save(update_fields=["workflow_state", "updated_at"])
                    wf_state = session.workflow_state or {}
                    log_step(trace_id, "leave_wizard_paused_for_balance", {})
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
                if intent == INTENT_EXPENSE_DAY_SUMMARY and is_expense_paused(wf_state):
                    wf_state = _persist_workflow_state(
                        session, resume_expense_session(wf_state)
                    )
                    log_step(trace_id, "expense_wizard_resumed_for_summary", {})
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
            elif intent == INTENT_UNKNOWN and not leave_nav_no_session:
                if not policy_complaint and (
                    workflow_turn == TURN_CHITCHAT or general_out_of_scope
                ):
                    exp_clarify = (
                        read_expense_block(wf_state)
                        if is_expense_in_progress(wf_state)
                        else {}
                    )
                    clarify_active = (
                        str(exp_clarify.get("pending_step") or "").strip().lower()
                        == "clarify"
                    )
                    if clarify_active:
                        from chat.services.expense.clarify import (
                            looks_like_clarify_reply_signal,
                        )

                        if looks_like_clarify_reply_signal(message):
                            clarify_active = False
                    if not clarify_active:
                        from chat.services.expense.clarify_praise import (
                            looks_like_wizard_praise_message,
                        )

                        if looks_like_wizard_praise_message(
                            message
                        ) or looks_like_expense_wizard_continuation(message):
                            clarify_active = True
                    if not is_expense_paused(wf_state) and not is_greeting_now:
                        if not clarify_active:
                            wf_state = _persist_workflow_state(
                                session, pause_expense_session(wf_state)
                            )
                            log_step(
                                trace_id, "expense_wizard_paused_for_side_question", {}
                            )
                    if not clarify_active:
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
            and not router_locked_intent(intent_result)
            and is_leave_collecting(wf_gate)
            and intent
            not in (INTENT_EXPENSE_DAY_SUMMARY, INTENT_EXPENSE_STATUS, INTENT_EXPENSE_CLAIM)
            and not expense_query_should_suspend_leave(message)
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
                from chat.services.expense.expense_confirm import looks_like_expense_correction

                expense_correction_turn = (
                    workflow_turn == TURN_CORRECTION
                    and looks_like_expense_correction(message)
                    and (
                        has_suspended_expense(wf_gate)
                        or is_expense_in_progress(wf_gate)
                        or bool(read_expense_block(wf_gate).get("items"))
                    )
                )
                if expense_correction_turn:
                    intent = INTENT_EXPENSE_CLAIM
                    intent_result = {
                        **intent_result,
                        "intent": INTENT_EXPENSE_CLAIM,
                        "source": (intent_result.get("source") or "intent")
                        + "+expense_correction_priority",
                    }
                else:
                    intent = INTENT_LEAVE_REQUEST
                    intent_result = {
                        **intent_result,
                        "intent": INTENT_LEAVE_REQUEST,
                        "source": (intent_result.get("source") or "intent")
                        + "+leave_workflow_lock",
                    }
        from chat.services.expense.wizard_commands import (
            is_expense_wizard_command,
            wants_expense_submit_command,
        )

        from chat.services.expense.session_action_memory import (
            looks_like_submitted_expense_correction_attempt as _submitted_edit_block,
            wants_expense_meta_question as _wants_expense_meta_lock,
            wants_post_submit_edit_question as _wants_post_submit_edit,
        )

        expense_wizard_command_turn = wants_expense_submit_command(
            message
        ) or is_expense_wizard_command(message)
        expense_meta_turn = (
            _wants_expense_meta_lock(message)
            or _wants_post_submit_edit(message)
            or _submitted_edit_block(wf_gate, message)
        )

        from chat.services.leave_confirm import (
            is_confirmation_yes as leave_is_confirmation_yes,
            wants_leave_submit_command,
        )

        leave_confirm_turn = (
            is_awaiting_leave_confirmation(wf_gate) and leave_is_confirmation_yes(message)
        ) or (
            is_leave_in_progress(wf_gate) and wants_leave_submit_command(message)
        )
        if leave_confirm_turn:
            intent = INTENT_LEAVE_REQUEST
            intent_result = {
                **intent_result,
                "intent": INTENT_LEAVE_REQUEST,
                "source": (intent_result.get("source") or "intent") + "+leave_confirm_priority",
            }
        if (
            wizard_dismissed_reason is None
            and not router_locked_intent(intent_result)
            and is_expense_in_progress(wf_gate)
            and not expense_meta_turn
            and not leave_confirm_turn
            and (
                expense_wizard_command_turn
                or intent
                not in (
                    INTENT_EXPENSE_DAY_SUMMARY,
                    INTENT_EXPENSE_STATUS,
                    INTENT_REQUEST_STATUS,
                    INTENT_LEAVE_BALANCE,
                    INTENT_HR_POLICY,
                    INTENT_LEAVE_REQUEST,
                    INTENT_WFH_REQUEST,
                    INTENT_ATTENDANCE_CORRECTION,
                    INTENT_APPROVAL_ESCALATION,
                )
            )
            and (
                expense_wizard_command_turn
                or intent == INTENT_UNKNOWN
                or (
                    workflow_turn is not None
                    and is_workflow_continuation_turn(workflow_turn)
                )
                or (
                    (_is_confirmation_yes(message) or _is_confirmation_no(message) or wants_expense_summary(message))
                    and intent != INTENT_HR_POLICY
                    and intent != INTENT_LEAVE_BALANCE
                )
            )
            and not expense_side_interrupt
            and not expense_workflow_interrupt
            and not leave_nav_no_session
            and not general_out_of_scope
        ):
            intent = INTENT_EXPENSE_CLAIM
            intent_result = {
                **intent_result,
                "intent": INTENT_EXPENSE_CLAIM,
                "source": (intent_result.get("source") or "intent") + "+expense_workflow_lock",
            }
        from chat.services.expense_extraction import message_contains_expense_claim_lines

        if (
            wizard_dismissed_reason is None
            and not is_expense_in_progress(wf_gate)
            and _strong_expense_claim(message)
        ):
            intent = INTENT_EXPENSE_CLAIM
            intent_result = {
                **intent_result,
                "intent": INTENT_EXPENSE_CLAIM,
                "source": (intent_result.get("source") or "intent") + "+expense_start_heuristic",
            }
        elif (
            wizard_dismissed_reason is None
            and not is_expense_in_progress(wf_gate)
            and (
                wants_post_submit_expense_summary(message)
                or _strong_expense_day_summary(message)
                or wants_expense_spend_recap_query(message)
            )
            and not message_contains_expense_claim_lines(message)
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
            and wants_expense_submit_command(message)
        ):
            intent = INTENT_EXPENSE_CLAIM
            intent_result = {
                **intent_result,
                "intent": INTENT_EXPENSE_CLAIM,
                "source": (intent_result.get("source") or "intent") + "+expense_submit_heuristic",
            }
        elif wizard_dismissed_reason is None and _asks_recent_leave_submission(message):
            intent = INTENT_REQUEST_STATUS
            intent_result = {
                **intent_result,
                "intent": INTENT_REQUEST_STATUS,
                "source": (intent_result.get("source") or "intent") + "+leave_submit_status_heuristic",
            }
        elif wizard_dismissed_reason is None and (
            _asks_recent_expense_submission(message)
            or _asks_expense_ref_or_status(message)
        ):
            intent = INTENT_EXPENSE_STATUS
            intent_result = {
                **intent_result,
                "intent": INTENT_EXPENSE_STATUS,
                "source": (intent_result.get("source") or "intent") + "+expense_submit_status_heuristic",
            }
        elif wizard_dismissed_reason is None:
            from chat.services.expense.session_action_memory import (
                wants_expense_meta_question,
            )

            if wants_expense_meta_question(message):
                intent = INTENT_EXPENSE_STATUS
                intent_result = {
                    **intent_result,
                    "intent": INTENT_EXPENSE_STATUS,
                    "source": (intent_result.get("source") or "intent")
                    + "+expense_meta_heuristic",
                }
            else:
                from chat.services.expense.expense_total_dispute import (
                    is_expense_total_check_query,
                )

                if is_expense_in_progress(wf_gate) and is_expense_total_check_query(
                    message
                ):
                    intent = INTENT_EXPENSE_STATUS
                    intent_result = {
                        **intent_result,
                        "intent": INTENT_EXPENSE_STATUS,
                        "source": (intent_result.get("source") or "intent")
                        + "+expense_total_check_heuristic",
                    }

        duplicate_leave_early_msg: str | None = (
            session_router_effects.duplicate_leave_message
        )
        context_clarification_msg: str | None = None
        if duplicate_leave_early_msg:
            intent = INTENT_LEAVE_REQUEST
            intent_result = {
                **intent_result,
                "intent": INTENT_LEAVE_REQUEST,
                "confidence": 0.99,
                "source": (intent_result.get("source") or "intent")
                + "+duplicate_leave_early",
            }
            from chat.services.leave.duplicate_choice import (
                is_duplicate_leave_choice_pending,
                mark_duplicate_leave_choice_pending,
            )
            from chat.services.leave_meta_queries import _target_date_range_from_leave_message

            wf_dup = getattr(session, "workflow_state", None) or {}
            if not is_duplicate_leave_choice_pending(wf_dup):
                target_rng = _target_date_range_from_leave_message(message)
                if target_rng:
                    wf_dup = mark_duplicate_leave_choice_pending(
                        wf_dup,
                        target_start=target_rng[0],
                        target_end=target_rng[1],
                    )
                    session.workflow_state = wf_dup
                    session.save(update_fields=["workflow_state", "updated_at"])
                    wf_state = wf_dup
                    log_step(trace_id, "duplicate_leave_choice_pending_marked", {})
        elif session_router_effects.context_clarification_message:
            context_clarification_msg = (
                session_router_effects.context_clarification_message
            )
            intent = INTENT_UNKNOWN
            intent_result = {
                **intent_result,
                "intent": INTENT_UNKNOWN,
                "source": (intent_result.get("source") or "intent")
                + "+context_clarification",
            }
            log_step(trace_id, "context_clarification_asked", {})
        elif wizard_dismissed_reason is None and not general_out_of_scope:
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
            ) and not duplicate_leave_early_msg:
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

        if wizard_dismissed_reason is None and "defer_expense_submit" in str(
            intent_result.get("source") or ""
        ):
            wf_flag = mark_restore_leave_after_expense_submit(
                getattr(session, "workflow_state", None) or {}
            )
            session.workflow_state = wf_flag
            session.save(update_fields=["workflow_state", "updated_at"])
            wf_state = session.workflow_state or {}

        log_step(trace_id, "entity_extraction_start", {})
        wf_ent = getattr(session, "workflow_state", None) or {}
        use_rules_entities = (
            is_leave_in_progress(wf_ent)
            and workflow_turn is not None
            and is_workflow_continuation_turn(workflow_turn)
            and not _is_policy_interrupt_message(message)
        )
        from chat.services.expense.entity_pipeline import ExpenseEntityPipeline
        from chat.services.expense.llm_gate import expense_extraction_should_use_llm
        from chat.services.leave.entity_pipeline import LeaveEntityPipeline
        from chat.services.leave.llm_gate import leave_extraction_should_use_llm

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
            use_llm = leave_extraction_should_use_llm(
                message, workflow_turn=workflow_turn
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
            use_llm = expense_extraction_should_use_llm(
                message, workflow_turn=workflow_turn
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

        meta_short_circuit = self._try_workflow_meta_short_circuit(
            session=session,
            message=message,
            intent_result=intent_result,
            trace_id=trace_id,
        )
        if meta_short_circuit is not None:
            self.memory.append(session, "user", message)
            self.memory.append(
                session,
                "assistant",
                meta_short_circuit["response"]["message"],
            )
            return _attach_ui_actions(meta_short_circuit, session.workflow_state)

        if wizard_dismissed_reason is None and intent == INTENT_LEAVE_REQUEST:
            wf_resume = getattr(session, "workflow_state", None) or {}
            src = str(intent_result.get("source") or "")
            if re.search(
                r"(?:^|\b)(?:আবার|abar|again)\b",
                message or "",
                re.I | re.UNICODE,
            ) and _is_leave_application_message(message):
                from chat.services.leave_meta_queries import check_overlapping_submitted_leave

                overlap_msg = check_overlapping_submitted_leave(wf_resume, message)
                if not overlap_msg:
                    wf_resume = deactivate_expense_session(
                        clear_suspended_expense(wf_resume)
                    )
                    wf_resume = _persist_workflow_state(session, wf_resume)
                    log_step(trace_id, "expense_cleared_for_fresh_leave_restart", {})
            if (
                has_suspended_leave(wf_resume)
                and not is_leave_in_progress(wf_resume)
                and not _should_restore_suspended_leave_for_intent(message, wf_resume)
            ):
                wf_resume = _persist_workflow_state(
                    session, clear_suspended_leave(wf_resume)
                )
                log_step(trace_id, "suspended_leave_cleared_for_new_request", {})
            if (
                has_suspended_leave(wf_resume)
                and not is_leave_in_progress(wf_resume)
                and _should_restore_suspended_leave_for_intent(message, wf_resume)
                and "suspended_leave_correction" not in src
                and "leave_draft_correction" not in src
                and "pending_leave_show" not in src
                and "leave_summary" not in src
                and "cancel_leave_verify" not in src
                and "submit_command" not in src
            ):
                wf_resume = _persist_workflow_state(
                    session, restore_suspended_leave(wf_resume, force_active=True)
                )
                log_step(trace_id, "suspended_leave_restored_for_intent", {})

        if wizard_dismissed_reason is None and intent == INTENT_EXPENSE_CLAIM:
            wf_resume = getattr(session, "workflow_state", None) or {}
            from chat.services.leave_confirm import is_confirmation_yes as _leave_confirm_yes

            leave_confirm_resume_block = is_awaiting_leave_confirmation(
                wf_resume
            ) and _leave_confirm_yes(message)
            if (
                has_suspended_expense(wf_resume)
                and not is_expense_in_progress(wf_resume)
                and not leave_confirm_resume_block
            ):
                wf_resume = _persist_workflow_state(
                    session, restore_suspended_expense(wf_resume)
                )
                log_step(trace_id, "suspended_expense_restored_for_intent", {})

        lv_pack: dict[str, Any] = {}
        exp_pack: dict[str, Any] = {}
        leave_collecting_blocked = False
        expense_collecting_blocked = False
        wf_leave = getattr(session, "workflow_state", None) or {}
        run_leave_turn = not general_out_of_scope and (
            (
                intent == INTENT_LEAVE_REQUEST and not leave_workflow_interrupt
            )
            or (
                is_leave_in_progress(wf_leave)
                and workflow_turn is not None
                and is_workflow_continuation_turn(workflow_turn)
                and intent
                not in (
                    INTENT_EXPENSE_DAY_SUMMARY,
                    INTENT_EXPENSE_STATUS,
                    INTENT_REQUEST_STATUS,
                )
                and not leave_workflow_interrupt
                and not leave_side_interrupt
            )
        )
        if (
            not run_leave_turn
            and is_awaiting_leave_confirmation(wf_leave)
            and leave_is_confirmation_yes(message)
        ):
            run_leave_turn = True
            intent = INTENT_LEAVE_REQUEST
            intent_result = {
                **intent_result,
                "intent": INTENT_LEAVE_REQUEST,
                "source": (intent_result.get("source") or "intent")
                + "+leave_confirm_force",
            }
        if run_leave_turn:
            lv_pack = process_leave_turn(
                workflow_state=wf_leave,
                message=message,
                entities=dict(entities),
                company_id=company_id,
                trace_id=trace_id,
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
        elif (
            is_leave_in_progress(wf_leave)
            and not leave_workflow_interrupt
            and not general_out_of_scope
        ):
            leave_collecting_blocked = True
        log_step(
            trace_id,
            "leave_workflow_gate",
            {"blocked": leave_collecting_blocked, "intent": intent},
        )

        wf_exp_gate = getattr(session, "workflow_state", None) or {}
        leave_submit_confirm_turn = (
            is_awaiting_leave_confirmation(wf_leave)
            and leave_is_confirmation_yes(message)
        )
        run_expense_turn = not leave_submit_confirm_turn and not expense_workflow_interrupt and not expense_side_interrupt and not general_out_of_scope and (
            intent == INTENT_EXPENSE_CLAIM
            or (
                is_expense_in_progress(wf_exp_gate)
                and intent
                not in (
                    INTENT_HR_POLICY,
                    INTENT_LEAVE_BALANCE,
                    INTENT_LEAVE_REQUEST,
                    INTENT_WFH_REQUEST,
                    INTENT_ATTENDANCE_CORRECTION,
                    INTENT_APPROVAL_ESCALATION,
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
                if has_suspended_leave(wf_exp):
                    wf_exp = mark_restore_leave_after_expense_submit(wf_exp)
                exp_block = wf_exp.get("expense_request") or {}
                stage_def = str(exp_block.get("stage") or "")
                if stage_def in ("review", "submit_confirm"):
                    expense_turn_message = "yes"
            from chat.services.expense.expense_confirm import looks_like_expense_correction
            from chat.services.expense_workflow import _wants_finish_collecting_rules_only

            if wants_expense_summary(message) and not looks_like_expense_correction(
                message
            ):
                if is_expense_paused(wf_exp):
                    wf_exp = resume_expense_session(wf_exp)
                exp_block = wf_exp.get("expense_request") or {}
                exp_items = list(exp_block.get("items") or [])
                pending = exp_block.get("pending_line")
                has_pending = isinstance(pending, dict) and pending.get("amount")
                if (
                    exp_items
                    and not has_pending
                    and _wants_finish_collecting_rules_only(message)
                ):
                    expense_turn_message = "শেষ"
            exp_pack = process_expense_turn(
                workflow_state=wf_exp,
                message=expense_turn_message,
                company_id=company_id,
                employee_id=employee_id,
                session_id=session.session_id,
                trace_id=trace_id,
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
                    from chat.services.leave.session_ledger import (
                        build_session_leave_balance,
                        detect_balance_leave_type,
                    )

                    balance_type = detect_balance_leave_type(message)
                    ledger = build_session_leave_balance(
                        self.crm,
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                        leave_type_filter=balance_type,
                    )
                    crm_payload.update(bal)
                    crm_payload.update(ledger)

            if intent in (INTENT_EXPENSE_STATUS, INTENT_REQUEST_STATUS):
                from chat.services.expense.session_action_memory import (
                    wants_expense_pre_submit_review,
                )

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
                elif wants_expense_pre_submit_review(message):
                    from chat.services.expense.session_action_memory import (
                        format_meta_question_answer,
                    )

                    meta = format_meta_question_answer(
                        getattr(session, "workflow_state", None) or {},
                        message,
                        lang=detect_user_language(message),
                    )
                    if meta:
                        crm_payload["expense_meta_answer"] = meta
                elif _asks_recent_expense_submission(message) or _asks_expense_ref_or_status(
                    message
                ):
                    last = _latest_expense_submission_from_session(
                        getattr(session, "workflow_state", None) or {}
                    )
                    if last.get("reference_id"):
                        crm_payload["expense_last_submission"] = last
                        st = self.crm.get_request_status(
                            str(last["reference_id"]),
                            company_id=company_id,
                            employee_id=employee_id,
                            session_id=session.session_id,
                        )
                        if st.get("status") and st.get("status") != "NOT_FOUND":
                            crm_payload.update(st)
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
                    from chat.services.expense.expense_total_dispute import (
                        format_expense_total_check_message,
                        is_expense_total_check_query,
                    )
                    from chat.services.expense.session_action_memory import (
                        format_meta_question_answer,
                        format_submitted_expense_edit_blocked_answer,
                        looks_like_submitted_expense_correction_attempt,
                        record_expense_total_check,
                        wants_expense_meta_question,
                    )

                    if is_expense_total_check_query(message):
                        inc_chk = (entities.get("expense_incurred_date") or "").strip()
                        if not inc_chk:
                            inc_chk = infer_expense_incurred_date_iso(
                                message=message,
                                hints=entities,
                                today=expense_incurred_date_mod.date.today(),
                            )
                        total_msg = format_expense_total_check_message(
                            getattr(session, "workflow_state", None) or {},
                            crm_breakdown=crm_context,
                            incurred_date_iso=inc_chk,
                            lang=detect_user_language(message),
                            user_message=message,
                        )
                        if total_msg:
                            crm_payload["expense_total_check"] = total_msg
                            wf_st = getattr(session, "workflow_state", None) or {}
                            exp_blk = read_expense_block(wf_st)
                            exp_items = list(exp_blk.get("items") or [])
                            wf_chk = record_expense_total_check(
                                wf_st,
                                total=sum(float(x.get("amount") or 0) for x in exp_items),
                                line_count=len(exp_items),
                                stage=str(exp_blk.get("stage") or "review"),
                            )
                            session.workflow_state = wf_chk
                            session.save(update_fields=["workflow_state", "updated_at"])
                    elif looks_like_submitted_expense_correction_attempt(
                        getattr(session, "workflow_state", None) or {},
                        message,
                    ):
                        crm_payload["expense_meta_answer"] = (
                            format_submitted_expense_edit_blocked_answer(
                                getattr(session, "workflow_state", None) or {},
                                lang=detect_user_language(message),
                            )
                        )
                    elif wants_expense_meta_question(message):
                        meta = format_meta_question_answer(
                            getattr(session, "workflow_state", None) or {},
                            message,
                            lang=detect_user_language(message),
                        )
                        if meta:
                            crm_payload["expense_meta_answer"] = meta
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
                and is_expense_entitlement_query(message)
                and not general_out_of_scope
                and not policy_complaint
            ):
                from chat.services.expense.expense_policy import (
                    build_daily_cap_response,
                    is_expense_daily_cap_query,
                )

                if is_expense_daily_cap_query(message):
                    inc_cap = (entities.get("expense_incurred_date") or "").strip()
                    if not inc_cap:
                        inc_cap = infer_expense_incurred_date_iso(
                            message=message,
                            hints=entities,
                            today=expense_incurred_date_mod.date.today(),
                        )
                    cap_breakdown: dict[str, Any] = {}
                    try:
                        cap_breakdown = self.crm.get_expense_day_breakdown(
                            company_id=company_id,
                            employee_id=employee_id,
                            session_id=session.session_id,
                            incurred_date_iso=inc_cap,
                        )
                    except Exception:
                        cap_breakdown = {}
                    crm_payload["rules_answer"] = build_daily_cap_response(
                        getattr(session, "workflow_state", None) or {},
                        crm_breakdown=cap_breakdown,
                        incurred_date_iso=inc_cap,
                    )
                    crm_payload["rules_mode"] = "expense_daily_cap"
                    log_step(trace_id, "expense_daily_cap_answer", {"date": inc_cap})

            if (
                intent == INTENT_HR_POLICY
                and is_policy_kb_query(message)
                and not general_out_of_scope
                and not policy_complaint
                and not crm_payload.get("rules_answer")
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
                from chat.services.expense.session_ledger import (
                    build_session_expense_ledger,
                    enrich_crm_payload_with_ledger,
                    infer_session_expense_summary_date,
                )

                inc_iso = (
                    hr_query_decision.date_iso(today=expense_incurred_date_mod.date.today())
                    or infer_session_expense_summary_date(
                        getattr(session, "workflow_state", None) or {},
                        message=message,
                        hints=entities,
                        today=expense_incurred_date_mod.date.today(),
                    )
                )
                breakdown = self.crm.get_expense_day_breakdown(
                    company_id=company_id,
                    employee_id=employee_id,
                    session_id=session.session_id,
                    incurred_date_iso=inc_iso,
                )
                from chat.constants import EXPENSE_DAY_CAP_BDT

                ledger = build_session_expense_ledger(
                    getattr(session, "workflow_state", None) or {},
                    crm_breakdown=breakdown,
                    incurred_date_iso=inc_iso,
                    daily_cap=float(EXPENSE_DAY_CAP_BDT),
                )
                breakdown = enrich_crm_payload_with_ledger(breakdown, ledger)
                crm_payload.update(breakdown)
                crm_context.update(breakdown)
                from chat.services.expense.session_action_memory import (
                    wants_expense_history_query,
                )

                if wants_expense_history_query(message):
                    wf_hist = getattr(session, "workflow_state", None) or {}
                    crm_payload["expense_history_view"] = True
                    crm_payload["bot_action_log"] = list(
                        wf_hist.get("bot_action_log") or []
                    )

            if (
                leave_collecting_blocked
                and not leave_workflow_interrupt
                and not general_out_of_scope
                and intent
                not in (INTENT_EXPENSE_DAY_SUMMARY, INTENT_EXPENSE_STATUS)
            ):
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
            elif leave_nav_no_session:
                from chat.services.workflow_navigation import (
                    format_no_active_leave_session_message,
                )

                decision = {
                    "outcome": "INFORMATIONAL",
                    "reason": format_no_active_leave_session_message(
                        expense_active=is_expense_in_progress(
                            getattr(session, "workflow_state", None) or {}
                        )
                    ),
                    "rules_applied": ["LEAVE_NAV_NO_SESSION"],
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
                and intent
                not in (
                    INTENT_EXPENSE_DAY_SUMMARY,
                    INTENT_EXPENSE_STATUS,
                    INTENT_REQUEST_STATUS,
                )
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
                expense_already_submitted = bool(
                    intent == INTENT_EXPENSE_CLAIM
                    and decision.get("outcome") == "SUBMITTED"
                    and crm_payload.get("expense_submission")
                    and request_id
                )
                if expense_already_submitted and hasattr(
                    self.crm, "record_expense_submission"
                ):
                    exp_rec = self.crm.record_expense_submission(
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                        reference_id=request_id,
                        entities=crm_entities,
                        decision=decision,
                        idempotency_key=idempotency_key,
                    )
                    crm_payload["expense_crm_record"] = exp_rec
                    log_step(
                        trace_id,
                        "expense_crm_recorded",
                        {"reference_id": request_id},
                    )
                if not leave_already_submitted and not expense_already_submitted:
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
            if leave_nav_no_session:
                response_finalized = True
            if crm_payload.get("expense_total_check"):
                response_finalized = True
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
                from chat.services.expense.expense_fsm import finalize_expense_submission

                wf_submitted = finalize_expense_submission(
                    getattr(session, "workflow_state", None) or {},
                    reference_id=sub_ref,
                    items=list(entities.get("expense_items") or []),
                    incurred_date_iso=str(entities.get("expense_incurred_date") or ""),
                )
                if should_restore_leave_after_expense_submit(
                    wf_submitted
                ) and has_suspended_leave(wf_submitted):
                    wf_submitted = clear_restore_leave_after_expense_submit(wf_submitted)
                    wf_submitted = restore_suspended_leave(
                        wf_submitted, force_active=True
                    )
                    log_step(
                        trace_id,
                        "suspended_leave_restored_after_deferred_expense_submit",
                        {},
                    )
                wf_submitted = _persist_workflow_state(session, wf_submitted)
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
                and not _any_wizard_active(wf_for_conv)
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
                footer = _soft_expense_pause_footer(
                    wf_policy_resume, user_message=message
                )
                if footer and footer not in msg:
                    msg = msg.rstrip() + footer

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
                and not leave_nav_no_session
                and not response_finalized
                and is_expense_in_progress(getattr(session, "workflow_state", None) or {})
                and not (_is_confirmation_yes(message) or _is_confirmation_no(message))
            ):
                from chat.services.policy_intent_helpers import (
                    is_general_knowledge_out_of_scope as _gk_oos_exp,
                )

                wf_exp_chat = getattr(session, "workflow_state", None) or {}
                if general_out_of_scope or _gk_oos_exp(message):
                    fallback = _wizard_deterministic_fallback(
                        message,
                        wf_exp_chat,
                        general_out_of_scope=True,
                    )
                    if fallback:
                        msg = fallback
                        rstatus = "success"
                        response_finalized = True
                elif (
                    hr_query_decision.query_kind == QUERY_CHITCHAT
                    or _looks_like_chitchat(message, strict=True)
                    or _is_fresh_start_greeting(message)
                ):
                    reply = None
                    try:
                        reply = conversational_reply(
                            message=message,
                            context_lines=context_lines,
                            trace_id=trace_id,
                            workflow_hint=_expense_chitchat_workflow_hint(
                                wf_exp_chat, user_message=message
                            ),
                        )
                    except Exception:
                        reply = _wizard_deterministic_fallback(
                            message,
                            wf_exp_chat,
                            general_out_of_scope=general_out_of_scope,
                        )
                    if reply:
                        msg = reply
                        if is_expense_paused(wf_exp_chat):
                            foot = _expense_paused_side_footer(
                                wf_exp_chat, user_message=message
                            )
                            if foot and foot not in msg:
                                msg = msg.rstrip() + foot
                        rstatus = "success"
                        used_conversational = True
                        response_finalized = True
                if not response_finalized:
                    fallback = _wizard_deterministic_fallback(
                        message,
                        wf_exp_chat,
                        general_out_of_scope=general_out_of_scope,
                    )
                    if fallback:
                        msg = fallback
                        rstatus = "success"
                        response_finalized = True

            if (
                msg
                and expense_side_interrupt
                and is_expense_in_progress(getattr(session, "workflow_state", None) or {})
                and not (_is_confirmation_yes(message) or _is_confirmation_no(message))
            ):
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
                and is_leave_in_progress(getattr(session, "workflow_state", None) or {})
            ):
                from chat.services.policy_intent_helpers import (
                    is_general_knowledge_out_of_scope as _gk_oos,
                )

                if (
                    hr_query_decision.query_kind == QUERY_CHITCHAT
                    or _looks_like_chitchat(message, strict=True)
                ) and not _gk_oos(message):
                    reply = conversational_reply(
                        message=message,
                        context_lines=context_lines,
                        trace_id=trace_id,
                    )
                    if reply:
                        msg = _append_leave_workflow_resume(
                            reply,
                            getattr(session, "workflow_state", None) or {},
                        )
                        rstatus = "success"
                        used_conversational = True
                        response_finalized = True
                if not response_finalized:
                    fallback = _wizard_deterministic_fallback(
                        message,
                        getattr(session, "workflow_state", None) or {},
                        general_out_of_scope=general_out_of_scope,
                    )
                    if fallback:
                        msg = fallback
                        rstatus = "success"
                        response_finalized = True

            if (
                not leave_terminal_turn
                and leave_side_interrupt
                and not policy_complaint
                and not general_out_of_scope
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

        oos_only = "OUT_OF_SCOPE_GENERAL" in list(decision.get("rules_applied") or [])
        return _attach_ui_actions(
            {
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
            },
            getattr(session, "workflow_state", None) or {},
            suppress_wizard_actions=oos_only,
        )

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
                from chat.services.workflow_priority import (
                    expense_query_should_suspend_leave,
                )

                if expense_query_should_suspend_leave(msg) or _strong_expense_day_summary(
                    msg
                ):
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
        or re.search(
            r"(?:apply|submit|joma|জমা).{0,20}(?:korechi|korchi|kor[eo]chi)",
            low,
        )
        or re.search(
            r"(?:ki|kono).{0,30}(?:leave|chuti|chhuti|ছুটি).{0,30}(?:apply|submit|joma)",
            low,
        )
    )


def _latest_expense_submission_from_session(
    workflow_state: dict[str, Any],
) -> dict[str, Any]:
    wf = workflow_state or {}
    last = dict(wf.get("expense_last_submission") or {})
    if last.get("reference_id"):
        return last
    history = list(wf.get("expense_submissions_history") or [])
    for row in reversed(history):
        ref = str(row.get("reference_id") or row.get("request_id") or "").strip()
        if ref:
            return dict(row)
    return {}


def _asks_expense_ref_or_status(message: str) -> bool:
    low = (message or "").lower()
    raw = message or ""
    if not re.search(r"\b(expense|খরচ|reimbursement|claim)\b", low) and "খরচ" not in raw:
        return False
    return bool(
        re.search(
            r"\b(ref|reference|status|track|rid|request\s*id)\b|রেফারেন্স|স্ট্যাটাস",
            low,
        )
        or re.search(r"EXP-\d{4}-", message or "", re.I)
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


def new_trace_id() -> str:
    return str(uuid.uuid4())
