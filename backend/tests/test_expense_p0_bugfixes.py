"""P0 regression: submit false-positive, amount correction, post-submit edit policy."""

import pytest

from chat.services.expense.command_executor import execute_correction_plan
from chat.services.expense.command_parser import parse_correction_plan
from chat.services.expense.done_intent_llm import (
    clear_done_intent_cache,
    parse_finish_collecting_llm,
)
from chat.services.expense.expense_confirm import looks_like_expense_correction
from chat.services.expense.session_action_memory import (
    format_meta_question_answer,
    record_expense_submitted,
    wants_expense_meta_question,
    wants_post_submit_edit_question,
)
from chat.services.expense.wizard_commands import wants_expense_submit_command
from chat.services.expense_workflow import process_expense_turn, wants_expense_summary


@pytest.mark.parametrize(
    "message",
    [
        "submit",
        "submit please",
        "submit koro",
        "joma daw",
        "yes submit",
        "হ্যাঁ submit",
    ],
)
def test_submit_command_still_matches_imperatives(message):
    assert wants_expense_submit_command(message)


@pytest.mark.parametrize(
    "message",
    [
        "can i edit it after submit?",
        "can I change it after submit",
        "submit kora hoise ki?",
        "already submitted yet?",
    ],
)
def test_submit_command_rejects_questions(message):
    assert not wants_expense_submit_command(message)


def test_post_submit_edit_meta_detected():
    assert wants_post_submit_edit_question("can i edit it after submit?")
    assert wants_expense_meta_question("can i edit it after submit?")


def test_post_submit_edit_answer_no_edit_allowed():
    wf = record_expense_submitted(
        {},
        items=[{"category": "Bus", "amount": 500}],
        reference_id="EXP-2026-C7A365",
        incurred_date_iso="2026-06-09",
    )
    wf["expense_last_submission"] = {
        "reference_id": "EXP-2026-C7A365",
        "items": [{"category": "Bus", "amount": 500}],
    }
    ans = format_meta_question_answer(wf, "can i edit it after submit?", lang="en")
    assert ans
    assert "cannot be edited" in ans.lower() or "can't" in ans.lower()
    assert "EXP-2026-C7A365" in ans


def test_amount_instead_of_correction_detected():
    msg = "ok, then use 400 instead of 4000"
    assert looks_like_expense_correction(msg)


def test_amount_instead_of_correction_applied_single_line():
    plan = parse_correction_plan("use 400 instead of 4000")
    items = [{"category": "Bus", "amount": 500, "from_location": "dhaka", "to_location": "bhola"}]
    result = execute_correction_plan(items, plan)
    assert result.changed
    assert float(result.items[0]["amount"]) == 400.0


def test_wants_expense_summary_does_not_treat_ok_correction_as_summary():
    msg = "ok, then use 400 instead of 4000"
    assert not wants_expense_summary(msg)


def test_done_intent_llm_cached_per_trace(monkeypatch):
    calls: list[str] = []

    def fake_chat_json(self, **kwargs):
        calls.append(kwargs.get("user_prompt") or "")
        return {"finish_collecting": False, "confidence": 0.9}

    monkeypatch.setattr(
        "chat.services.expense.done_intent_llm.LLMClient.is_configured",
        lambda self: True,
    )
    monkeypatch.setattr(
        "chat.services.expense.done_intent_llm.LLMClient.chat_json",
        fake_chat_json,
    )
    clear_done_intent_cache("trace-cache")
    msg = "everything seems fine now"
    parse_finish_collecting_llm(msg, trace_id="trace-cache")
    parse_finish_collecting_llm(msg, trace_id="trace-cache")
    assert len(calls) == 1


def test_edit_after_submit_not_collecting_prompt(monkeypatch):
    fixed = __import__("datetime").date(2026, 6, 9)

    class FixedDate(type(fixed)):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr("chat.services.expense_workflow.date", FixedDate)

    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "incurred_date_iso": "2026-06-09",
            "items": [{"category": "Bus", "amount": 500, "from_location": "dhaka", "to_location": "bhola"}],
        }
    }
    pack = process_expense_turn(
        workflow_state=wf,
        message="can i edit it after submit?",
        trace_id="t-edit-q",
    )
    q = (pack.get("question") or "").lower()
    assert "more expenses today" not in q
    assert "got it for" not in q
