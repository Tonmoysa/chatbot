"""Expense hybrid entity pipeline — parser + LLM merge."""

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM
from chat.services.expense.entity_merge import (
    PARSER_PRIORITY_FIELDS,
    fill_parser_gaps_with_llm,
    merge_parser_and_llm,
    overlay_llm_expense_lines,
    parser_needs_llm_gap_fill,
)
from chat.services.expense.entity_pipeline import ExpenseEntityPipeline
from chat.services.expense.llm_gate import (
    expense_extraction_should_use_llm,
    expense_wizard_should_use_llm,
)
from chat.services.expense_extraction import (
    ExpenseLineItem,
    ExtractionResult,
    extract_expense_items,
)
from chat.services.expense_workflow import process_expense_turn
from chat.services.turn_classifier import TURN_CONFIRM, TURN_SLOT_ANSWER


def test_parser_priority_fields_documented():
    assert "category" in PARSER_PRIORITY_FIELDS
    assert "amount" in PARSER_PRIORITY_FIELDS
    assert "description" not in PARSER_PRIORITY_FIELDS


def test_merge_parser_wins_when_lines_present():
    parser = extract_expense_items("lunch 100, bus 50")
    merged, sources = merge_parser_and_llm(
        parser,
        {
            "expense_lines": [
                {"category": "Snack", "amount": 999},
            ]
        },
    )
    assert len(merged.items) == 2
    assert merged.items[0].category == "Lunch"
    assert sources.get("items") == "parser"


def test_overlay_llm_lines_when_parser_empty():
    parser = extract_expense_items("ghore baper jonno 150 taka kharcha hoyeche")
    overlay, sources = overlay_llm_expense_lines(
        parser,
        {
            "expense_lines": [
                {
                    "category": "Lunch",
                    "amount": 150,
                    "notes": "ghore baper jonno",
                }
            ]
        },
        "ghore baper jonno 150 taka kharcha hoyeche",
        llm_used=True,
    )
    assert len(overlay.items) == 1
    assert overlay.items[0].amount == 150.0
    assert sources.get("items") == "llm_primary"


def test_llm_gate_confirm_off_slot_on():
    assert expense_wizard_should_use_llm("yes", workflow_turn=TURN_CONFIRM) is False
    assert expense_wizard_should_use_llm("bus 50", workflow_turn=TURN_SLOT_ANSWER) is True


def test_extraction_gate_forces_llm_on_long_compound():
    msg = "ajke bus 120, lunch 180, nasta 40 taka"
    assert expense_extraction_should_use_llm(msg, workflow_turn=TURN_CONFIRM)


def test_pipeline_regex_path_without_llm():
    pipe = ExpenseEntityPipeline()
    result = pipe.extract(
        "lunch 100, rickshaw 20",
        intent=INTENT_EXPENSE_CLAIM,
        context_lines=[],
        trace_id="expense-pipe-rules",
        use_llm=False,
    )
    assert result.extraction is not None
    assert len(result.extraction.items) >= 1
    assert result.extraction.items[0].category == "Lunch"


def test_pipeline_llm_freeform_lines(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: True,
    )

    class FakeLLM:
        def is_configured(self):
            return True

        def chat_json(self, *, system_prompt, user_prompt, trace_id):
            return {
                "expense_lines": [
                    {
                        "category": "Rickshaw",
                        "amount": 80,
                        "from_location": "office",
                        "to_location": "basha",
                    }
                ],
                "expense_incurred_date": "2026-06-05",
            }

    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient",
        FakeLLM,
    )

    pipe = ExpenseEntityPipeline()
    result = pipe.extract(
        "office theke bashay gelam, 80 taka",
        intent=INTENT_EXPENSE_CLAIM,
        context_lines=[],
        trace_id="expense-pipe-llm",
        use_llm=True,
    )
    assert result.extraction is not None
    assert len(result.extraction.items) == 1
    assert result.extraction.items[0].category == "Rickshaw"
    assert result.extraction.items[0].amount == 80.0


def test_process_expense_turn_reuses_pipeline_result(monkeypatch):
    from chat.services.expense.entity_pipeline import ExpenseExtractionResult
    from chat.services.expense_extraction import ExpenseLineItem, ExtractionResult

    preloaded = ExpenseExtractionResult(
        extraction=ExtractionResult(
            items=[ExpenseLineItem(category="Lunch", amount=120.0)]
        ),
        entities={"expense_incurred_date": "2026-06-05"},
    )

    def _fail_extract(self, *args, **kwargs):
        raise AssertionError("pipeline_result should skip second extract()")

    monkeypatch.setattr(ExpenseEntityPipeline, "extract", _fail_extract)

    r = process_expense_turn(
        workflow_state={},
        message="lunch 120",
        pipeline_result=preloaded,
    )
    assert any(row.get("category") == "Lunch" for row in r.get("items") or [])


def test_parser_needs_llm_gap_fill_for_missing_route():
    parser = ExtractionResult(
        items=[ExpenseLineItem(category="Bus", amount=100.0)],
    )
    assert parser_needs_llm_gap_fill(parser) is True


def test_parser_needs_llm_gap_fill_false_when_complete():
    parser = extract_expense_items("lunch 100, bus 50 mirpur to badda")
    assert parser_needs_llm_gap_fill(parser) is False


