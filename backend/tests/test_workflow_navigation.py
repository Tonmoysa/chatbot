"""Cross-workflow navigation phrases (leave ↔ expense)."""

import pytest

from chat.services.workflow_suspend import wants_resume_suspended_leave


@pytest.mark.parametrize(
    "message",
    [
        "leave e back koro",
        "leave e asho",
        "leave request e back koro",
        "chuti e back koro",
        "ছুটি তে ফিরে যাও",
        "back to leave",
    ],
)
def test_resume_suspended_leave_phrases(message: str) -> None:
    assert wants_resume_suspended_leave(message)


@pytest.mark.parametrize(
    "message",
    [
        "expense e back koro",
        "summery",
        "snack 70 taka",
    ],
)
def test_resume_leave_not_triggered_by_expense_slot_work(message: str) -> None:
    assert not wants_resume_suspended_leave(message)
