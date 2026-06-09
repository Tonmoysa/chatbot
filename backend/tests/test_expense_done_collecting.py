"""Finish-collecting intent (rules + LLM) and incomplete-draft warm prompts."""

import pytest

from chat.services.expense.done_collecting import (
    detect_finish_collecting_intent,
    expense_draft_is_incomplete,
    format_done_incomplete_prompt,
    wants_expense_done_phrase,
)
from chat.services.expense.wizard_commands import (
    wants_expense_done_command_rules,
)
from chat.services.expense_workflow import process_expense_turn
from tests.test_bangla_expense_claim_intent import BANGLA_TEN_LINE_VOICE_DUMP


@pytest.mark.parametrize(
    "message",
    [
        "all done",
        "everything is perfect",
        "everything is okay",
        "that's all",
        "I'm done",
        "looks good",
        "sob thik",
    ],
)
def test_done_phrase_rules_or_patterns(message):
    assert detect_finish_collecting_intent(message, use_llm=False)


def test_done_rules_reject_clarify_line():
    msg = "145 taka bus, train 135 taka, bus 90 taka"
    assert not detect_finish_collecting_intent(msg, use_llm=False)


def test_done_llm_fallback(monkeypatch):
    monkeypatch.setattr(
        "chat.services.expense.done_intent_llm.parse_finish_collecting_llm",
        lambda message, trace_id="", use_llm=True: True,
    )
    assert detect_finish_collecting_intent(
        "yeah everything seems fine now", use_llm=True
    )


def test_all_done_incomplete_warm_list(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    pack = process_expense_turn(
        workflow_state={}, message=BANGLA_TEN_LINE_VOICE_DUMP
    )
    block = pack["workflow_state"]["expense_request"]
    assert block.get("pending_step") == "clarify" or block.get("stage") == "collecting"

    pack2 = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="everything is perfect",
    )
    q = pack2.get("question") or ""
    low = q.lower()
    assert "baki" in low or "missing" in low or "category" in low
    assert "145" in q
    assert "শেষ করতে" in q or "wrap up" in low or "shesh" in low


def test_expense_draft_incomplete_detects_missing_category():
    block: dict = {"items": [{"amount": 145, "category": ""}]}
    assert expense_draft_is_incomplete(block, block["items"])


def test_format_done_incomplete_has_warm_intro():
    from chat.services.expense.clarify import ClarificationIssue

    issues = [
        ClarificationIssue(kind="missing_category", item_index=0, amount=145.0),
    ]
    text = format_done_incomplete_prompt(issues, lang="banglish")
    assert "baki" in text.lower() or "shesh" in text.lower()
    assert "145" in text