def test_fill_parser_gaps_route_from_llm():
    parser = ExtractionResult(
        items=[ExpenseLineItem(category="Bus", amount=100.0)],
    )
    filled, sources = fill_parser_gaps_with_llm(
        parser,
        {
            "expense_lines": [
                {
                    "category": "Bus",
                    "amount": 100,
                    "from_location": "mirpur",
                    "to_location": "badda",
                }
            ]
        },
        "ajke 100 taka bus mirpur theke badda",
        llm_used=True,
    )
    assert filled.items[0].from_location == "mirpur"
    assert filled.items[0].to_location == "badda"
    assert sources.get("line_0_route") == "llm_gap_fill"


def test_fill_parser_gaps_appends_missed_llm_line():
    parser = ExtractionResult(
        items=[ExpenseLineItem(category="Lunch", amount=100.0)],
    )
    llm_payload = {
        "expense_lines": [
            {"category": "Lunch", "amount": 100},
            {
                "category": "Metro Rail",
                "amount": 30,
                "from_location": "uttora",
                "to_location": "motijheel",
            },
        ]
    }
    assert parser_needs_llm_gap_fill(parser, llm_payload) is True
    filled, sources = fill_parser_gaps_with_llm(
        parser,
        llm_payload,
        "lunch 100, metro 30 uttora to motijheel",
        llm_used=True,
    )
    cats = {item.category: item for item in filled.items}
    assert cats["Lunch"].amount == 100.0
    assert cats["Metro Rail"].amount == 30.0
    assert cats["Metro Rail"].from_location == "uttora"
    assert sources.get("items") == "parser+llm_gap_fill"


def test_fill_parser_gaps_malformed_uses_llm_primary():
    parser = ExtractionResult(malformed=["then 20 taka"])
    filled, sources = fill_parser_gaps_with_llm(
        parser,
        {
            "expense_lines": [
                {"category": "Other", "amount": 20, "notes": "unknown"},
            ]
        },
        "then 20 taka",
        llm_used=True,
    )
    assert filled.items == []
    assert filled.malformed == ["then 20 taka"]
    assert sources == {}


def test_fill_parser_gaps_malformed_with_real_llm_category():
    parser = ExtractionResult(malformed=["then 20 taka"])
    filled, sources = fill_parser_gaps_with_llm(
        parser,
        {
            "expense_lines": [
                {"category": "Snack", "amount": 20, "notes": "snacks"},
            ]
        },
        "then 20 taka",
        llm_used=True,
    )
    assert len(filled.items) == 1
    assert filled.items[0].category == "Snack"
    assert filled.items[0].amount == 20.0
    assert sources.get("items") == "llm_primary"


def test_pipeline_fills_missing_route_from_llm(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: True,
    )

    class FakeLLM:
        def is_configured(self):
            return True

        def chat_json(self, *, system_prompt, user_prompt, trace_id):
            assert "expense_lines" in system_prompt
            return {
                "expense_lines": [
                    {
                        "category": "Bus",
                        "amount": 100,
                        "from_location": "mirpur",
                        "to_location": "badda",
                    },
                    {"category": "Lunch", "amount": 100},
                ]
            }

    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient",
        FakeLLM,
    )
    monkeypatch.setattr(
        "chat.services.expense.entity_pipeline.extract_expense_items",
        lambda message: ExtractionResult(
            items=[
                ExpenseLineItem(category="Bus", amount=100.0),
                ExpenseLineItem(category="Lunch", amount=100.0),
            ]
        ),
    )

    pipe = ExpenseEntityPipeline()
    result = pipe.extract(
        "ajke amar expense hoyeche 100 taka bus mirpur to badda then lunch 100 taka",
        intent=INTENT_EXPENSE_CLAIM,
        context_lines=[],
        trace_id="expense-pipe-gap",
        use_llm=True,
    )
    assert result.extraction is not None
    bus = next(i for i in result.extraction.items if i.category == "Bus")
    assert bus.from_location == "mirpur"
    assert bus.to_location == "badda"
    assert result.field_sources.get("line_0_route") == "llm_gap_fill"


def test_workflow_compound_message_asks_category_for_loose_amount():
    msg = (
        "ajke amar expense hoyeche 100 taka bus mirpur to badda "
        "then lunch 100 taka then metro rail 30 taka uttora to irpur.."
        "then 20 taka"
    )
    r = process_expense_turn(workflow_state={}, message=msg)
    er = r["workflow_state"].get("expense_request") or {}
    assert er.get("pending_step") == "clarify"
    assert "Other" not in {row.get("category") for row in r.get("items") or []}


def test_workflow_clarify_reply_then_review():
    msg = (
        "ajke amar expense hoyeche 100 taka bus mirpur to badda "
        "then lunch 100 taka then metro rail 30 taka uttora to irpur.."
        "then 20 taka"
    )
    r1 = process_expense_turn(workflow_state={}, message=msg)
    r2 = process_expense_turn(
        workflow_state=r1["workflow_state"],
        message="mirpur, snack",
    )
    er2 = r2["workflow_state"].get("expense_request") or {}
    cats = {row["category"]: row for row in r2.get("items") or []}
    assert cats["Metro Rail"]["to_location"] == "mirpur"
    assert cats["Snack"]["amount"] == 20.0
    assert er2.get("stage") == "review"
    assert "⚠️" not in (r2.get("question") or "") or "mirpur" in (r2.get("question") or "")
