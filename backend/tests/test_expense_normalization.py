"""Expense normalization — category synonyms and line cleanup."""

import pytest

from chat.services.expense.normalization import (
    normalize_category_label,
    normalize_expense_line,
    normalize_pending_line,
    pending_line_ready,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("খাবার", "Lunch"),
        ("রিক্সা", "Rickshaw"),
        ("সিএনজি", "CNG"),
        ("মেট্রো রেল", "Metro Rail"),
        ("lunch", "Lunch"),
        ("bus", "Bus"),
        ("unknown thing", "Other"),
    ],
)
def test_normalize_category_label(raw, expected):
    assert normalize_category_label(raw) == expected


def test_normalize_expense_line_trims_locations():
    row = normalize_expense_line(
        {
            "category": "rickshaw",
            "amount": "50",
            "from_location": "  office ",
            "to_location": " home ",
        }
    )
    assert row["category"] == "Rickshaw"
    assert row["amount"] == 50.0
    assert row["from_location"] == "office"
    assert row["to_location"] == "home"


def test_normalize_pending_line():
    pending = normalize_pending_line(
        {"amount": "100", "category": "খাবার", "from_location": "", "to_location": ""}
    )
    assert pending["category"] == "Lunch"
    assert pending["amount"] == 100.0


def test_pending_line_ready_travel_requires_route():
    assert not pending_line_ready(
        {"category": "Bus", "amount": 50, "from_location": "", "to_location": ""}
    )
    assert pending_line_ready(
        {
            "category": "Bus",
            "amount": 50,
            "from_location": "mirpur",
            "to_location": "gulshan",
        }
    )


def test_pending_line_ready_non_travel():
    assert pending_line_ready({"category": "Lunch", "amount": 80})
