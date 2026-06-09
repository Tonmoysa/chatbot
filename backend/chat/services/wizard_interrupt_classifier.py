"""
Classify user messages that interrupt an active leave or expense wizard.

Rules-first (Bangla/Banglish keywords) with optional LLM fallback for free-form
voice transcripts that regex cannot cover reliably.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from chat.constants import (
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_HR_POLICY,
    INTENT_LEAVE_BALANCE,
    INTENT_LEAVE_REQUEST,
    INTENT_UNKNOWN,
)
from chat.services.llm_client import LLMClient

# Mirror turn_classifier constants — avoid circular import.
_TURN_NEW_WORKFLOW = "NEW_WORKFLOW"
_TURN_POLICY_QUERY = "POLICY_QUERY"
_TURN_CHITCHAT = "CHITCHAT"

logger = logging.getLogger("hr_chatbot")

CONFIDENCE_LLM_FALLBACK = 0.72

INTERRUPT_CONTINUE_EXPENSE = "continue_expense"
INTERRUPT_CONTINUE_LEAVE = "continue_leave"
INTERRUPT_NEW_LEAVE = "new_leave_request"
INTERRUPT_NEW_EXPENSE = "new_expense_request"
INTERRUPT_RESUME_LEAVE = "resume_suspended_leave"
INTERRUPT_RESUME_EXPENSE = "resume_suspended_expense"
INTERRUPT_POLICY = "policy_query"
INTERRUPT_BALANCE = "balance_query"
INTERRUPT_CHITCHAT = "chitchat"
INTERRUPT_CONFIRM = "confirm"
INTERRUPT_DENY = "deny"
INTERRUPT_CANCEL = "cancel"
INTERRUPT_EXPENSE_RECAP = "expense_recap"
INTERRUPT_LEAVE_SUBMIT = "leave_submit_request"
INTERRUPT_UNCLEAR = "unclear"

_LEAVE_DOMAIN_RE = re.compile(
    r"(?:"
    r"ছুটি|chuti|chhuti|chutti|leave|লিভ|সিক\s*লিভ|sick\s*leave|"
    r"অসুস্থতা|medical\s*leave"
    r")",
    re.I | re.UNICODE,
)

_LEAVE_INTENT_VERB_RE = re.compile(
    r"(?:"
    r"চাই|lagbe|lage|dorkar|need|apply|request|নিতে|লাগবে|nit[e]?\s*chai|"
    r"nite\s*chai|নিব|নেব|নেওয়া|নেবো"
    r")",
    re.I | re.UNICODE,
)

_SICK_LEAVE_REASON_RE = re.compile(
    r"(?:"
    r"শরীর\s*খারাপ|সরীর\s*খারাপ|অসুস্থ|অসুস্থতা|"
    r"soril\s*kharap|shorir\s*kharap|body\s*ache|fever|জ্বর|"
    r"pet\s*betha|matha\s*betha|বমি|মাথা\s*ব্যথা"
    r")",
    re.I | re.UNICODE,
)

_INTERRUPT_LLM_SYSTEM = """You classify HR chatbot messages sent WHILE another workflow wizard is active.

The user may be continuing the active form OR switching to a different HR task.
Return STRICT JSON only:
{
  "interrupt_type": "continue_expense" | "continue_leave" | "new_leave_request" | "new_expense_request" |
    "resume_suspended_leave" | "resume_suspended_expense" | "expense_recap" | "leave_submit_request" |
    "policy_query" | "balance_query" |
    "chitchat" | "confirm" | "deny" | "cancel" | "unclear",
  "confidence": 0.0 to 1.0
}

RULES
- continue_expense: editing/adding expense lines, amounts, routes, done/submit for CURRENT expense draft
  Examples: "bus 50 hobe", "lunch 100", "joma daw", "হ্যাঁ", "remove train"
- new_leave_request: user wants to APPLY for leave (not ask policy) — may be in Bengali script or Banglish
  Examples: "আমার শরীর খারাপ তাই কালকে লিভ লাগবে", "amar kalke chuti lagbe paid full day",
  "sick leave tomorrow full day"
- new_expense_request: user lists NEW costs while leave wizard is active
  Examples: "ajke lunch 100 taka bus 50"
