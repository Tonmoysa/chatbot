"""Expense wizard routing heuristics."""

from chat.services.expense.routing import looks_like_expense_wizard_continuation
from chat.services.policy_intent_helpers import is_general_knowledge_out_of_scope
from chat.services.turn_classifier import (
    TURN_CHITCHAT,
    TURN_CORRECTION,
    TURN_SLOT_ANSWER,
    classify_workflow_turn,
    is_workflow_continuation_turn,
)


def test_eid_kobe_during_expense_is_not_slot_answer():
    turn = classify_workflow_turn(
        "eid kobe?",
        leave_active=False,
        expense_active=True,
    )
    assert turn == TURN_CHITCHAT
    assert turn != TURN_SLOT_ANSWER


def test_lunch_100_during_expense_is_slot_answer():
    turn = classify_workflow_turn(
        "lunch 100",
        leave_active=False,
        expense_active=True,
    )
    assert turn in (TURN_SLOT_ANSWER, TURN_CORRECTION)
    assert is_workflow_continuation_turn(turn)


def test_eid_kobe_not_expense_continuation():
    assert not looks_like_expense_wizard_continuation("eid kobe?")
    assert is_general_knowledge_out_of_scope("eid kobe?")


def test_bus_50_is_expense_continuation():
    assert looks_like_expense_wizard_continuation("bus 50 office to home")
