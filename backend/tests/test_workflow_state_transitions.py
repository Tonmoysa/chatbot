"""Parametrized workflow stage transitions (P3)."""

import datetime as dt

import pytest

from chat.services.expense.slots import STAGE_COLLECTING, STAGE_REVIEW, STAGE_SUBMIT_CONFIRM
from chat.services.expense_workflow import process_expense_turn
from chat.services.leave_workflow import process_leave_turn


@pytest.mark.parametrize(
    "message,expected_stage",
    [
        ("lunch 100, bus 50", STAGE_COLLECTING),
        ("lunch 100, done", STAGE_REVIEW),
    ],
)
def test_expense_collecting_to_review(monkeypatch, message, expected_stage):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    r = process_expense_turn(workflow_state={}, message=message)
    block = r["workflow_state"].get("expense_request") or {}
    assert block.get("stage") == expected_stage


def test_expense_review_yes_to_submit_confirm(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    wf = {}
    r1 = process_expense_turn(workflow_state=wf, message="lunch 100, done")
    wf = r1["workflow_state"]
    r2 = process_expense_turn(workflow_state=wf, message="yes")
    block = r2["workflow_state"].get("expense_request") or {}
    assert block.get("stage") == STAGE_SUBMIT_CONFIRM


def test_expense_submit_confirm_no_back_to_review(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    wf = {}
    r1 = process_expense_turn(workflow_state=wf, message="lunch 100, done")
    wf = r1["workflow_state"]
    r2 = process_expense_turn(workflow_state=wf, message="yes")
    wf = r2["workflow_state"]
    r3 = process_expense_turn(workflow_state=wf, message="no")
    block = r3["workflow_state"].get("expense_request") or {}
    assert block.get("stage") == STAGE_REVIEW


def test_review_submit_praise_advances_to_submit_confirm(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    wf = {}
    r1 = process_expense_turn(workflow_state=wf, message="lunch 100, done")
    wf = r1["workflow_state"]
    r2 = process_expense_turn(
        workflow_state=wf, message="awesome perfect ...submit koro"
    )
    block = r2["workflow_state"].get("expense_request") or {}
    assert block.get("stage") == STAGE_SUBMIT_CONFIRM
    assert "CRM" in (r2.get("question") or "") or "Submit expense" in (
        r2.get("question") or ""
    )


def test_expense_review_fallback_message_does_not_crash(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    wf = {}
    r1 = process_expense_turn(workflow_state=wf, message="lunch 100, done")
    wf = r1["workflow_state"]
    r2 = process_expense_turn(workflow_state=wf, message="expense e jao")
    block = r2["workflow_state"].get("expense_request") or {}
    assert block.get("stage") == STAGE_REVIEW
    assert r2.get("question")


def test_leave_reaches_review_pending(monkeypatch):
    fixed = dt.date(2026, 6, 4)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    wf = {}
    wf = process_leave_turn(
        workflow_state=wf,
        message="tomorrow paid full day family program",
        entities={},
    )["workflow_state"]
    assert wf.get("review_pending") or wf.get("status") == "active"