- resume_suspended_leave: return to a parked leave draft ("leave e back koro", "ছুটি তে ফিরে যাও")
- resume_suspended_expense: return to parked expense ("expense e back koro")
- expense_recap: read-only spend question — NOT a new claim line
  Examples: "amar expense koto ajke", "ajker kharcha bolo", "what did I spend today"
- leave_submit_request: user wants to submit/finish a parked leave (often after a side question)
  Examples: "leave request ta submit koro", "chuti ta joma daw", "submit my leave now"
- policy_query: HR rules / handbook / entitlement ("leave policy ki", "300 taka limit koto")
- balance_query: remaining leave balance ("kotodin chuti ache")
- chitchat: greetings, jokes, general knowledge unrelated to HR forms
- confirm/deny/cancel: yes/no/cancel for the ACTIVE wizard only
- If unsure, interrupt_type unclear and confidence below 0.7
- NEVER classify expense line edits as new_leave_request
- Bengali "লিভ" means leave; "শরীর খারাপ" + future date + leave need → new_leave_request
"""


@dataclass
class WizardInterruptContext:
    expense_active: bool = False
    leave_active: bool = False
    expense_stage: str = ""
    expense_review_pending: bool = False
    leave_review_pending: bool = False
    has_suspended_leave: bool = False
    has_suspended_expense: bool = False
    pending_leave_step: str = ""
    expense_pending_step: str = ""
    expense_item_summary: str = ""
    leave_draft_summary: str = ""


@dataclass
class WizardInterruptDecision:
    interrupt_type: str = INTERRUPT_UNCLEAR
    confidence: float = 0.0
    source: str = "none"
    maps_to_intent: str | None = None
    maps_to_turn: str | None = None
    signals: dict[str, Any] = field(default_factory=dict)


def _map_interrupt_to_outputs(
    interrupt_type: str,
    *,
    confidence: float,
    source: str,
) -> WizardInterruptDecision:
    intent: str | None = None
    turn: str | None = None
    if interrupt_type == INTERRUPT_NEW_LEAVE:
        intent = INTENT_LEAVE_REQUEST
        turn = _TURN_NEW_WORKFLOW
    elif interrupt_type == INTERRUPT_RESUME_LEAVE:
        intent = INTENT_LEAVE_REQUEST
        turn = _TURN_NEW_WORKFLOW
    elif interrupt_type == INTERRUPT_NEW_EXPENSE:
        intent = INTENT_EXPENSE_CLAIM
        turn = _TURN_NEW_WORKFLOW
    elif interrupt_type == INTERRUPT_RESUME_EXPENSE:
        intent = INTENT_EXPENSE_CLAIM
        turn = _TURN_NEW_WORKFLOW
    elif interrupt_type == INTERRUPT_POLICY:
        intent = INTENT_HR_POLICY
        turn = _TURN_POLICY_QUERY
    elif interrupt_type == INTERRUPT_BALANCE:
        intent = INTENT_LEAVE_BALANCE
        turn = _TURN_POLICY_QUERY
    elif interrupt_type == INTERRUPT_CHITCHAT:
        intent = INTENT_UNKNOWN
        turn = _TURN_CHITCHAT
    elif interrupt_type in (INTERRUPT_CONTINUE_EXPENSE, INTERRUPT_CONFIRM, INTERRUPT_DENY):
        intent = INTENT_EXPENSE_CLAIM
    elif interrupt_type == INTERRUPT_CONTINUE_LEAVE:
        intent = INTENT_LEAVE_REQUEST
    elif interrupt_type == INTERRUPT_EXPENSE_RECAP:
        intent = INTENT_EXPENSE_DAY_SUMMARY
    elif interrupt_type == INTERRUPT_LEAVE_SUBMIT:
        intent = INTENT_LEAVE_REQUEST
    return WizardInterruptDecision(
        interrupt_type=interrupt_type,
        confidence=confidence,
        source=source,
        maps_to_intent=intent,
        maps_to_turn=turn,
    )


def _rules_leave_application(message: str) -> bool:
    from chat.services.workflow_navigation import is_leave_application_message

    if is_leave_application_message(message):
        return True
    text = (message or "").strip()
    if not text:
        return False
    low = text.lower()
    if _LEAVE_DOMAIN_RE.search(text) and _LEAVE_INTENT_VERB_RE.search(low):
        return True
    if _SICK_LEAVE_REASON_RE.search(text) and (
        _LEAVE_DOMAIN_RE.search(text) or _LEAVE_INTENT_VERB_RE.search(low)
    ):
        if re.search(
            r"(?:কাল|কালকে|আগামী|tomorrow|kal|kalke|পরশু|next\s*day)",
            text,
            re.I | re.UNICODE,
        ):
            return True
        if re.search(r"(?:full|half|ফুল|হাফ|paid|unpaid|বেতন)", low, re.I):
            return True
    return False


def _rules_expense_claim(message: str) -> bool:
    from chat.services.intent_detector import _strong_expense_claim

    return _strong_expense_claim(message)


def rules_wizard_interrupt(
    message: str,
    *,
    context: WizardInterruptContext,
) -> WizardInterruptDecision:
    """Deterministic interrupt classification — no LLM."""
    text = (message or "").strip()
    if not text:
        return WizardInterruptDecision()

    from chat.services.expense.expense_confirm import (
        is_confirmation_no,
        is_confirmation_yes,
    )
    from chat.services.intent_detector import _is_cancel_form_request
    from chat.services.leave_confirm import (
        is_confirmation_cancel,
        is_confirmation_yes as leave_confirmation_yes,
    )
    from chat.services.leave_balance_intent import is_leave_balance_query
    from chat.services.policy_intent_helpers import is_rules_query
    from chat.services.workflow_suspend import wants_resume_suspended_leave

    if _is_cancel_form_request(text) or is_confirmation_cancel(text):
        return _map_interrupt_to_outputs(
            INTERRUPT_CANCEL, confidence=1.0, source="rules_cancel"
        )

    if context.expense_active and (is_confirmation_yes(text) or is_confirmation_no(text)):
        return _map_interrupt_to_outputs(
            INTERRUPT_CONFIRM if is_confirmation_yes(text) else INTERRUPT_DENY,
            confidence=1.0,
            source="rules_expense_confirm",
        )
    if context.leave_active and leave_confirmation_yes(text):
        return _map_interrupt_to_outputs(
            INTERRUPT_CONFIRM, confidence=1.0, source="rules_leave_confirm"
        )

    if is_leave_balance_query(text):
        return _map_interrupt_to_outputs(
            INTERRUPT_BALANCE, confidence=0.99, source="rules_balance"
        )

    if is_rules_query(text):
        return _map_interrupt_to_outputs(
            INTERRUPT_POLICY, confidence=0.99, source="rules_policy"
        )

    if context.has_suspended_leave and wants_resume_suspended_leave(text):
        return _map_interrupt_to_outputs(
            INTERRUPT_RESUME_LEAVE, confidence=0.99, source="rules_resume_leave"
        )

    from chat.services.expense_workflow import wants_resume_or_show_expense

    if context.has_suspended_expense and wants_resume_or_show_expense(text):
        return _map_interrupt_to_outputs(
            INTERRUPT_RESUME_EXPENSE, confidence=0.99, source="rules_resume_expense"
        )

    if context.expense_active and _rules_leave_application(text):
        return _map_interrupt_to_outputs(
            INTERRUPT_NEW_LEAVE, confidence=0.99, source="rules_new_leave"
        )

    if context.leave_active and _rules_expense_claim(text):
        return _map_interrupt_to_outputs(
            INTERRUPT_NEW_EXPENSE, confidence=0.99, source="rules_new_expense"
        )

    from chat.services.expense_workflow import wants_expense_spend_recap_query

    if wants_expense_spend_recap_query(text):
        return _map_interrupt_to_outputs(
            INTERRUPT_EXPENSE_RECAP, confidence=0.99, source="rules_expense_recap"
        )

    from chat.services.leave_confirm import wants_defer_expense_for_leave_submit

    if wants_defer_expense_for_leave_submit(text):
        return _map_interrupt_to_outputs(
            INTERRUPT_LEAVE_SUBMIT, confidence=0.99, source="rules_leave_submit"
        )

    return WizardInterruptDecision()


def _context_block(context: WizardInterruptContext) -> str:
    lines: list[str] = []
    if context.expense_active:
        lines.append(f"Active workflow: expense (stage={context.expense_stage or '?'})")
        if context.expense_review_pending:
            lines.append("Expense is at review/confirm.")
        if context.expense_item_summary:
            lines.append(f"Expense draft: {context.expense_item_summary}")
        if context.expense_pending_step:
            lines.append(f"Pending expense step: {context.expense_pending_step}")
    if context.leave_active:
        lines.append("Active workflow: leave")
        if context.leave_review_pending:
            lines.append("Leave is at review/confirm.")
        if context.pending_leave_step:
            lines.append(f"Pending leave step: {context.pending_leave_step}")
    if context.leave_draft_summary:
        lines.append(f"Leave draft: {context.leave_draft_summary}")
    if context.has_suspended_leave:
        lines.append("Suspended leave snapshot: yes")
    if context.has_suspended_expense:
        lines.append("Suspended expense snapshot: yes")
    if not lines:
        lines.append("Active workflow: none")
    return "\n".join(lines)


def _llm_wizard_interrupt(
    message: str,
    *,
    context: WizardInterruptContext,
    trace_id: str,
    llm: LLMClient | None = None,
) -> WizardInterruptDecision | None:
    client = llm or LLMClient()
    if not client.is_configured():
        return None
    user_prompt = (
        f"{_context_block(context)}\n\n"
        f"User message:\n{(message or '').strip()}\n\n"
        "Return JSON only."
    )
    out = client.chat_json(
        system_prompt=_INTERRUPT_LLM_SYSTEM,
        user_prompt=user_prompt,
        trace_id=trace_id or "wizard-interrupt-llm",
    )
    if not isinstance(out, dict):
        return None
    interrupt_type = str(out.get("interrupt_type") or INTERRUPT_UNCLEAR).strip().lower()
    confidence = float(out.get("confidence") or 0.0)
    if interrupt_type == INTERRUPT_UNCLEAR or confidence < CONFIDENCE_LLM_FALLBACK:
        return WizardInterruptDecision(
            interrupt_type=INTERRUPT_UNCLEAR,
            confidence=confidence,
            source="llm_unclear",
        )
    logger.info(
        "wizard_interrupt_llm trace_id=%s type=%s confidence=%s",
        trace_id,
        interrupt_type,
        confidence,
    )
    return _map_interrupt_to_outputs(
        interrupt_type, confidence=confidence, source="llm_interrupt"
    )


def classify_wizard_interrupt(
    message: str,
    *,
    context: WizardInterruptContext,
    trace_id: str = "",
    use_llm: bool = True,
    llm: LLMClient | None = None,
) -> WizardInterruptDecision:
    """
    Rules-first wizard interrupt classification with optional LLM fallback.

    Call when regex gates did not resolve a cross-workflow switch.
    """
    rules = rules_wizard_interrupt(message, context=context)
    if rules.interrupt_type != INTERRUPT_UNCLEAR and rules.confidence >= CONFIDENCE_LLM_FALLBACK:
        return rules

    if not use_llm:
        return rules

    # Only spend LLM when a wizard is active and message is non-trivial.
    text = (message or "").strip()
    if not text or len(text) < 8:
        return rules
    if not (context.expense_active or context.leave_active):
        return rules

    llm_decision = _llm_wizard_interrupt(
        message, context=context, trace_id=trace_id, llm=llm
    )
    if llm_decision and llm_decision.interrupt_type != INTERRUPT_UNCLEAR:
        return llm_decision
    return rules if rules.interrupt_type != INTERRUPT_UNCLEAR else (llm_decision or rules)


def classify_active_wizard_interrupt(
    message: str,
    *,
    workflow_state: dict[str, Any] | None = None,
    leave_active: bool = False,
    expense_active: bool = False,
    leave_review_pending: bool = False,
    expense_review_pending: bool = False,
    pending_leave_step: str = "",
    trace_id: str = "",
    use_llm: bool = True,
) -> WizardInterruptDecision:
    """
    Single entry for wizard interrupt classification (P3).

    Builds rich context from workflow_state and runs rules + optional LLM.
    """
    wf = workflow_state or {}
    context = WizardInterruptContext(
        expense_active=expense_active,
        leave_active=leave_active,
        leave_review_pending=leave_review_pending,
        expense_review_pending=expense_review_pending,
        pending_leave_step=pending_leave_step or "",
    )
    if expense_active:
        exp_ctx = build_expense_interrupt_context(wf)
        context = WizardInterruptContext(
            expense_active=True,
            expense_stage=exp_ctx.expense_stage,
            expense_review_pending=exp_ctx.expense_review_pending,
            expense_item_summary=exp_ctx.expense_item_summary,
            expense_pending_step=exp_ctx.expense_pending_step,
            leave_active=leave_active,
            leave_review_pending=leave_review_pending,
            pending_leave_step=pending_leave_step or "",
            has_suspended_leave=exp_ctx.has_suspended_leave,
            has_suspended_expense=exp_ctx.has_suspended_expense,
        )
    elif leave_active:
        leave_ctx = build_leave_interrupt_context(
            wf,
            pending_leave_step=pending_leave_step or "",
            leave_review_pending=leave_review_pending,
        )
        context = leave_ctx
        context.leave_active = True
        context.leave_review_pending = leave_review_pending
        context.pending_leave_step = pending_leave_step or ""

    return classify_wizard_interrupt(
        message,
        context=context,
        trace_id=trace_id,
        use_llm=use_llm,
    )


def interrupt_is_workflow_switch(decision: WizardInterruptDecision) -> bool:
    return decision.interrupt_type in (
        INTERRUPT_NEW_LEAVE,
        INTERRUPT_NEW_EXPENSE,
        INTERRUPT_RESUME_LEAVE,
        INTERRUPT_RESUME_EXPENSE,
        INTERRUPT_EXPENSE_RECAP,
        INTERRUPT_LEAVE_SUBMIT,
    )


def build_expense_interrupt_context(
    workflow_state: dict[str, Any] | None,
) -> WizardInterruptContext:
    from chat.services.expense.expense_fsm import is_expense_review, read_expense_block
    from chat.services.workflow_suspend import has_suspended_leave

    wf = workflow_state or {}
    block = read_expense_block(wf)
    items = list(block.get("items") or [])
    summary_parts: list[str] = []
    for row in items[:6]:
        cat = str(row.get("category") or "?")
        amt = row.get("amount", "?")
        summary_parts.append(f"{cat} {amt}")
    return WizardInterruptContext(
        expense_active=bool(block.get("active")),
        expense_stage=str(block.get("stage") or ""),
        expense_review_pending=bool(block.get("active") and is_expense_review(block)),
        has_suspended_leave=has_suspended_leave(wf),
        expense_item_summary=", ".join(summary_parts) if summary_parts else "(empty)",
        expense_pending_step=str(block.get("pending_step") or ""),
    )


def build_leave_interrupt_context(
    workflow_state: dict[str, Any] | None,
    *,
    pending_leave_step: str = "",
    leave_review_pending: bool = False,
) -> WizardInterruptContext:
    from chat.services.leave_fsm import read_leave_state
    from chat.services.workflow_suspend import has_suspended_expense, has_suspended_leave

    wf = workflow_state or {}
    leave_summary = ""
    st = read_leave_state(wf)
    draft = dict(st.get("draft") or {})
    if draft:
        parts = [
            str(draft.get("start_date") or ""),
            str(draft.get("end_date") or ""),
            str(draft.get("leave_payment_category") or ""),
            str(draft.get("day_scope") or ""),
        ]
        leave_summary = ", ".join(p for p in parts if p)
    elif has_suspended_leave(wf):
        sl = wf.get("suspended_leave") or {}
        sd = dict(sl.get("draft") or {})
        parts = [
            str(sd.get("start_date") or ""),
            str(sd.get("end_date") or ""),
            str(sd.get("leave_payment_category") or ""),
            str(sd.get("day_scope") or ""),
        ]
        leave_summary = ", ".join(p for p in parts if p)
    return WizardInterruptContext(
        leave_active=True,
        leave_review_pending=leave_review_pending,
        has_suspended_expense=has_suspended_expense(wf),
        has_suspended_leave=has_suspended_leave(wf),
        pending_leave_step=pending_leave_step or "",
        leave_draft_summary=leave_summary,
    )
