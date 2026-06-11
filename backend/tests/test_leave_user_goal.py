"""Leave user-goal classification (informational vs slot)."""

import pytest

from chat.services.leave.user_goal import (
    UserGoal,
    classify_leave_user_goal,
    is_informational_leave_goal,
    needs_leave_goal_clarification,
)
from chat.services.leave_balance_intent import is_leave_balance_query


@pytest.mark.parametrize(
    "message",
    [
        "amar leave koyta?",
        "amar sick leave koyta ache?",
        "amar koyta leave ache",
    ],
)
def test_balance_phrases_are_informational(message: str):
    assert is_leave_balance_query(message)
    goal = classify_leave_user_goal(message, leave_active=True)
    assert is_informational_leave_goal(goal)
    assert goal == UserGoal.QUERY_BALANCE


def test_sick_leave_without_question_is_slot():
    goal = classify_leave_user_goal("sick leave", leave_active=True)
    assert goal == UserGoal.ANSWER_SLOT
    assert not needs_leave_goal_clarification("sick leave", leave_active=True)
