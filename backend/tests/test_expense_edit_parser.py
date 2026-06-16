"""Expense line-edit parser — numeric ordinals and auto-apply."""

import pytest

from chat.services.expense.command_parser import (
    parse_correction_plan,
    parse_ordinal_amount_correction,
)
from chat.services.expense.command_executor import execute_correction_plan
from chat.services.expense.expense_confirm import looks_like_expense_correction
from chat.services.expense.expense_edit_parser import (
    is_explicit_line_amount_update,
    parse_line_amount_update,
    parse_line_index_from_message,
    should_auto_apply_line_amount_update,
)


@pytest.mark.parametrize(
    "message,expected_index",
    [
        ("1 no expense 120 na, 140 taka hobe", 0),
        ("2 number expense 65 na 80", 1),
        ("line 1 120 na 140", 0),
        ("#2 expense 65 na 80 hobe", 1),
        ("prothom expense 120 na, 140 taka hobe", 0),
    ],
)
def test_numeric_and_word_ordinals_resolve_line_index(message, expected_index):
    assert parse_line_index_from_message(message, item_count=4) == expected_index


@pytest.mark.parametrize(
    "message,expected",
    [
        ("1 no expense 120 na, 140 taka hobe", (0, 140.0)),
        ("prothom expense 120 na, 140 taka hobe", (0, 140.0)),
        ("2 no expense 65 na, 80 taka hobe", (1, 80.0)),
    ],
)
def test_ordinal_amount_correction_parses(message, expected):
    assert parse_ordinal_amount_correction(message, item_count=4) == expected


def test_one_no_expense_correction_plan():
    plan = parse_correction_plan(
        "1 no expense 120 na, 140 taka hobe",
        item_count=3,
    )
    assert plan.update_amount_by_index == (0, 140.0)
    assert not plan.amount_replacements


def test_explicit_update_auto_apply_when_old_matches():
    items = [
        {"category": "Bus", "amount": 120.0, "from_location": "mirpur", "to_location": "motijheel"},
        {"category": "Snack", "amount": 65.0},
        {"category": "Lunch", "amount": 180.0},
    ]
    msg = "1 no expense 120 na, 140 taka hobe"
    edit = parse_line_amount_update(msg, item_count=len(items))
    assert edit is not None
    assert edit.line_index == 0
    assert edit.new_amount == 140.0
    assert edit.old_amount == 120.0
    assert is_explicit_line_amount_update(msg, edit, items)
    assert should_auto_apply_line_amount_update(
        msg,
        items,
        line_index=0,
        new_amount=140.0,
        old_amount=120.0,
    )


def test_ambiguous_amount_only_does_not_auto_apply():
    items = [{"category": "Bus", "amount": 120.0}]
    msg = "140 kore dao"
    edit = parse_line_amount_update(msg, item_count=1)
    assert edit is None
    assert not should_auto_apply_line_amount_update(
        msg, items, line_index=0, new_amount=140.0, old_amount=120.0
    )


def test_wrong_old_amount_blocks_auto_apply():
    items = [{"category": "Bus", "amount": 100.0}]
    msg = "1 no expense 120 na, 140 taka hobe"
    edit = parse_line_amount_update(msg, item_count=1)
    assert edit is not None
    assert not is_explicit_line_amount_update(msg, edit, items)


def test_looks_like_correction_for_one_no_pattern():
    assert looks_like_expense_correction("1 no expense 120 na, 140 taka hobe")


def test_execute_one_no_correction_updates_single_line():
    items = [
        {"category": "Bus", "amount": 120.0},
        {"category": "Snack", "amount": 65.0},
    ]
    plan = parse_correction_plan("1 no expense 120 na, 140", item_count=2)
    result = execute_correction_plan(items, plan)
    assert result.changed
    assert result.items[0]["amount"] == 140.0
    assert result.items[1]["amount"] == 65.0


def test_process_expense_turn_auto_applies_one_no_explicit_update():
    from chat.services.expense_workflow import process_expense_turn

    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "incurred_date_iso": "2026-06-16",
            "items": [
                {
                    "category": "Bus",
                    "amount": 120.0,
                    "from_location": "mirpur",
                    "to_location": "motijheel",
                },
                {"category": "Snack", "amount": 65.0},
                {"category": "Lunch", "amount": 180.0},
            ],
        }
    }
    pack = process_expense_turn(
        workflow_state=wf,
        message="1 no expense 120 na, 140 taka hobe",
    )
    assert pack["items"][0]["amount"] == 140.0
    block = pack["workflow_state"]["expense_request"]
    assert not block.get("ordinal_amount_confirm_pending")
