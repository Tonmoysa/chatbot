"""
Structured prompt context for session turn routing.

When a wizard is waiting for a specific slot, user replies bind to that slot
first — before balance/meta/clarification heuristics guess from message text alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chat.services.session_snapshot import SessionSnapshot

# Answer kinds the router / clarity layer understand.
KIND_NONE = "none"
KIND_DATE = "date"
KIND_DURATION = "duration"
KIND_ENUM = "enum"
KIND_YES_NO = "yes_no"
KIND_AMOUNT = "amount"
KIND_FREE_TEXT = "free_text"
KIND_ROUTE = "route"
KIND_CLARIFY = "clarify"

_LEAVE_SLOT_KIND: dict[str, str] = {
    "leave_dates": KIND_DATE,
    "leave_type": KIND_ENUM,
    "leave_payment_category": KIND_ENUM,
    "day_scope": KIND_ENUM,
    "half_day_period": KIND_ENUM,
    "reason": KIND_FREE_TEXT,
    "supporting_document": KIND_FREE_TEXT,
    "date_clarification": KIND_DATE,
    "duplicate_choice": KIND_ENUM,
    "review_confirm": KIND_YES_NO,
}

_EXPENSE_SLOT_KIND: dict[str, str] = {
    "category": KIND_ENUM,
    "from_to": KIND_ROUTE,
    "amount": KIND_AMOUNT,
    "clarify": KIND_CLARIFY,
    "delete_verify": KIND_YES_NO,
    "delete_confirm": KIND_YES_NO,
    "delete_pick": KIND_ENUM,
    "add_modify_choice": KIND_ENUM,
    "modify_target": KIND_ENUM,
    "submit_confirm": KIND_YES_NO,
    "more_lines": KIND_FREE_TEXT,
}

_DURATION_ANSWER_RE = re.compile(
    r"^(?:ha+|haa|hmm+|hm+|yes|yeah|yep|yup|ok|okay|ji|jee|thik|ঠিক)?\s*"
    r"(?P<n>\d{1,3})\s*(?:days?|din|দিন)\s*[!.?…]*\s*$",
    re.I,
)


@dataclass(frozen=True)
class PromptContext:
    domain: str | None
    slot: str | None
    kind: str = KIND_NONE


def derive_prompt_context(snapshot: SessionSnapshot) -> PromptContext:
    """What the bot is waiting for — derived from pending wizard state."""
    return derive_prompt_context_fields(
        duplicate_leave_choice_pending=snapshot.duplicate_leave_choice_pending,
        leave_active=snapshot.leave_active,
        leave_review_pending=snapshot.leave_review_pending,
        leave_submit_confirm_pending=snapshot.leave_submit_confirm_pending,
        pending_leave_step=snapshot.pending_leave_step,
        expense_active=snapshot.expense_active,
        expense_review_pending=snapshot.expense_review_pending,
        expense_delete_verify_pending=snapshot.expense_delete_verify_pending,
        expense_submit_confirm_pending=snapshot.expense_submit_confirm_pending,
        pending_expense_step=snapshot.pending_expense_step,
    )


def derive_prompt_context_fields(
    *,
    duplicate_leave_choice_pending: bool = False,
    leave_active: bool = False,
    leave_review_pending: bool = False,
    leave_submit_confirm_pending: bool = False,
    pending_leave_step: str | None = None,
    expense_active: bool = False,
    expense_review_pending: bool = False,
    expense_delete_verify_pending: bool = False,
    expense_submit_confirm_pending: bool = False,
    pending_expense_step: str | None = None,
    expense_active_prompt_kind: str | None = None,
) -> PromptContext:
    """Prompt context from raw session flags (no full snapshot required)."""
    if duplicate_leave_choice_pending:
        return PromptContext(domain="leave", slot="duplicate_choice", kind=KIND_ENUM)

    if expense_review_pending:
        return PromptContext(domain="expense", slot="review_confirm", kind=KIND_YES_NO)

    if leave_review_pending or leave_submit_confirm_pending:
        return PromptContext(domain="leave", slot="review_confirm", kind=KIND_YES_NO)

    if leave_active and pending_leave_step:
        slot = str(pending_leave_step).strip().lower()
        if slot == "dates":
            slot = "leave_dates"
        kind = _LEAVE_SLOT_KIND.get(slot, KIND_FREE_TEXT)
        if slot == "leave_dates":
            kind = KIND_DATE
        return PromptContext(domain="leave", slot=slot, kind=kind)

    prompt_kind = (expense_active_prompt_kind or "").strip().lower()
    if prompt_kind:
        slot = prompt_kind
        if prompt_kind == "delete_verify":
            slot = "delete_verify"
        kind = _EXPENSE_SLOT_KIND.get(slot, KIND_ENUM)
        return PromptContext(domain="expense", slot=slot, kind=kind)

    if expense_delete_verify_pending:
        return PromptContext(domain="expense", slot="delete_verify", kind=KIND_YES_NO)

    if expense_submit_confirm_pending:
        return PromptContext(domain="expense", slot="submit_confirm", kind=KIND_YES_NO)

    if expense_active and pending_expense_step:
        slot = str(pending_expense_step).strip().lower()
        kind = _EXPENSE_SLOT_KIND.get(slot, KIND_FREE_TEXT)
        return PromptContext(domain="expense", slot=slot, kind=kind)

    return PromptContext(domain=None, slot=None, kind=KIND_NONE)


def snapshot_has_pending_prompt(snapshot: SessionSnapshot) -> bool:
    ctx = derive_prompt_context(snapshot)
    return ctx.kind != KIND_NONE and bool(ctx.domain) and bool(ctx.slot)


def message_plausibly_answers_prompt(message: str, snapshot: SessionSnapshot) -> bool:
    """True when the user message likely answers the pending wizard prompt."""
    ctx = derive_prompt_context(snapshot)
    if ctx.kind == KIND_NONE or not ctx.domain or not ctx.slot:
        return False
    msg = (message or "").strip()
    if not msg:
        return False

    if ctx.domain == "leave":
        return _leave_message_answers_prompt(msg, ctx)
    if ctx.domain == "expense":
        return _expense_message_answers_prompt(msg, ctx)
    return False


def _leave_slot_preempted_by_expense(message: str) -> bool:
    """Strong expense claim lines are workflow switches, not leave slot answers."""
    from chat.services.intent_detector import (
        _strong_expense_claim,
        _strong_expense_day_summary,
    )
    from chat.services.expense_extraction import message_contains_expense_claim_lines

    if _strong_expense_claim(message) or _strong_expense_day_summary(message):
        return True
    return bool(message_contains_expense_claim_lines(message))


def _leave_message_answers_prompt(message: str, ctx: PromptContext) -> bool:
    from chat.services.intent_detector import _message_answers_wizard_step
    from chat.services.leave_balance_intent import is_leave_balance_query

    if is_leave_balance_query(message):
        return False

    if _leave_slot_preempted_by_expense(message):
        return False

    if ctx.kind == KIND_YES_NO:
        from chat.services.leave_confirm import is_confirmation_cancel, is_confirmation_yes
        from chat.services.workflow_suspend import wants_resume_suspended_leave

        if wants_resume_suspended_leave(message):
            return False
        return is_confirmation_yes(message) or is_confirmation_cancel(message)

    slot = ctx.slot or ""
    if _message_answers_wizard_step(message, slot):
        return True

    if ctx.kind in (KIND_DATE, KIND_DURATION):
        if _DURATION_ANSWER_RE.match(message):
            return True
        if re.search(r"\d", message) and re.search(
            r"(?:days?|din|দিন|tomorrow|kal|kalke|agamikal|আগামীকাল|today|ajke|আজ)",
            message,
            re.I | re.UNICODE,
        ):
            return True

    if ctx.kind == KIND_FREE_TEXT and len(message.strip()) >= 3:
        from chat.services.intent_detector import looks_like_wizard_side_question
        from chat.services.leave_balance_intent import is_leave_balance_query

        if is_leave_balance_query(message):
            return False
        try:
            from chat.services.expense_workflow import wants_expense_summary
            from chat.services.leave_meta_queries import wants_leave_session_summary

            if wants_expense_summary(message) or wants_leave_session_summary(message):
                return False
        except Exception:
            pass
        try:
            from chat.services.intent_detector import _strong_expense_claim

            if _strong_expense_claim(message):
                return False
        except Exception:
            pass

        if looks_like_wizard_side_question(message):
            return False
        try:
            from chat.services.wizard_turn_gate import is_casual_wizard_side_statement

            if is_casual_wizard_side_statement(message):
                return False
        except Exception:
            pass
        return True

    return False


def _expense_message_answers_prompt(message: str, ctx: PromptContext) -> bool:
    from chat.services.workflow_navigation import is_leave_application_message

    if is_leave_application_message(message):
        return False
    try:
        from chat.services.policy_intent_helpers import is_expense_entitlement_query, is_rules_query
        from chat.services.intent_detector import _strong_hr_policy

        if is_expense_entitlement_query(message) or (
            _strong_hr_policy(message) and is_rules_query(message)
        ):
            return False
    except Exception:
        pass

    slot = (ctx.slot or "").strip().lower()

    if ctx.kind == KIND_YES_NO:
        from chat.services.leave_confirm import is_confirmation_cancel, is_confirmation_yes

        return is_confirmation_yes(message) or is_confirmation_cancel(message)

    if slot == "clarify" or ctx.kind == KIND_CLARIFY:
        from chat.services.expense.clarify import looks_like_clarify_reply_signal

        return looks_like_clarify_reply_signal(message)

    if slot == "from_to" or ctx.kind == KIND_ROUTE:
        from chat.services.expense_extraction import _looks_like_route_answer

        return _looks_like_route_answer(message)

    if slot == "delete_pick":
        from chat.services.expense.delete_flow import message_answers_delete_pick

        return message_answers_delete_pick(message)

    if slot == "add_modify_choice":
        from chat.services.expense.add_modify import parse_add_modify_choice_reply

        return parse_add_modify_choice_reply(message) is not None

    if slot == "modify_target":
        from chat.services.expense.modify_flow import parse_modify_target_number

        return parse_modify_target_number(message) is not None

    if slot in ("category", "more_lines"):
        from chat.services.expense.routing import looks_like_expense_wizard_continuation

        if looks_like_expense_wizard_continuation(message):
            return True

    if ctx.kind == KIND_AMOUNT and re.search(r"\d", message):
        return True

    if ctx.kind == KIND_FREE_TEXT and len(message.strip()) >= 2:
        return True

    return False


def build_slot_aware_clarification(
    message: str,
    snapshot: SessionSnapshot,
    *,
    lang: str | None = None,
) -> str | None:
    """
    When we must clarify, tailor options to the active prompt when possible.
    Returns None to fall back to generic clarification copy.
    """
    ctx = derive_prompt_context(snapshot)
    if ctx.kind == KIND_NONE:
        return None

    from chat.services.message_context_clarity import build_context_clarification_message
    from chat.services.translator import detect_user_language

    user_lang = lang or detect_user_language(message)

    if ctx.domain == "leave" and ctx.slot == "leave_dates":
        if user_lang == "bn":
            return (
                "ছুটি **কোন তারিখ** থেকে শুরু হবে, একটু স্পষ্ট করবেন?\n\n"
                "উদাহরণ:\n"
                "• **আগামীকাল** থেকে ৩ দিন\n"
                "• **২০২৬-০৬-১৫** থেকে **২০২৬-০৬-১৭**"
            )
        return (
            "Could you clarify **which dates** your leave should start from?\n\n"
            "For example:\n"
            "• **Tomorrow** for 3 days\n"
            "• **2026-06-15** to **2026-06-17**"
        )

    if ctx.domain == "leave" and ctx.slot == "leave_type":
        if user_lang == "bn":
            return (
                "কোন ধরনের ছুটি নিতে চান?\n\n"
                "• **Annual leave** (বেতনসহ)\n"
                "• **Leave without pay** (বেতন ছাড়া)"
            )
        return (
            "Which leave type do you want?\n\n"
            "• **Annual leave** (paid)\n"
            "• **Leave without pay** (unpaid)"
        )

    if ctx.domain == "leave" and ctx.slot == "reason":
        if user_lang == "bn":
            return (
                "ছুটির **কারণ** একটু বলবেন?\n\n"
                "উদাহরণ: family program, medical check-up, personal work"
            )
        return (
            "What is the **reason** for your leave?\n\n"
            "Example: family program, medical check-up, personal work"
        )

    if ctx.domain == "expense" and ctx.slot == "add_modify_choice":
        if user_lang == "en":
            return (
                "This line may already be in your draft.\n\n"
                "• **add korbo** — add a new line\n"
                "• **modify korbo** — update an existing line"
            )
        return (
            "এই line draft-এ থাকতে পারে।\n\n"
            "• **add korbo** — নতুন line যোগ\n"
            "• **modify korbo** — আগের line বদলান"
        )

    if ctx.domain == "expense" and ctx.slot == "modify_target":
        if user_lang == "en":
            return (
                "Which **line number** should I update?\n\n"
                "Reply with the number — e.g. `1` or `2`."
            )
        return "কোন **নম্বরের** line বদলাবেন? — যেমন: `1` বা `2`"

    if ctx.domain == "expense" and ctx.slot == "delete_pick":
        if user_lang == "en":
            return (
                "Which entry should I delete? Reply with the **line number** "
                "(e.g. `2`) or `lunch 120 baad daw`."
            )
        return (
            "Kon entry delete korbo? **নম্বর** বলুন (যেমন: `2`) "
            "অথবা `lunch 120 baad daw`।"
        )

    return build_context_clarification_message(
        message,
        list(snapshot.context_lines),
        lang=lang,
    )
