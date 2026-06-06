"""Expense workflow schema — missing field detection."""

import pytest

from chat.services.expense.slots import (
    SLOT_CATEGORY,
    SLOT_FROM_TO,
    SLOT_ITEMS,
    SLOT_MORE_LINES,
    SLOT_REVIEW,
    SLOT_SUBMIT_CONFIRM,
)
from chat.services.expense.workflow_schema import (
    ExpenseWorkflowSchema,
    get_expense_workflow_schema,
)
from chat.services.expense_workflow import expense_pending_prompt


def test_schema_singleton():
    assert get_expense_workflow_schema() is get_expense_workflow_schema()


def test_schema_pending_category():
    schema = ExpenseWorkflowSchema()
    block = {
        "stage": "collecting",
        "incurred_date_iso": "2026-06-05",
        "pending_line": {"amount": 100},
        "pending_step": "category",
    }
    missing = schema.missing_fields(block, [])
    assert missing == [SLOT_CATEGORY]
    assert schema.primary_slot(block, []) == SLOT_CATEGORY


def test_schema_pending_from_to():
    schema = ExpenseWorkflowSchema()
    block = {
        "stage": "collecting",
        "incurred_date_iso": "2026-06-05",
        "pending_line": {
            "amount": 50,
            "category": "Bus",
            "from_location": "",
            "to_location": "",
        },
        "pending_step": "from_to",
    }
    missing = schema.missing_fields(block, [])
    assert missing == [SLOT_FROM_TO]
    assert schema.primary_slot(block, []) == SLOT_FROM_TO


def test_schema_more_lines_when_items_exist():
    schema = ExpenseWorkflowSchema()
    block = {
        "stage": "collecting",
        "incurred_date_iso": "2026-06-05",
        "items": [{"category": "Lunch", "amount": 100}],
    }
    missing = schema.missing_fields(block, block["items"])
    assert missing == [SLOT_MORE_LINES]


def test_schema_empty_collecting_needs_items():
    schema = ExpenseWorkflowSchema()
    block = {"stage": "collecting", "incurred_date_iso": "2026-06-05"}
    assert schema.missing_fields(block, []) == [SLOT_ITEMS]


def test_schema_review_and_submit_confirm():
    schema = ExpenseWorkflowSchema()
    review_block = {
        "stage": "review",
        "incurred_date_iso": "2026-06-05",
        "items": [{"category": "Lunch", "amount": 100}],
    }
    assert schema.missing_fields(review_block, review_block["items"]) == [SLOT_REVIEW]

    submit_block = dict(review_block)
    submit_block["stage"] = "submit_confirm"
    assert schema.missing_fields(submit_block, submit_block["items"]) == [
        SLOT_SUBMIT_CONFIRM
    ]


def test_schema_legacy_confirming_alias():
    schema = ExpenseWorkflowSchema()
    assert schema.normalize_stage("confirming") == "review"


def test_expense_pending_prompt_uses_schema():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "incurred_date_iso": "2026-06-05",
            "reply_language": "en",
            "pending_line": {"amount": 80},
            "pending_step": "category",
        }
    }
    prompt = expense_pending_prompt(wf)
    assert prompt is not None
    assert "80" in prompt
    assert "category" in prompt.lower()


def test_schema_is_collecting_complete():
    schema = ExpenseWorkflowSchema()
    block = {
        "stage": "collecting",
        "incurred_date_iso": "2026-06-05",
        "items": [{"category": "Snack", "amount": 20}],
    }
    assert schema.is_collecting_complete(block, block["items"])

    pending_block = {
        **block,
        "pending_line": {"amount": 30},
        "pending_step": "category",
    }
    assert not schema.is_collecting_complete(pending_block, block["items"])
