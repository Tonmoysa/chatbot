"""Regression: submit_confirm accepts yes/submit/chip; done-intent LLM kwargs."""

from datetime import date

import pytest

from chat.services.expense.done_collecting import done_intent_llm_should_use
from chat.services.expense.expense_confirm import is_submit_confirm_yes
from chat.services.expense_workflow import handle_submit_confirm_turn, process_expense_turn


def _submit_confirm_wf() -> dict:
    return {
        "expense_request": {
            "active": True,
            "stage": "submit_confirm",
            "incurred_date_iso": "2026-06-09",
            "items": [
                {"category": "Lunch", "amount": 100},
                {"category": "Bus", "amount": 50, "from_location": "a", "to_location": "b"},
            ],
            "reply_language": "banglish",
        }
    }


@pytest.mark.parametrize(
    "message",
    ["yes", "হ্যাঁ", "submit", "submit please", "submit koro", "joma daw", "yes submit"],
)
def test_is_submit_confirm_yes_accepts_final_gate_phrases(message):
    assert is_submit_confirm_yes(message)


def test_done_intent_llm_skips_bare_yes_and_review_stage():
    assert not done_intent_llm_should_use("yes")
    assert not done_intent_llm_should_use("ok")
    assert not done_intent_llm_should_use("yes", wizard_stage="submit_confirm")
    assert not done_intent_llm_should_use("everything is perfect", wizard_stage="review")


def test_done_intent_llm_uses_correct_chat_json_kwargs(monkeypatch):
    captured: dict = {}

    def fake_chat_json(**kwargs):
        captured.update(kwargs)
        return {"finish_collecting": False, "confidence": 0.9}

    monkeypatch.setattr(
        "chat.services.expense.done_intent_llm.LLMClient.is_configured",
        lambda self: True,
    )
    monkeypatch.setattr(
        "chat.services.expense.done_intent_llm.LLMClient.chat_json",
        lambda self, **kwargs: fake_chat_json(**kwargs),
    )
    from chat.services.expense.done_intent_llm import parse_finish_collecting_llm

    parse_finish_collecting_llm("everything seems fine now", trace_id="t-done")
    assert "system_prompt" in captured
    assert "user_prompt" in captured
    assert captured["user_prompt"] == "everything seems fine now"


@pytest.fixture
def fixed_june_9(monkeypatch):
    fixed = date(2026, 6, 9)

    class FixedDate(date):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr("chat.services.expense_workflow.date", FixedDate)


@pytest.mark.parametrize(
    "message",
    ["yes", "submit please", "submit koro", "joma daw"],
)
def test_submit_confirm_turn_submits(message, fixed_june_9):
    wf = _submit_confirm_wf()
    pack = handle_submit_confirm_turn(
        wf,
        wf["expense_request"],
        wf["expense_request"]["items"],
        message,
        inc_iso="2026-06-09",
        day_logged_total=0.0,
        daily_cap=300.0,
        lang="banglish",
    )
    assert pack.get("submitted")
    assert pack.get("complete")


def test_submit_confirm_via_process_expense_turn(fixed_june_9):
    wf = _submit_confirm_wf()
    pack = process_expense_turn(workflow_state=wf, message="submit")
    assert pack.get("submitted")
    assert pack.get("complete")
