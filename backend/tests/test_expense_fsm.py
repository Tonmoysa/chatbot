"""Expense FSM helpers."""

from chat.services.expense.expense_fsm import (
    deactivate_expense_session,
    is_expense_collecting,
    is_expense_in_progress,
    normalize_expense_stage,
    pause_expense_session,
    read_expense_block,
    resume_expense_session,
    set_expense_stage,
)
from chat.services.expense.slots import STAGE_COLLECTING, STAGE_REVIEW, STAGE_SUBMIT_CONFIRM


def test_normalize_expense_stage_aliases():
    assert normalize_expense_stage("confirming") == STAGE_REVIEW
    assert normalize_expense_stage(STAGE_SUBMIT_CONFIRM) == STAGE_SUBMIT_CONFIRM


def test_pause_and_resume():
    wf = {"expense_request": {"active": True, "stage": STAGE_COLLECTING, "items": []}}
    paused = pause_expense_session(wf)
    assert is_expense_collecting(paused) is False
    assert read_expense_block(paused).get("paused") is True
    resumed = resume_expense_session(paused)
    assert is_expense_collecting(resumed) is True


def test_set_expense_stage():
    block: dict = {"stage": STAGE_COLLECTING}
    set_expense_stage(block, STAGE_REVIEW)
    assert block["stage"] == STAGE_REVIEW


def test_deactivate_clears_block():
    wf = {"expense_request": {"active": True}}
    out = deactivate_expense_session(wf)
    assert "expense_request" not in out
    assert is_expense_in_progress(out) is False
