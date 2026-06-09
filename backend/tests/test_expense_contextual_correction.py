"""Banglish/Bangla contextual review corrections."""

from chat.services.expense.command_parser import parse_correction_plan
from chat.services.expense.command_executor import apply_message_corrections


def test_contextual_lunch_amount_hobe():
    plan = parse_correction_plan("ekhon je lunch ta ache eta 155 hobe")
    assert ("Lunch", 155.0) in plan.set_amounts


def test_contextual_lunch_amount_applies_to_draft():
    items = [
        {"category": "Lunch", "amount": 150.0},
        {"category": "Bus", "amount": 50.0, "from_location": "a", "to_location": "b"},
    ]
    result = apply_message_corrections(
        items,
        "ekhon je lunch ta ache eta 155 hobe",
        extract_lines=None,
        use_llm=False,
        review_stage=True,
    )
    assert result.changed
    lunch = next(r for r in result.items if r.get("category") == "Lunch")
    assert lunch["amount"] == 155.0
