"""Out-of-scope and policy/status predicates with confidence scores."""

from __future__ import annotations


def probe_out_of_scope(message: str) -> tuple[bool, float, str]:
    try:
        from chat.services.policy_intent_helpers import (
            is_general_knowledge_out_of_scope,
            is_off_topic_for_hr_assistant,
        )

        if is_off_topic_for_hr_assistant(message):
            return True, 0.95, "off_topic"
        if is_general_knowledge_out_of_scope(message):
            return True, 0.9, "general_knowledge"
    except Exception:
        pass
    return False, 0.0, ""


def probe_policy_query(message: str) -> tuple[bool, float]:
    try:
        from chat.services.policy_intent_helpers import is_expense_entitlement_query, is_rules_query
        from chat.services.intent_detector import _strong_hr_policy

        if is_expense_entitlement_query(message):
            return True, 0.92
        if _strong_hr_policy(message) and is_rules_query(message):
            return True, 0.9
    except Exception:
        pass
    return False, 0.0


def probe_balance_query(message: str) -> tuple[bool, float]:
    try:
        from chat.services.leave_balance_intent import is_leave_balance_query

        if is_leave_balance_query(message):
            return True, 0.92
    except Exception:
        pass
    return False, 0.0
