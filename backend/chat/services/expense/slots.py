"""
Expense wizard slot identifiers and stage constants.

Canonical source — workflow_schema and expense_workflow import from here.
"""

from __future__ import annotations

# Wizard slots (collecting phase)
SLOT_INCURRED_DATE = "incurred_date"
SLOT_AMOUNT = "amount"
SLOT_CATEGORY = "category"
SLOT_FROM_TO = "from_to"
SLOT_CLARIFY = "clarify"
SLOT_MORE_LINES = "more_lines"
SLOT_ITEMS = "items"

# Post-collection gates
SLOT_REVIEW = "review"
SLOT_SUBMIT_CONFIRM = "submit_confirm"

# FSM stages stored on workflow_state["expense_request"]["stage"]
STAGE_COLLECTING = "collecting"
STAGE_REVIEW = "review"
STAGE_SUBMIT_CONFIRM = "submit_confirm"

# Priority order for asking (only missing slots are prompted)
SLOT_ASK_ORDER: tuple[str, ...] = (
    SLOT_INCURRED_DATE,
    SLOT_AMOUNT,
    SLOT_CATEGORY,
    SLOT_FROM_TO,
    SLOT_ITEMS,
    SLOT_MORE_LINES,
    SLOT_REVIEW,
    SLOT_SUBMIT_CONFIRM,
)
