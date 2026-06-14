"""
Expense prompt predicates for session_turn_router (no routing decisions here).
"""

from __future__ import annotations

from typing import Any

from chat.services.expense.active_prompt import (
    KIND_ADD_MODIFY_CHOICE,
    KIND_DELETE_CONFIRM,
    KIND_DELETE_PICK,
    active_prompt_kind,
    read_active_prompt,
)
from chat.services.expense.delete_disambiguation_pending import (
    has_delete_disambiguation_pending,
)


def expense_active_prompt_kind(block: dict[str, Any] | None) -> str | None:
    kind = active_prompt_kind(block)
    if kind:
        return kind
    if has_delete_disambiguation_pending(block):
        return KIND_DELETE_PICK
    return None


def snapshot_expense_prompt_kind(snapshot: Any) -> str | None:
    return getattr(snapshot, "expense_active_prompt_kind", None) or None


def snapshot_has_expense_interactive_prompt(snapshot: Any) -> bool:
    return bool(snapshot_expense_prompt_kind(snapshot))


def message_abandons_expense_prompt(
    message: str,
    block: dict[str, Any] | None = None,
) -> bool:
    """True when user starts a new command instead of answering the active prompt."""
    from chat.services.expense.interactive_pending import (
        message_abandons_expense_interactive_pending,
    )

    if message_abandons_expense_interactive_pending(message, block):
        return True
    try:
        from chat.services.leave_balance_intent import is_leave_balance_query
        from chat.services.policy_intent_helpers import is_expense_entitlement_query, is_rules_query
        from chat.services.intent_detector import _strong_hr_policy
        from chat.services.leave.session_action_memory import wants_leave_meta_question
        from chat.services.expense.session_action_memory import wants_expense_meta_question

        if is_leave_balance_query(message):
            return True
        if is_expense_entitlement_query(message) or (
            _strong_hr_policy(message) and is_rules_query(message)
        ):
            return True
        if wants_leave_meta_question(message) or wants_expense_meta_question(message):
            return True
    except Exception:
        pass
    return False


def apply_expense_interactive_clear_from_router(
    workflow_state: dict[str, Any],
    decision: Any,
    message: str,
) -> dict[str, Any]:
    """Orchestrator: clear stale expense prompts per router decision (spec P02e)."""
    from chat.services.expense.expense_fsm import read_expense_block
    from chat.services.expense.interactive_pending import clear_expense_interactive_pending

    wf = dict(workflow_state or {})
    block = read_expense_block(wf)
    if not isinstance(block, dict) or not block:
        return workflow_state or {}
    should_clear = bool((getattr(decision, "flags", None) or {}).get("clear_expense_interactive"))
    if not should_clear and expense_active_prompt_kind(block):
        if message_abandons_expense_prompt(message, block) or router_clears_expense_interactive_prompt(
            decision
        ):
            should_clear = True
    if not should_clear:
        return workflow_state or {}
    clear_expense_interactive_pending(block)
    wf["expense_request"] = block
    return wf


def router_clears_expense_interactive_prompt(decision: Any) -> bool:
    """Orchestrator should clear stale expense prompts before executing this decision."""
    kind = getattr(getattr(decision, "turn_kind", None), "value", "") or ""
    return kind in {
        "submit_command",
        "policy_query",
        "balance_query",
        "workflow_switch",
        "new_expense",
        "new_leave",
        "out_of_scope",
        "summary",
        "meta_question",
        "cancel",
    }
