"""Expense confirm gate and inline corrections."""

from chat.services.expense.expense_confirm import (
    apply_corrections,
    dedupe_expense_items,
    is_confirmation_no,
    is_confirmation_yes,
    looks_like_expense_correction,
)


def test_confirmation_yes_no():
    assert is_confirmation_yes("yes")
    assert is_confirmation_yes("হ্যাঁ")
    assert is_confirmation_no("no")
    assert is_confirmation_no("না")
    assert not is_confirmation_yes("bus 50")


def test_looks_like_expense_correction_bengali_na():
    assert looks_like_expense_correction("bus 50 না 70 হবে")


def test_dedupe_expense_items():
    items = [
        {"category": "Lunch", "amount": 100},
        {"category": "Lunch", "amount": 100},
        {"category": "Bus", "amount": 50},
    ]
    out = dedupe_expense_items(items)
    assert len(out) == 2


def test_apply_corrections_update_amount():
    items = [{"category": "Bus", "amount": 50, "from_location": "a", "to_location": "b"}]
    out, changed = apply_corrections(items, "bus 70")
    assert changed
    assert out[0]["amount"] == 70.0


def test_apply_corrections_remove_category():
    items = [
        {"category": "Lunch", "amount": 100},
        {"category": "Bus", "amount": 50},
    ]
    out, changed = apply_corrections(items, "bus remove")
    assert changed
    assert len(out) == 1
    assert out[0]["category"] == "Lunch"
