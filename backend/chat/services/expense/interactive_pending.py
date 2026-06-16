"""
Transient expense wizard UI states (delete disambiguation, verify, etc.).

These must not survive workflow switches (leave ↔ expense) or explicit new commands.
"""

from __future__ import annotations

import re
from typing import Any


def clear_expense_interactive_pending(block: dict[str, Any]) -> dict[str, Any]:
    """Drop abandoned delete/amount confirm prompts — not draft lines or from_to slots."""
    from chat.services.expense.active_prompt import clear_active_prompt
    from chat.services.expense.amount_correction_pending import (
        clear_amount_correction_pending,
    )
    from chat.services.expense.route_correction_pending import (
        clear_route_correction_pending,
    )
    from chat.services.expense.delete_disambiguation_pending import (
        clear_delete_disambiguation_pending,
    )
    from chat.services.expense.expense_confirm import (
        clear_expense_delete_verify,
        clear_ordinal_amount_confirm,
    )

    clear_active_prompt(block)
    clear_delete_disambiguation_pending(block)
    clear_expense_delete_verify(block)
    clear_ordinal_amount_confirm(block)
    clear_amount_correction_pending(block)
    clear_route_correction_pending(block)
    return block


def message_answers_expense_interactive_pending(
    message: str,
    block: dict[str, Any] | None,
) -> bool:
    """True when the user is answering an open delete/amount/modify prompt."""
    if not isinstance(block, dict):
        return False
    text = (message or "").strip()
    if not text:
        return False

    from chat.services.expense.amount_correction_pending import (
        has_amount_correction_pending,
        read_amount_correction_pending,
    )
    from chat.services.expense.amount_correction_pending import (
        _parse_amount_hint,
    )
    from chat.services.expense.command_parser import _ordinal_index_from_message
    from chat.services.expense_extraction import parse_category_token

    if has_amount_correction_pending(block):
        pending = read_amount_correction_pending(block) or {}
        if _parse_amount_hint(text) is not None:
            return True
        if _ordinal_index_from_message(text.lower(), item_count=99) is not None:
            return True
        cat = parse_category_token(text)
        if cat and len(text.split()) <= 3:
            return True
        if pending.get("category") and cat:
            return True

    from chat.services.expense.route_correction_pending import (
        has_route_correction_pending,
    )

    if has_route_correction_pending(block):
        if _ordinal_index_from_message(text.lower(), item_count=99) is not None:
            return True
        if re.match(r"^#?\d{1,2}\s*$", text):
            return True

    from chat.services.expense.active_prompt import (
        KIND_ADD_MODIFY_CHOICE,
        KIND_DELETE_CONFIRM,
        KIND_DELETE_PICK,
        KIND_MODIFY_TARGET,
        read_active_prompt,
    )
    from chat.services.expense.delete_disambiguation_pending import (
        has_delete_disambiguation_pending,
    )

    prompt = read_active_prompt(block)
    kind = str((prompt or {}).get("kind") or "")
    if kind == KIND_DELETE_PICK or has_delete_disambiguation_pending(block):
        from chat.services.expense.delete_flow import message_answers_delete_pick

        if message_answers_delete_pick(text, block):
            return True
    if kind == KIND_DELETE_CONFIRM:
        from chat.services.expense.expense_confirm import (
            is_confirmation_no,
            is_confirmation_yes,
        )

        if is_confirmation_yes(text) or is_confirmation_no(text):
            return True
    if kind == KIND_ADD_MODIFY_CHOICE:
        from chat.services.expense.add_modify import parse_add_modify_choice_reply

        if parse_add_modify_choice_reply(text, block):
            return True
    if kind == KIND_MODIFY_TARGET:
        from chat.services.expense.modify_flow import parse_modify_target_number

        if parse_modify_target_number(text) is not None:
            return True

    from chat.services.expense.expense_confirm import (
        has_ordinal_amount_confirm_pending,
        is_expense_delete_verify_pending,
    )

    if has_ordinal_amount_confirm_pending(block):
        from chat.services.expense.expense_confirm import (
            is_confirmation_no,
            is_confirmation_yes,
        )

        if is_confirmation_yes(text) or is_confirmation_no(text):
            return True
        if _parse_amount_hint(text) is not None:
            return True

    if is_expense_delete_verify_pending(block):
        from chat.services.expense.expense_confirm import (
            is_confirmation_no,
            is_confirmation_yes,
        )

        if is_confirmation_yes(text) or is_confirmation_no(text):
            return True

    return False


def message_abandons_expense_interactive_pending(
    message: str,
    block: dict[str, Any] | None = None,
) -> bool:
    """True when the user is starting a new command, not answering delete/confirm."""
    from chat.services.expense.wizard_commands import (
        wants_cancel_expense_command,
        wants_expense_done_command_rules,
        wants_expense_submit_command,
    )
    from chat.services.expense_workflow import wants_expense_summary
    from chat.services.intent_detector import _strong_expense_claim
    from chat.services.workflow_navigation import is_leave_application_message

    text = (message or "").strip()
    if not text:
        return False
    if message_answers_expense_interactive_pending(text, block):
        return False
    if wants_expense_submit_command(text):
        return True
    if wants_cancel_expense_command(text):
        return True
    if wants_expense_done_command_rules(text):
        return True
    if wants_expense_summary(text):
        return True
    if _strong_expense_claim(text):
        return True
    if is_leave_application_message(text):
        return True
    from chat.services.expense.expense_confirm import (
        looks_like_bare_delete_request,
        parse_ordinal_delete_index,
    )

    if parse_ordinal_delete_index(text) is not None:
        return True
    if looks_like_bare_delete_request(text):
        return True
    if re.search(r"\b(delete|remove|বাদ|মুছ)\b", text, re.I | re.UNICODE):
        return True
    return False
