"""P99 fallback: deterministic wizard intent gates (pre-session_turn_router)."""

from __future__ import annotations

import re
from typing import Any

from chat.constants import (
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_EXPENSE_STATUS,
    INTENT_HR_POLICY,
    INTENT_LEAVE_BALANCE,
    INTENT_LEAVE_REQUEST,
    INTENT_REQUEST_STATUS,
    INTENT_UNKNOWN,
)
from chat.services.intent_detector import (
    _is_fresh_start_greeting,
    _looks_like_chitchat,
    _message_answers_wizard_step,
    _strong_expense_claim,
    _strong_expense_day_summary,
    looks_like_wizard_side_question,
    wants_post_submit_expense_summary,
)
from chat.services.leave_confirm import (
    is_awaiting_leave_confirmation,
    is_confirmation_cancel,
    is_confirmation_yes,
    parse_edit_slot,
    wants_defer_expense_for_leave_submit,
    wants_defer_leave_for_expense_submit,
)
from chat.services.leave_workflow import pending_step
from chat.services.expense_workflow import (
    _is_confirmation_no,
    _is_confirmation_yes,
    wants_expense_summary,
)
from chat.services.policy_intent_helpers import (
    is_expense_entitlement_query,
    is_general_knowledge_out_of_scope,
    is_off_topic_for_hr_assistant,
    is_policy_interrupt_message,
)
from chat.services.turn_classifier import _canonical_leave_wizard_token
from chat.services.workflow_suspend import (
    has_suspended_expense,
    has_suspended_leave,
    wants_resume_suspended_leave,
)
from chat.services.expense.routing import looks_like_expense_wizard_continuation

def intent_from_wizard_interrupt(
    decision: Any,
    *,
    gate_prefix: str,
) -> dict[str, Any] | None:
    from chat.services.wizard_interrupt_classifier import CONFIDENCE_LLM_FALLBACK

    if not decision.maps_to_intent:
        return None
    if decision.confidence < CONFIDENCE_LLM_FALLBACK:
        return None
    return {
        "intent": decision.maps_to_intent,
        "confidence": decision.confidence,
        "source": f"{gate_prefix}+{decision.source}",
    }


