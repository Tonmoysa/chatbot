"""Phase 2.5: LLM correction parse with rules fallback."""

from unittest.mock import patch

from chat.services.expense.command_executor import apply_message_corrections
from chat.services.expense.command_llm_gate import correction_llm_should_use
from chat.services.expense.command_llm_parser import llm_json_to_correction_plan
from chat.services.expense.command_parser import resolve_correction_plan


def test_correction_llm_gate_blocks_confirm_and_submit():
    assert not correction_llm_should_use("yes", [], review_stage=True)
    assert not correction_llm_should_use("joma daw", [], review_stage=True)
    assert not correction_llm_should_use("bus 50", [], review_stage=False)


def test_llm_json_to_plan_valid():
    plan = llm_json_to_correction_plan(
        {
            "remove_travel_group": False,
            "remove_categories": ["Train"],
            "set_amounts": [{"category": "Bus", "amount": 50}],
            "replacements": [],
            "transfers": [],
            "partial_deducts": [],
            "add_amounts": [],
        }
    )
    assert plan is not None
    assert plan.remove_verb_first == ["Train"]
    assert plan.set_amounts == [("Bus", 50.0)]


def test_llm_json_rejects_oversized_amount():
    plan = llm_json_to_correction_plan(
        {"set_amounts": [{"category": "Bus", "amount": 9_999_999}]}
    )
    assert plan is None


def test_rules_win_without_llm_when_regex_matches():
    items = [
        {"category": "Bus", "amount": 100},
        {"category": "Bike", "amount": 100},
    ]
    msg = "bus 50 taka hobe and bike 150 taka hobe"
    with patch(
        "chat.services.expense.command_llm_parser.parse_correction_plan_llm"
    ) as mock_llm:
        result = apply_message_corrections(
            items,
            msg,
            extract_lines=None,
            use_llm=True,
            review_stage=True,
        )
        mock_llm.assert_not_called()
    assert result.changed
    assert result.parse_source == "rules"
    by_cat = {r["category"]: r["amount"] for r in result.items}
    assert by_cat["Bus"] == 50.0
    assert by_cat["Bike"] == 150.0


def test_llm_gap_fill_when_rules_miss():
    items = [
        {"category": "Lunch", "amount": 100},
        {"category": "Train", "amount": 400},
    ]
    msg = "please drop the train line completely"
    llm_payload = {
        "remove_categories": ["Train"],
        "remove_travel_group": False,
        "replacements": [],
        "transfers": [],
        "partial_deducts": [],
        "set_amounts": [],
        "add_amounts": [],
    }

    with patch(
        "chat.services.expense.command_llm_gate.looks_like_expense_correction",
        return_value=True,
    ), patch(
        "chat.services.expense.command_llm_parser.LLMClient.is_configured",
        return_value=True,
    ), patch(
        "chat.services.expense.command_llm_parser.LLMClient.chat_json",
        return_value=llm_payload,
    ):
        result = apply_message_corrections(
            items,
            msg,
            extract_lines=None,
            trace_id="p25-llm",
            use_llm=True,
            review_stage=True,
        )
    assert result.changed
    assert result.parse_source == "llm"
    assert len(result.items) == 1
    assert result.items[0]["category"] == "Lunch"


def test_resolve_correction_plan_llm_only_when_rules_empty():
    items = [{"category": "Bus", "amount": 100}]
    with patch(
        "chat.services.expense.command_parser.parse_correction_plan"
    ) as mock_rules:
        from chat.services.expense.command_schema import CorrectionCommandPlan

        mock_rules.return_value = CorrectionCommandPlan()
        with patch(
            "chat.services.expense.command_llm_gate.looks_like_expense_correction",
            return_value=True,
        ), patch(
            "chat.services.expense.command_llm_parser.parse_correction_plan_llm"
        ) as mock_llm:
            mock_llm.return_value = CorrectionCommandPlan(
                set_amounts=[("Bus", 70.0)]
            )
            out = resolve_correction_plan(
                "make bus seventy",
                items,
                trace_id="t1",
                use_llm=True,
                review_stage=True,
            )
    assert out.source == "llm"
    assert out.plan.set_amounts == [("Bus", 70.0)]
