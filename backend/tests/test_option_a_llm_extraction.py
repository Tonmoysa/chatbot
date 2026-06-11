"""Option A — long-message LLM extraction triggers + polish envelopes (TURN_ROUTER_SPEC)."""

import pytest

from chat.services.expense.entity_merge import parser_needs_llm_gap_fill
from chat.services.expense_extraction import ExpenseLineItem, ExtractionResult
from chat.services.expense.llm_extraction_trigger import (
    count_distinct_amount_mentions,
    looks_like_long_compound_expense_message,
    should_force_expense_llm_extraction,
)
from chat.services.expense.llm_gate import (
    expense_extraction_should_use_llm,
    expense_wizard_should_use_llm,
)
from chat.services.expense_message_facts import (
    build_confirm_prompt_envelope,
    build_disambiguation_envelope,
    confirm_prompt_facts_preserved,
    disambiguation_facts_preserved,
)
from chat.services.leave.llm_extraction_trigger import (
    looks_like_long_compound_leave_message,
    should_force_leave_llm_extraction,
)
from chat.services.leave.llm_gate import leave_extraction_should_use_llm
from chat.services.turn_classifier import TURN_CONFIRM


LONG_EXPENSE = (
    "ajke office theke basha bus e 120 taka, lunch 180 taka, "
    "nasta 40 taka, rickshaw 30"
)


def test_long_compound_expense_message_detected():
    assert looks_like_long_compound_expense_message(LONG_EXPENSE)
    assert count_distinct_amount_mentions(LONG_EXPENSE) >= 3
    assert should_force_expense_llm_extraction(LONG_EXPENSE)


def test_ha_not_forced_llm_extraction():
    assert not should_force_expense_llm_extraction("ha")
    assert not expense_extraction_should_use_llm("ha", workflow_turn=TURN_CONFIRM)


def test_force_llm_overrides_confirm_gate_for_long_message():
    assert expense_extraction_should_use_llm(
        LONG_EXPENSE, workflow_turn=TURN_CONFIRM
    )


def test_wizard_gate_unchanged_for_short_confirm():
    assert expense_wizard_should_use_llm("yes", workflow_turn=TURN_CONFIRM) is False
    assert expense_wizard_should_use_llm("bus 50", workflow_turn=None) is True


def test_parser_needs_gap_when_fewer_lines_than_amounts():
    parser = ExtractionResult(
        items=[ExpenseLineItem(category="Lunch", amount=180.0)],
    )
    assert parser_needs_llm_gap_fill(parser, message=LONG_EXPENSE) is True


def test_parser_needs_gap_partial_travel_route():
    parser = ExtractionResult(
        items=[
            ExpenseLineItem(
                category="Bus",
                amount=120.0,
                from_location="office",
                to_location="",
            )
        ],
    )
    assert parser_needs_llm_gap_fill(parser) is True


def test_long_compound_leave_message():
    msg = "agami 15 august sick leave, full day, paid, family program"
    assert looks_like_long_compound_leave_message(msg)
    assert should_force_leave_llm_extraction(msg)


def test_leave_confirm_not_forced():
    assert not should_force_leave_llm_extraction("ha")
    assert leave_extraction_should_use_llm("ha", workflow_turn=TURN_CONFIRM) is False


def test_disambiguation_envelope_facts_guard():
    items = [{"category": "Lunch", "amount": 150.0}, {"category": "Bus", "amount": 100.0}]
    env = build_disambiguation_envelope(
        "konta expense update korbo? **Lunch** 150, **Bus** 100",
        items=items,
        lang="banglish",
        prompt_kind="amount_correction",
    )
    ok = "**Lunch** 150, **Bus** 100 — konta?"
    bad = "**Lunch** 200 only"
    assert disambiguation_facts_preserved(env, ok)
    assert not disambiguation_facts_preserved(env, bad)


def test_confirm_prompt_envelope_facts_guard():
    items = [{"category": "Bus", "amount": 100.0, "from_location": "office", "to_location": "basha"}]
    env = build_confirm_prompt_envelope(
        "Line 1 **200 Tk** korbo?",
        items=items,
        lang="banglish",
        prompt_kind="ordinal_amount",
        target_index=0,
        target_amount=200.0,
    )
    assert confirm_prompt_facts_preserved(env, "Line 1 **200 Tk** korbo? **Yes** / **no**")
    assert not confirm_prompt_facts_preserved(env, "Line 2 **300 Tk** korbo?")
