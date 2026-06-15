"""
Build a normalized session view for ``session_turn_router``.

Single place to compute domain flags (``expense_domain_active``, etc.) so routing
rules do not re-derive inconsistent context from raw workflow_state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from chat.constants import INTENT_UNKNOWN
from chat.services.expense.amount_correction_pending import (
    has_amount_correction_pending,
)
from chat.services.expense.expense_confirm import (
    has_ordinal_amount_confirm_pending,
    is_expense_delete_verify_pending,
)
from chat.services.expense.expense_fsm import (
    is_expense_in_progress,
    is_expense_review,
    is_expense_submit_confirm,
    read_expense_block,
)
from chat.services.expense.slots import STAGE_COLLECTING
from chat.services.expense.expense_fsm import normalize_expense_stage
from chat.services.intent_detector import _is_cancel_form_request, _is_fresh_start_greeting
from chat.services.leave.duplicate_choice import is_duplicate_leave_choice_pending
from chat.services.leave_fsm import is_awaiting_leave_confirmation, is_leave_in_progress
from chat.services.leave_workflow import pending_step
from chat.services.workflow_suspend import has_suspended_expense, has_suspended_leave


@dataclass(frozen=True)
class SessionSnapshot:
    message: str
    # Active workflows
    leave_active: bool
    leave_stage: str | None
    leave_review_pending: bool
    pending_leave_step: str | None
    expense_active: bool
    expense_stage: str | None
    expense_review_pending: bool
    pending_expense_step: str | None
    # Parked workflows
    has_suspended_leave: bool
    has_suspended_expense: bool
    has_expense_draft: bool
    has_leave_summary_context: bool
    # Pending UI states
    duplicate_leave_choice_pending: bool
    expense_delete_verify_pending: bool
    expense_ordinal_amount_confirm_pending: bool
    expense_amount_correction_pending: bool
    expense_active_prompt_kind: str | None
    leave_submit_confirm_pending: bool
    expense_submit_confirm_pending: bool
    # Pre-computed routing hints
    duplicate_leave_prompt: str | None
    balance_probe: bool
    policy_interrupt: bool
    provisional_intent: str
    workflow_continuation: bool
    # Context
    context_lines: tuple[str, ...]
    is_greeting: bool
    is_cancel: bool
    expense_submission_locked: bool
    leave_submission_locked: bool
    # Structured prompt binding (slot-first context)
    active_prompt_domain: str | None
    active_prompt_slot: str | None
    expected_answer_kind: str

    @property
    def expense_domain_active(self) -> bool:
        """Active or suspended expense work — not stale drafts after CRM submit."""
        base = self.expense_active or self.has_suspended_expense
        if self.expense_submission_locked:
            return base
        return base or self.has_expense_draft

    @property
    def leave_domain_active(self) -> bool:
        return self.leave_active or self.has_suspended_leave

    @property
    def leave_summary_available(self) -> bool:
        return self.leave_domain_active or self.has_leave_summary_context

    @property
    def has_pending_prompt(self) -> bool:
        return self.expected_answer_kind not in ("", "none")


def _empty_prompt_fields() -> tuple[str | None, str | None, str]:
    from chat.services.session_expected_answer import KIND_NONE

    return None, None, KIND_NONE


def _expense_items_count(workflow_state: dict[str, Any]) -> int:
    block = read_expense_block(workflow_state)
    items = block.get("items") or []
    if items:
        return len(items)
    if not has_suspended_expense(workflow_state):
        return 0
    se = (workflow_state or {}).get("suspended_expense") or {}
    suspended_block = se.get("expense_request") if isinstance(se, dict) else {}
    if not isinstance(suspended_block, dict):
        suspended_block = se if isinstance(se, dict) else {}
    return len(suspended_block.get("items") or [])


def _read_expense_block_for_routing(workflow_state: dict[str, Any]) -> dict[str, Any]:
    block = read_expense_block(workflow_state)
    if block.get("active") or block.get("items"):
        return block
    if has_suspended_expense(workflow_state):
        se = (workflow_state or {}).get("suspended_expense") or {}
        suspended = se.get("expense_request") if isinstance(se, dict) else {}
        if isinstance(suspended, dict) and suspended:
            return suspended
    return block


def _leave_balance_probe(message: str) -> bool:
    from chat.services.leave_balance_intent import is_leave_balance_query

    return is_leave_balance_query(message)


def _policy_interrupt_probe(message: str) -> bool:
    from chat.services.policy_intent_helpers import is_expense_entitlement_query, is_rules_query
    from chat.services.intent_detector import _strong_hr_policy

    import re

    raw = message or ""
    if re.search(
        r"annual\s+leave.{0,30}(?:বছরে|per\s*year|yearly|পাওয়া\s*যায়|কত\s*দিন)",
        raw,
        re.I | re.UNICODE,
    ):
        return True
    if re.search(r"(ডকুমেন্ট|document|নথি|প্রমাণ)", raw, re.I | re.UNICODE) and re.search(
        r"(ছুটি|leave|sick|অসুস্থ|অসুস্থতা|medical)",
        raw,
        re.I | re.UNICODE,
    ):
        return True
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


def _duplicate_leave_prompt(
    workflow_state: dict[str, Any],
    message: str,
    *,
    today: date | None,
) -> str | None:
    import re

    if not re.search(
        r"(?:^|\b)(?:আবার|abar|again)\b",
        message or "",
        re.I | re.UNICODE,
    ):
        from chat.services.workflow_navigation import is_leave_application_message

        if not is_leave_application_message(message):
            return None
    from chat.services.leave_meta_queries import check_overlapping_submitted_leave

    return check_overlapping_submitted_leave(workflow_state, message, today=today)


def build_classifier_snapshot(
    message: str,
    *,
    leave_active: bool = False,
    expense_active: bool = False,
    pending_leave_step: str | None = None,
    pending_expense_step: str = "",
    leave_review_pending: bool = False,
    expense_review_pending: bool = False,
    balance_probe: bool = False,
    has_suspended_expense: bool = False,
    has_expense_draft: bool = False,
) -> SessionSnapshot:
    """Minimal snapshot for ``turn_classifier`` (flags only, no workflow_state)."""
    expense_stage = "review" if expense_review_pending else ("collecting" if expense_active else None)
    leave_stage = "review_pending" if leave_review_pending else ("collecting" if leave_active else None)
    from chat.services.session_expected_answer import derive_prompt_context_fields

    prompt_ctx = derive_prompt_context_fields(
        leave_active=leave_active,
        leave_review_pending=leave_review_pending,
        leave_submit_confirm_pending=leave_review_pending,
        pending_leave_step=pending_leave_step,
        expense_active=expense_active,
        expense_review_pending=expense_review_pending,
        pending_expense_step=pending_expense_step or None,
    )
    return SessionSnapshot(
        message=(message or "").strip(),
        leave_active=leave_active,
        leave_stage=leave_stage,
        leave_review_pending=leave_review_pending,
        pending_leave_step=pending_leave_step,
        expense_active=expense_active,
        expense_stage=expense_stage,
        expense_review_pending=expense_review_pending,
        pending_expense_step=pending_expense_step or None,
        has_suspended_leave=False,
        has_suspended_expense=has_suspended_expense,
        has_expense_draft=has_expense_draft,
        has_leave_summary_context=False,
        duplicate_leave_choice_pending=False,
        expense_delete_verify_pending=False,
        expense_ordinal_amount_confirm_pending=False,
        expense_amount_correction_pending=False,
        expense_active_prompt_kind=None,
        leave_submit_confirm_pending=leave_review_pending,
        expense_submit_confirm_pending=expense_review_pending,
        duplicate_leave_prompt=None,
        balance_probe=balance_probe,
        policy_interrupt=_policy_interrupt_probe(message),
        provisional_intent=INTENT_UNKNOWN,
        workflow_continuation=False,
        context_lines=(),
        is_greeting=_is_fresh_start_greeting(message),
        is_cancel=_is_cancel_form_request(message),
        expense_submission_locked=False,
        leave_submission_locked=False,
        active_prompt_domain=prompt_ctx.domain,
        active_prompt_slot=prompt_ctx.slot,
        expected_answer_kind=prompt_ctx.kind,
    )


def build_session_snapshot(
    message: str,
    *,
    workflow_state: dict[str, Any] | None = None,
    context_lines: list[str] | None = None,
    balance_probe: bool | None = None,
    policy_interrupt: bool | None = None,
    is_greeting: bool | None = None,
    is_cancel: bool | None = None,
    provisional_intent: str | None = None,
    workflow_continuation: bool = False,
    today: date | None = None,
) -> SessionSnapshot:
    """Derive router input from workflow_state + message (orchestrator calls this)."""
    wf = dict(workflow_state or {})
    msg = (message or "").strip()

    leave_active = is_leave_in_progress(wf)
    leave_review_pending = is_awaiting_leave_confirmation(wf)
    pending_leave = pending_step(wf) if leave_active else None

    expense_active = is_expense_in_progress(wf)
    exp_block = _read_expense_block_for_routing(wf)
    expense_stage = (
        normalize_expense_stage(str(exp_block.get("stage") or STAGE_COLLECTING))
        if (expense_active or exp_block.get("items"))
        else None
    )
    expense_review_pending = bool(
        expense_active and is_expense_review(exp_block)
    )
    pending_expense = str(exp_block.get("pending_step") or "") or None

    suspended_leave = has_suspended_leave(wf)
    suspended_expense = has_suspended_expense(wf)
    has_draft = _expense_items_count(wf) > 0

    dup_prompt = _duplicate_leave_prompt(wf, msg, today=today)

    from chat.services.leave_meta_queries import session_has_leave_summary_context

    leave_summary_ctx = session_has_leave_summary_context(wf)

    from chat.services.expense.session_action_memory import has_expense_submission_lock
    from chat.services.leave_fsm import is_leave_submission_locked

    expense_submission_locked = has_expense_submission_lock(wf)
    leave_submission_locked = is_leave_submission_locked(wf)

    leave_stage: str | None = None
    if leave_active:
        leave_stage = "review_pending" if leave_review_pending else "collecting"
    elif leave_review_pending:
        leave_stage = "review_pending"

    dup_choice_pending = is_duplicate_leave_choice_pending(wf)
    exp_delete_pending = is_expense_delete_verify_pending(exp_block)
    exp_submit_confirm = bool(expense_active and is_expense_submit_confirm(exp_block))

    from chat.services.expense.prompt_routing import expense_active_prompt_kind

    exp_prompt_kind = expense_active_prompt_kind(exp_block)

    from chat.services.session_expected_answer import derive_prompt_context_fields

    prompt_ctx = derive_prompt_context_fields(
        duplicate_leave_choice_pending=dup_choice_pending,
        leave_active=leave_active,
        leave_review_pending=leave_review_pending,
        leave_submit_confirm_pending=leave_review_pending,
        pending_leave_step=pending_leave,
        expense_active=expense_active,
        expense_review_pending=expense_review_pending,
        expense_delete_verify_pending=exp_delete_pending,
        expense_submit_confirm_pending=exp_submit_confirm,
        pending_expense_step=pending_expense,
        expense_active_prompt_kind=exp_prompt_kind,
    )

    return SessionSnapshot(
        message=msg,
        leave_active=leave_active,
        leave_stage=leave_stage,
        leave_review_pending=leave_review_pending,
        pending_leave_step=pending_leave,
        expense_active=expense_active,
        expense_stage=expense_stage,
        expense_review_pending=expense_review_pending,
        pending_expense_step=pending_expense,
        has_suspended_leave=suspended_leave,
        has_suspended_expense=suspended_expense,
        has_expense_draft=has_draft,
        has_leave_summary_context=leave_summary_ctx,
        duplicate_leave_choice_pending=dup_choice_pending,
        expense_delete_verify_pending=exp_delete_pending,
        expense_ordinal_amount_confirm_pending=has_ordinal_amount_confirm_pending(
            exp_block
        ),
        expense_amount_correction_pending=has_amount_correction_pending(exp_block),
        expense_active_prompt_kind=exp_prompt_kind,
        leave_submit_confirm_pending=leave_review_pending,
        expense_submit_confirm_pending=exp_submit_confirm,
        duplicate_leave_prompt=dup_prompt,
        balance_probe=balance_probe if balance_probe is not None else _leave_balance_probe(msg),
        policy_interrupt=(
            policy_interrupt if policy_interrupt is not None else _policy_interrupt_probe(msg)
        ),
        provisional_intent=provisional_intent or INTENT_UNKNOWN,
        workflow_continuation=workflow_continuation,
        context_lines=tuple(context_lines or []),
        is_greeting=is_greeting if is_greeting is not None else _is_fresh_start_greeting(msg),
        is_cancel=is_cancel if is_cancel is not None else _is_cancel_form_request(msg),
        expense_submission_locked=expense_submission_locked,
        leave_submission_locked=leave_submission_locked,
        active_prompt_domain=prompt_ctx.domain,
        active_prompt_slot=prompt_ctx.slot,
        expected_answer_kind=prompt_ctx.kind,
    )
