"""Expense location typo suggestions."""

from chat.services.expense_locations import (
    suggest_location_correction,
    strip_location_punctuation,
)
from chat.services.expense_validation import validate_expense_items


def test_strip_location_trailing_period():
    assert strip_location_punctuation("irpur.") == "irpur"


def test_suggest_irpur_to_mirpur():
    assert suggest_location_correction("irpur") == "mirpur"
    assert suggest_location_correction("irpur.", context=frozenset({"uttora"})) == "mirpur"


def test_validate_auto_fixes_location_with_warning():
    items = [
        {
            "category": "Metro Rail",
            "amount": 30,
            "from_location": "uttora",
            "to_location": "irpur.",
        }
    ]
    val = validate_expense_items(items, apply_location_fixes=True)
    assert val.ok is True
    assert items[0]["to_location"] == "mirpur"
    assert any("mirpur" in w.lower() for w in val.warnings)


def test_validate_typo_flags_without_auto_fix():
    items = [
        {
            "category": "Metro Rail",
            "amount": 30,
            "from_location": "uttora",
            "to_location": "irpur",
        }
    ]
    val = validate_expense_items(items, apply_location_fixes=False)
    assert val.ok is True
    assert items[0]["to_location"] == "irpur"
    assert val.line_flags.get(0)
