"""Warm praise during expense review — not generic wizard help."""

import pytest

from chat.services.expense.clarify_praise import (
    looks_like_wizard_praise_message,
    resolve_clarify_praise_for_review,
)
from chat.services.expense.slots import STAGE_REVIEW
from chat.services.expense.turn_parser import resolve_expense_turn
from chat.services.expense.turn_schema import TURN_NAVIGATE, TURN_PRAISE
from chat.services.expense_workflow import process_expense_turn
from chat.services.turn_classifier import TURN_CHITCHAT, TURN_SLOT_ANSWER, classify_workflow_turn


@pytest.mark.parametrize(
    "message",
    [
        "very good observation",
        "awesome perfectly detect korcho",
        "khub sundor handle korcho",
    ],
)
def test_looks_like_wizard_praise(message):
    assert looks_like_wizard_praise_message(message)


def test_pure_submit_not_praise():
    assert not looks_like_wizard_praise_message("submit koro")


def test_praise_with_submit_still_praise():
    assert looks_like_wizard_praise_message("okay awesome ekhon submit koro")


def test_turn_classifier_praise_not_chitchat():
    turn = classify_workflow_turn(
        "very good observation",
        leave_active=False,
        expense_active=True,
        expense_review_pending=True,
    )
    assert turn == TURN_SLOT_ANSWER
    assert turn != TURN_CHITCHAT


def test_turn_classifier_praise_submit_continues_wizard():
    turn = classify_workflow_turn(
        "okay awesome ekhon submit koro",
        leave_active=False,
        expense_active=True,
        expense_review_pending=True,
    )
    assert turn == TURN_SLOT_ANSWER


def test_resolve_praise_regex_fallback():
    ctx = resolve_clarify_praise_for_review(
        "very good observation",
        lang="en",
        use_llm=False,
        wizard_stage="review",
    )
    assert ctx is not None
    assert ctx.is_praise
    assert ctx.ack_text


def test_review_praise_submit_ask_not_full_list(monkeypatch):
    monkeypatch.setattr(
        "chat.services.expense.clarify_praise_llm.clarify_praise_llm_enabled",
        lambda **k: False,
    )
    wf = {
        "expense_request": {
            "stage": STAGE_REVIEW,
            "items": [
                {"category": "Bus", "amount": 50, "from_location": "a", "to_location": "b"},
            ],
            "incurred_date_iso": "2026-06-09",
        }
    }
    pack = process_expense_turn(
        workflow_state=wf,
        message="okay thank you",
    )
    q = pack.get("question") or ""
    assert "Expense form in progress" not in q
    assert "Bus" not in q
    assert "50" not in q
    assert "submit" in q.lower() or "জমা" in q or "হ্যাঁ" in q
    facts = pack.get("message_facts") or {}
    assert facts.get("message_type") == "expense_review_praise"
    decision = resolve_expense_turn(
        "very good observation",
        items=wf["expense_request"]["items"],
        stage=STAGE_REVIEW,
        use_llm=False,
    )
    assert decision.turn_type == TURN_PRAISE


def test_praise_submit_routes_navigate_not_praise_only():
    decision = resolve_expense_turn(
        "okay awesome ekhon submit koro",
        items=[{"category": "Lunch", "amount": 100}],
        stage=STAGE_REVIEW,
        use_llm=False,
    )
    assert decision.turn_type == TURN_NAVIGATE
    assert decision.submit_draft
