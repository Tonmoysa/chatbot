"""Rule-based workflow turn classification."""

from chat.services.turn_classifier import (
    TURN_CHITCHAT,
    TURN_CONFIRM,
    TURN_CORRECTION,
    TURN_POLICY_QUERY,
    TURN_SLOT_ANSWER,
    classify_workflow_turn,
    is_workflow_continuation_turn,
)


def test_paid_is_slot_not_chitchat():
    turn = classify_workflow_turn(
        "paid",
        leave_active=True,
        expense_active=False,
        pending_leave_step="leave_payment_category",
    )
    assert turn == TURN_SLOT_ANSWER
    assert is_workflow_continuation_turn(turn)


def test_hi_during_leave_is_chitchat():
    turn = classify_workflow_turn(
        "hi",
        leave_active=True,
        expense_active=False,
        pending_leave_step="leave_payment_category",
    )
    assert turn == TURN_CHITCHAT


def test_ha_during_expense_clarify_is_slot_answer():
    turn = classify_workflow_turn(
        "ha",
        leave_active=False,
        expense_active=True,
        pending_expense_step="clarify",
    )
    assert turn == TURN_SLOT_ANSWER
    assert turn != TURN_CHITCHAT
    assert is_workflow_continuation_turn(turn)


def test_expense_correction_turn():
    turn = classify_workflow_turn(
        "bus 50 না 70 হবে",
        leave_active=False,
        expense_active=True,
    )
    assert turn == TURN_CORRECTION


def test_policy_query_during_expense():
    turn = classify_workflow_turn(
        "amake leave policy ta bolo",
        leave_active=False,
        expense_active=True,
    )
    assert turn == TURN_POLICY_QUERY


def test_yes_during_expense_is_confirm():
    turn = classify_workflow_turn(
        "yes",
        leave_active=False,
        expense_active=True,
    )
    assert turn == TURN_CONFIRM
