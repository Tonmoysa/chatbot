"""Expense conversation manager — acknowledgment + contextual prompts."""

import pytest

from chat.services.expense.conversation_manager import ExpenseConversationManager
from chat.services.expense.slots import (
    SLOT_CATEGORY,
    SLOT_FROM_TO,
    SLOT_MORE_LINES,
)
from chat.services.expense_workflow import (
    _build_wizard_question,
    expense_pending_prompt,
    process_expense_turn,
)


def test_acknowledges_collected_lines_before_more_lines():
    mgr = ExpenseConversationManager()
    block = {
        "stage": "collecting",
        "incurred_date_iso": "2026-06-05",
        "reply_language": "en",
    }
    items = [
        {"category": "Lunch", "amount": 100},
        {"category": "Bus", "amount": 50, "from_location": "mirpur", "to_location": "gulshan"},
    ]
    q = mgr.build_follow_up(
        block,
        items,
        primary_slot=SLOT_MORE_LINES,
        missing=[SLOT_MORE_LINES],
        lang="en",
        incurred_date_iso="2026-06-05",
    )
    assert "2026-06-05" in q
    assert "Lunch" in q
    assert "100" in q
    assert "mirpur" in q
    assert "gulshan" in q
    assert q.lower().count("noted") <= 1
    assert "Any more" in q or "Anything else" in q or "More expenses" in q


def test_from_to_acks_category_without_duplicate_lead():
    mgr = ExpenseConversationManager()
    block = {
        "stage": "collecting",
        "incurred_date_iso": "2026-06-05",
        "reply_language": "en",
        "pending_line": {
            "amount": 50,
            "category": "Bus",
            "from_location": "",
            "to_location": "",
        },
        "pending_step": "from_to",
    }
    items = [{"category": "Lunch", "amount": 100}]
    q = mgr.build_follow_up(
        block,
        items,
        primary_slot=SLOT_FROM_TO,
        missing=[SLOT_FROM_TO],
        lang="en",
        pending_line=block["pending_line"],
        incurred_date_iso="2026-06-05",
    )
    assert q.count("**Bus**") == 1
    assert "From" in q and "To" in q
    assert "Lunch" in q


def test_build_wizard_question_delegates_to_manager():
    block = {
        "stage": "collecting",
        "incurred_date_iso": "2026-06-10",
        "reply_language": "bn",
        "items": [{"category": "Snack", "amount": 30}],
    }
    q = _build_wizard_question(
        block,
        block["items"],
        primary_slot=SLOT_MORE_LINES,
        lang="bn",
    )[0]
    assert "Snack" in q
    assert "৩০" in q or "30" in q
    assert "আর কিছু" in q or "আর কোনো" in q or "শেষ" in q


def test_expense_pending_prompt_uses_conversation_manager():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "incurred_date_iso": "2026-06-05",
            "reply_language": "en",
            "items": [{"category": "Lunch", "amount": 80}],
        }
    }
    prompt = expense_pending_prompt(wf)
    assert prompt is not None
    assert "Lunch" in prompt
    assert "80" in prompt
    assert "Any more" in prompt or "Anything else" in prompt or "More expenses" in prompt


def test_grouped_ack_no_per_line_noted():
    mgr = ExpenseConversationManager()
    block = {
        "stage": "collecting",
        "incurred_date_iso": "2026-06-05",
        "reply_language": "banglish",
    }
    items = [
        {"category": "Lunch", "amount": 50},
        {"category": "Lunch", "amount": 50},
        {"category": "Metro Rail", "amount": 100, "from_location": "uttora", "to_location": "motijheel"},
    ]
    q = mgr.build_follow_up(
        block,
        items,
        primary_slot=SLOT_MORE_LINES,
        missing=[SLOT_MORE_LINES],
        lang="banglish",
        incurred_date_iso="2026-06-05",
    )
    assert q.lower().count("note kora hoyeche") == 0
    assert "50" in q
    assert "Metro Rail" in q or "metro" in q.lower()


@pytest.mark.django_db
def test_wizard_after_lunch_bus_asks_more_with_ack():
    wf: dict = {}
    r1 = process_expense_turn(
        workflow_state=wf,
        message="lunch 100, bus 50",
    )
    block = r1["workflow_state"].get("expense_request") or {}
    assert block.get("pending_line") or r1.get("items")

    wf2 = r1["workflow_state"]
    r2 = process_expense_turn(
        workflow_state=wf2,
        message="mirpur to gulshan",
    )
    r3 = process_expense_turn(
        workflow_state=r2["workflow_state"],
        message="done",
    )
    assert r3.get("complete") or r3.get("question")
    q3 = r3.get("question") or ""
    if q3:
        if "Lunch" in q3:
            assert "100" in q3
        assert (
            "review" in q3.lower()
            or "মোট" in q3
            or "Total" in q3
            or "পর্যালোচনা" in q3
        )