def detect_intent_during_leave_workflow(
    message: str,
    workflow_state: dict[str, Any],
    *,
    balance_probe: bool,
    trace_id: str = "",
) -> dict[str, Any]:
    """
    Deterministic intent while leave_request draft exists — LLM must not override.
    """
    from chat.services.expense.expense_confirm import looks_like_expense_correction
    from chat.services.leave_meta_queries import (
        wants_cancel_leave_command,
        wants_leave_session_summary,
        wants_pending_leave_show,
    )
    from chat.services.suspended_leave_correction import looks_like_suspended_leave_correction
    from chat.services.leave_confirm import wants_leave_submit_command

    if wants_leave_submit_command(message):
        return {
            "intent": INTENT_LEAVE_REQUEST,
            "confidence": 0.99,
            "source": "leave_workflow_gate+submit_command",
        }
    if is_policy_interrupt_message(message):
        return {
            "intent": INTENT_HR_POLICY,
            "confidence": 0.99,
            "source": "leave_workflow_gate+policy",
        }
    if wants_leave_session_summary(message):
        return {
            "intent": INTENT_LEAVE_REQUEST,
            "confidence": 0.99,
            "source": "leave_workflow_gate+leave_summary",
        }
    if wants_pending_leave_show(message):
        return {
            "intent": INTENT_LEAVE_REQUEST,
            "confidence": 0.99,
            "source": "leave_workflow_gate+pending_leave_show",
        }
    if wants_cancel_leave_command(message):
        return {
            "intent": INTENT_LEAVE_REQUEST,
            "confidence": 0.99,
            "source": "leave_workflow_gate+cancel_leave_verify",
        }
    if (
        has_suspended_leave(workflow_state)
        and not looks_like_expense_correction(message)
        and looks_like_suspended_leave_correction(message)
    ):
        return {
            "intent": INTENT_LEAVE_REQUEST,
            "confidence": 0.99,
            "source": "leave_workflow_gate+leave_draft_correction",
        }
    if is_awaiting_leave_confirmation(workflow_state):
        if wants_defer_leave_for_expense_submit(message) and has_suspended_expense(
            workflow_state
        ):
            return {
                "intent": INTENT_EXPENSE_CLAIM,
                "confidence": 0.99,
                "source": "leave_workflow_gate+defer_expense_submit",
            }
        if wants_defer_expense_for_leave_submit(message):
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_defer_leave_submit",
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
        if wants_resume_suspended_leave(message):
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_resume_nav",
            }
        if balance_probe:
            return {
                "intent": INTENT_LEAVE_BALANCE,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_balance",
            }
        if is_policy_interrupt_message(message):
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
        from chat.services.expense_workflow import wants_expense_summary

        if wants_expense_summary(message):
            return {
                "intent": INTENT_EXPENSE_DAY_SUMMARY,
                "confidence": 0.99,
                "source": "leave_workflow_gate+expense_summary",
            }
        if _strong_expense_claim(message) or _strong_expense_day_summary(message):
            return {
                "intent": INTENT_EXPENSE_CLAIM
                if _strong_expense_claim(message)
                else INTENT_EXPENSE_DAY_SUMMARY,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_expense_switch",
            }
        from chat.services.wizard_interrupt_classifier import (
            build_leave_interrupt_context,
            classify_wizard_interrupt,
            interrupt_is_workflow_switch,
        )

        leave_intr = classify_wizard_interrupt(
            message,
            context=build_leave_interrupt_context(
                workflow_state,
                leave_review_pending=True,
            ),
            trace_id=trace_id,
            use_llm=True,
        )
        if interrupt_is_workflow_switch(leave_intr) or leave_intr.maps_to_intent in (
            INTENT_EXPENSE_DAY_SUMMARY,
            INTENT_LEAVE_REQUEST,
        ):
            mapped = intent_from_wizard_interrupt(
                leave_intr, gate_prefix="leave_workflow_gate+confirm"
            )
            if mapped:
                return mapped
        from chat.services.wizard_turn_gate import (
            is_casual_wizard_side_statement,
            is_leave_navigation_phrase,
            looks_like_leave_review_update,
        )

        if is_leave_navigation_phrase(message):
            return {
                "intent": INTENT_UNKNOWN,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_interrupt",
            }
        if is_casual_wizard_side_statement(message) or is_off_topic_for_hr_assistant(
            message, wizard_active=True
        ):
            return {
                "intent": INTENT_UNKNOWN,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_interrupt",
            }
        if looks_like_leave_review_update(message):
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_patch",
            }
        if _looks_like_chitchat(message, strict=True) or _is_fresh_start_greeting(message):
            return {
                "intent": INTENT_UNKNOWN,
                "confidence": 0.99,
                "source": "leave_workflow_gate+confirm_interrupt",
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
    from chat.services.expense_workflow import wants_expense_summary

    if wants_expense_summary(message):
        return {
            "intent": INTENT_EXPENSE_DAY_SUMMARY,
            "confidence": 0.99,
            "source": "leave_workflow_gate+expense_summary",
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
    if is_policy_interrupt_message(message):
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
    from chat.services.wizard_interrupt_classifier import (
        build_leave_interrupt_context,
        classify_wizard_interrupt,
        interrupt_is_workflow_switch,
    )

    leave_intr = classify_wizard_interrupt(
        message,
        context=build_leave_interrupt_context(
            workflow_state,
            pending_leave_step=pending_step(workflow_state),
        ),
        trace_id=trace_id,
        use_llm=True,
    )
    if interrupt_is_workflow_switch(leave_intr) or leave_intr.maps_to_intent in (
        INTENT_EXPENSE_CLAIM,
        INTENT_HR_POLICY,
        INTENT_LEAVE_BALANCE,
    ):
        mapped = intent_from_wizard_interrupt(
            leave_intr, gate_prefix="leave_workflow_gate"
        )
        if mapped:
            return mapped
    return {
        "intent": INTENT_UNKNOWN,
        "confidence": 0.99,
        "source": "leave_workflow_gate+interrupt",
    }


def detect_intent_during_expense_workflow(
    message: str,
    workflow_state: dict[str, Any],
    *,
    balance_probe: bool,
    trace_id: str = "",
) -> dict[str, Any]:
    """Deterministic intent while expense_request is active — LLM must not override."""
    from chat.services.leave_meta_queries import (
        wants_cancel_leave_command,
        wants_leave_session_summary,
        wants_pending_leave_show,
    )
    from chat.services.expense.expense_confirm import looks_like_expense_correction
    from chat.services.suspended_leave_correction import looks_like_suspended_leave_correction

    from chat.services.expense.wizard_commands import wants_cancel_expense_command

    if wants_cancel_expense_command(message):
        return {
            "intent": INTENT_EXPENSE_CLAIM,
            "confidence": 0.99,
            "source": "expense_workflow_gate+cancel_expense_verify",
        }
    from chat.services.intent_detector import _is_cancel_form_request
    from chat.services.leave_confirm import is_confirmation_cancel

    if _is_cancel_form_request(message) or is_confirmation_cancel(message):
        return {
            "intent": INTENT_EXPENSE_CLAIM,
            "confidence": 0.99,
            "source": "expense_workflow_gate+cancel_expense_verify",
        }
    from chat.services.expense.expense_confirm import (
        is_expense_delete_verify_pending,
    )
    from chat.services.expense.expense_fsm import read_expense_block

    exp_block = read_expense_block(workflow_state)
    if is_expense_delete_verify_pending(exp_block) and (
        _is_confirmation_yes(message) or _is_confirmation_no(message)
    ):
        return {
            "intent": INTENT_EXPENSE_CLAIM,
            "confidence": 0.99,
            "source": "expense_workflow_gate+delete_confirm",
        }
    if looks_like_expense_correction(message):
        return {
            "intent": INTENT_EXPENSE_CLAIM,
            "confidence": 0.99,
            "source": "expense_workflow_gate+correction",
        }
    if is_policy_interrupt_message(message):
        return {
            "intent": INTENT_HR_POLICY,
            "confidence": 0.99,
            "source": "expense_workflow_gate+policy",
        }
    if has_suspended_leave(workflow_state) and looks_like_suspended_leave_correction(
        message
    ):
        from chat.services.leave_fsm import read_leave_state, ACTIVE_FLOW_LEAVE
        from chat.services.session_snapshot import build_session_snapshot
        from chat.services.wizard_turn_gate import is_leave_collecting_slot_answer

        snap = build_session_snapshot(message, workflow_state=workflow_state)
        if not (
            read_leave_state(workflow_state).get("active_flow") == ACTIVE_FLOW_LEAVE
            and is_leave_collecting_slot_answer(
                message,
                pending_leave_step=snap.pending_leave_step,
                leave_active=snap.leave_active,
                leave_review_pending=snap.leave_review_pending,
            )
        ):
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "confidence": 0.99,
                "source": "expense_workflow_gate+suspended_leave_correction",
            }
    if wants_leave_session_summary(message):
        return {
            "intent": INTENT_LEAVE_REQUEST,
            "confidence": 0.99,
            "source": "expense_workflow_gate+leave_summary",
        }
    if wants_pending_leave_show(message) and has_suspended_leave(workflow_state):
        return {
            "intent": INTENT_LEAVE_REQUEST,
            "confidence": 0.99,
            "source": "expense_workflow_gate+pending_leave_show",
        }
    if wants_cancel_leave_command(message):
        return {
            "intent": INTENT_LEAVE_REQUEST,
            "confidence": 0.99,
            "source": "expense_workflow_gate+cancel_leave_verify",
        }
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
    if _is_leave_application_message(message):
        from chat.services.leave_meta_queries import check_duplicate_tomorrow_leave

        dup_msg = check_duplicate_tomorrow_leave(workflow_state)
        if dup_msg and re.search(
            r"agamikal|agamikal|আগামীকাল|tomorrow|kalke|kalker",
            message or "",
            re.I | re.UNICODE,
        ):
            return {
                "intent": INTENT_LEAVE_REQUEST,
                "confidence": 0.99,
                "source": "expense_workflow_gate+duplicate_tomorrow",
                "_block_message": dup_msg,
            }
        return {
            "intent": INTENT_LEAVE_REQUEST,
            "confidence": 0.99,
            "source": "expense_workflow_gate+leave_apply",
        }
    from chat.services.leave_workflow import is_leave_in_progress
    from chat.services.workflow_navigation import is_leave_navigation_phrase

    if (
        is_leave_navigation_phrase(message)
        and not has_suspended_leave(workflow_state)
        and not is_leave_in_progress(workflow_state)
    ):
        return {
            "intent": INTENT_UNKNOWN,
            "confidence": 0.99,
            "source": "expense_workflow_gate+leave_nav_no_session",
        }
    if balance_probe:
        return {
            "intent": INTENT_LEAVE_BALANCE,
            "confidence": 0.99,
            "source": "expense_workflow_gate+balance",
        }
    from chat.services.expense.expense_total_dispute import is_expense_total_check_query

    if is_expense_total_check_query(message):
        return {
            "intent": INTENT_EXPENSE_STATUS,
            "confidence": 0.99,
            "source": "expense_workflow_gate+total_check",
        }
    from chat.services.expense.expense_draft_snapshots import (
        wants_restore_expense_version,
    )

    if wants_restore_expense_version(message):
        return {
            "intent": INTENT_EXPENSE_CLAIM,
            "confidence": 0.99,
            "source": "expense_workflow_gate+restore",
        }
    from chat.services.expense_workflow import wants_resume_or_show_expense

    if wants_resume_or_show_expense(message):
        return {
            "intent": INTENT_EXPENSE_CLAIM,
            "confidence": 0.99,
            "source": "expense_workflow_gate+resume_show",
        }
    from chat.services.expense.session_action_memory import (
        looks_like_submitted_expense_correction_attempt,
        wants_expense_meta_question,
        wants_expense_pre_submit_review,
        wants_post_submit_edit_question,
    )

    if wants_expense_pre_submit_review(message):
        return {
            "intent": INTENT_EXPENSE_STATUS,
            "confidence": 0.99,
            "source": "expense_workflow_gate+pre_submit_review",
        }
    if _asks_recent_leave_submission(message):
        return {
            "intent": INTENT_REQUEST_STATUS,
            "confidence": 0.99,
            "source": "expense_workflow_gate+leave_submit_status",
        }
    if _asks_recent_expense_submission(message) or _asks_expense_ref_or_status(message):
        return {
            "intent": INTENT_EXPENSE_STATUS,
            "confidence": 0.99,
            "source": "expense_workflow_gate+submit_status",
        }

    if wants_post_submit_edit_question(message) or wants_expense_meta_question(message):
        return {
            "intent": INTENT_EXPENSE_STATUS,
            "confidence": 0.99,
            "source": "expense_workflow_gate+meta",
        }
    if looks_like_submitted_expense_correction_attempt(workflow_state, message):
        return {
            "intent": INTENT_EXPENSE_STATUS,
            "confidence": 0.99,
            "source": "expense_workflow_gate+post_submit_edit_blocked",
        }
    if wants_expense_summary(message):
        return {
            "intent": INTENT_EXPENSE_DAY_SUMMARY,
            "confidence": 0.99,
            "source": "expense_workflow_gate+summary",
        }
    if _is_confirmation_yes(message) or _is_confirmation_no(message):
        return {
            "intent": INTENT_EXPENSE_CLAIM,
            "confidence": 0.99,
            "source": "expense_workflow_gate+confirm",
        }
    from chat.services.expense.wizard_commands import (
        is_expense_wizard_command,
        wants_expense_submit_command,
    )

    if wants_expense_submit_command(message) or is_expense_wizard_command(message):
        return {
            "intent": INTENT_EXPENSE_CLAIM,
            "confidence": 0.99,
            "source": "expense_workflow_gate+command",
        }
    if wants_post_submit_expense_summary(message) or _strong_expense_day_summary(message):
        return {
            "intent": INTENT_EXPENSE_DAY_SUMMARY,
            "confidence": 0.99,
            "source": "expense_workflow_gate+day_summary",
        }
    if is_policy_interrupt_message(message):
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
    if _looks_like_chitchat(message, strict=True) or _is_fresh_start_greeting(message):
        return {
            "intent": INTENT_UNKNOWN,
            "confidence": 0.99,
            "source": "expense_workflow_gate+interrupt",
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
    if balance_probe:
        return {
            "intent": INTENT_LEAVE_BALANCE,
            "confidence": 0.99,
            "source": "expense_workflow_gate+balance",
        }
    if looks_like_expense_wizard_continuation(message):
        return {
            "intent": INTENT_EXPENSE_CLAIM,
            "confidence": 0.99,
            "source": "expense_workflow_gate+slot",
        }
    from chat.services.wizard_interrupt_classifier import (
        build_expense_interrupt_context,
        classify_wizard_interrupt,
        interrupt_is_workflow_switch,
    )

    expense_intr = classify_wizard_interrupt(
        message,
        context=build_expense_interrupt_context(workflow_state),
        trace_id=trace_id,
        use_llm=True,
    )
    if interrupt_is_workflow_switch(expense_intr) or expense_intr.maps_to_intent in (
        INTENT_LEAVE_REQUEST,
        INTENT_HR_POLICY,
        INTENT_LEAVE_BALANCE,
    ):
        mapped = intent_from_wizard_interrupt(
            expense_intr, gate_prefix="expense_workflow_gate"
        )
        if mapped:
            return mapped
    return {
        "intent": INTENT_UNKNOWN,
        "confidence": 0.99,
        "source": "expense_workflow_gate+unrelated",
    }

def _is_leave_application_message(message: str) -> bool:
    if is_policy_interrupt_message(message):
        return False
    from chat.services.workflow_navigation import is_leave_application_message

    return is_leave_application_message(message)


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
