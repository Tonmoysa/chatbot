"""Phase C — expense ack/summary facts envelope + LLM polish."""

from unittest.mock import MagicMock

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM
from chat.services.expense.conversation_manager import ExpenseConversationManager
from chat.services.expense.slots import SLOT_CATEGORY, SLOT_MORE_LINES, SLOT_REVIEW
from chat.services.expense_message_facts import (
    build_ack_envelope,
    build_summary_envelope,
    build_wizard_message_meta,
    envelope_facts_preserved,
)
from chat.services.message_polish import polish_outbound_message
from chat.services.message_polish_llm import (
    polish_expense_facts_message,
    polish_expense_message_with_envelope,
)


def test_build_ack_envelope_lines():
    env = build_ack_envelope(
        [{"category": "Lunch", "amount": 50}],
        incurred_date_iso="2026-06-05",
        lang="en",
        primary_slot=SLOT_MORE_LINES,
    )
    assert env["message_type"] == "expense_ack"
    assert env["facts"]["date"] == "2026-06-05"
    assert env["facts"]["lines"][0]["category"] == "Lunch"
    assert env["facts"]["lines"][0]["amount"] == 50


def test_build_summary_envelope_total():
    items = [
        {"category": "Lunch", "amount": 50},
        {"category": "Bus", "amount": 30, "from_location": "mirpur", "to_location": "gulshan"},
    ]
    env = build_summary_envelope(
        items,
        incurred_date_iso="2026-06-05",
        warnings=["cap warning"],
        lang="bn",
    )
    assert env["message_type"] == "expense_summary"
    assert env["facts"]["total"] == 80
    assert len(env["facts"]["lines"]) == 2


def test_envelope_facts_preserved_rejects_missing_amount():
    env = build_ack_envelope(
        [{"category": "Lunch", "amount": 50}],
        incurred_date_iso="2026-06-05",
        lang="en",
        primary_slot=SLOT_MORE_LINES,
    )
    env["template_fallback"] = "**Lunch** — **50 Tk**"
    assert envelope_facts_preserved(env, "Lunch added for 50 Tk on 2026-06-05.")
    assert not envelope_facts_preserved(env, "Lunch added without amount.")


def test_compose_follow_up_parts_returns_meta():
    mgr = ExpenseConversationManager()
    block = {
        "stage": "collecting",
        "incurred_date_iso": "2026-06-05",
        "reply_language": "en",
    }
    items = [{"category": "Lunch", "amount": 100}]
    ack, ask, meta = mgr.compose_follow_up_parts(
        block,
        items,
        primary_slot=SLOT_MORE_LINES,
        missing=[SLOT_MORE_LINES],
        lang="en",
        incurred_date_iso="2026-06-05",
    )
    assert ack
    assert ask
    assert meta is not None
    assert meta["message_type"] == "expense_ack"
    assert meta["polishable_part"] == ack.strip()
    assert meta["fixed_part"] == ask.strip()
    assert meta["ask_envelope"]["facts"]["prompt_kind"] == "more_lines"


def test_compose_ask_only_category_meta():
    mgr = ExpenseConversationManager()
    block = {
        "stage": "collecting",
        "reply_language": "en",
        "pending_line": {"amount": 50, "category": ""},
    }
    ack, ask, meta = mgr.compose_follow_up_parts(
        block,
        [],
        primary_slot=SLOT_CATEGORY,
        missing=[SLOT_CATEGORY],
        lang="en",
    )
    assert not ack
    assert ask
    assert meta is not None
    assert meta["message_type"] == "expense_wizard_prompt"
    assert meta["ask_envelope"]["facts"]["amount"] == 50
    assert meta["ask_envelope"]["facts"]["prompt_kind"] == "category"


def test_polish_expense_facts_message_fallback_on_bad_output(monkeypatch):
    monkeypatch.setattr(
        "chat.services.message_polish_llm.is_llm_message_polish_enabled",
        lambda: True,
    )
    llm = MagicMock()
    llm.is_configured.return_value = True
    llm.chat_text.return_value = "Looks good."

    env = build_ack_envelope(
        [{"category": "Lunch", "amount": 50}],
        incurred_date_iso="2026-06-05",
        lang="en",
        primary_slot=SLOT_MORE_LINES,
    )
    base = "Noted for **2026-06-05**:\n- **Lunch** — **50 Tk**"
    out = polish_expense_facts_message(
        base,
        envelope=env,
        user_message="lunch 50",
        trace_id="c-fallback",
        llm=llm,
    )
    assert out == base


def test_polish_expense_message_with_envelope_polishes_ask(monkeypatch):
    monkeypatch.setattr(
        "chat.services.message_polish_llm.is_llm_message_polish_enabled",
        lambda: True,
    )
    llm = MagicMock()
    llm.is_configured.return_value = True
    llm.chat_text.side_effect = [
        "Noted for **2026-06-05** — **Lunch** **50 Tk** added so far.",
        "Anything else to add today? Say **done** when ready.",
    ]

    mgr = ExpenseConversationManager()
    block = {"stage": "collecting", "incurred_date_iso": "2026-06-05", "reply_language": "en"}
    items = [{"category": "Lunch", "amount": 50}]
    ack, ask, meta = mgr.compose_follow_up_parts(
        block,
        items,
        primary_slot=SLOT_MORE_LINES,
        missing=[SLOT_MORE_LINES],
        lang="en",
        incurred_date_iso="2026-06-05",
    )
    assert meta is not None
    out = polish_expense_message_with_envelope(
        meta,
        user_message="lunch 50",
        trace_id="d-ok",
        llm=llm,
    )
    assert out is not None
    assert "50" in out
    assert "Lunch" in out
    assert "Anything else" in out
    assert ask.strip() not in out


def test_polish_outbound_uses_expense_message_facts(monkeypatch):
    monkeypatch.setattr(
        "chat.services.message_polish_llm.polish_expense_message_with_envelope",
        lambda envelope, **kwargs: "POLISHED_ACK\n\nAny more lines?",
    )
    base = "Noted for **2026-06-05**:\n- **Lunch** — **50 Tk**\n\nAny more lines?"
    out = polish_outbound_message(
        base,
        intent=INTENT_EXPENSE_CLAIM,
        outcome="NEEDS_CLARIFICATION",
        user_message="lunch 50",
        entities={
            "expense_message_facts": {
                "message_type": "expense_ack",
                "polishable_part": "Noted...",
                "fixed_part": "Any more lines?",
            }
        },
        decision={"rules_applied": ["EXPENSE_WORKFLOW_COLLECTING"]},
        trace_id="c-outbound",
    )
    assert out.startswith("POLISHED_ACK")


def test_review_meta_skips_footer_prompt_polish():
    mgr = ExpenseConversationManager()
    items = [
        {"category": "Lunch", "amount": 50},
        {"category": "Bus", "amount": 30},
    ]
    ack, ask, meta = mgr.compose_follow_up_parts(
        {"stage": "review", "reply_language": "en"},
        items,
        primary_slot=SLOT_REVIEW,
        missing=[],
        lang="en",
        incurred_date_iso="2026-06-05",
    )
    assert meta is not None
    assert meta["message_type"] == "expense_summary"
    assert meta["facts"]["total"] == 80
    assert ask
    assert "ask_envelope" not in meta
