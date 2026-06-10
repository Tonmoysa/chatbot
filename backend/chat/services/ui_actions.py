"""Contextual UI action chips derived from workflow_state (Phase 3)."""

from __future__ import annotations

from typing import Any

from chat.services.expense.expense_fsm import (
    is_expense_in_progress,
    is_expense_review,
    is_expense_submit_confirm,
    read_expense_block,
)
from chat.services.leave_confirm import SLOT_EDIT_MENU
from chat.services.leave_fsm import is_awaiting_leave_confirmation, read_leave_state
from chat.services.leave_slots import SLOT_HALF_PERIOD, SLOT_LEAVE_TYPE, SLOT_SCOPE
from chat.services.leave_workflow import is_leave_in_progress, pending_step

MAX_ACTIONS = 12


def _chip(
    action_id: str,
    *,
    label: str,
    label_bn: str,
    message: str,
    kind: str = "secondary",
) -> dict[str, str]:
    return {
        "id": action_id,
        "label": label,
        "label_bn": label_bn,
        "message": message,
        "kind": kind,
    }


def _expense_actions(workflow_state: dict[str, Any]) -> list[dict[str, str]]:
    block = read_expense_block(workflow_state)
    if not block.get("active"):
        return []

    items = list(block.get("items") or [])
    actions: list[dict[str, str]] = []

    if is_expense_review(block):
        actions.extend(
            [
                _chip("expense_review_yes", label="Yes", label_bn="হ্যাঁ", message="yes", kind="primary"),
                _chip("expense_review_no", label="Edit", label_bn="ঠিক করুন", message="no", kind="secondary"),
            ]
        )
        for idx, row in enumerate(items):
            cat = str(row.get("category") or f"line {idx + 1}").strip()
            slug = cat.lower().replace(" ", "_")
            actions.append(
                _chip(
                    f"expense_remove_{idx}",
                    label=f"Remove {cat}",
                    label_bn=f"{cat} বাদ",
                    message=f"remove {cat.lower()}",
                    kind="danger",
                )
            )
        actions.append(
            _chip("expense_cancel", label="Cancel", label_bn="বাতিল", message="cancel expense")
        )
        return actions[:MAX_ACTIONS]

    if is_expense_submit_confirm(block):
        return [
            _chip(
                "expense_submit_yes",
                label="Submit",
                label_bn="জমা দিন",
                message="yes",
                kind="primary",
            ),
            _chip(
                "expense_submit_no",
                label="Go back",
                label_bn="ফিরে যান",
                message="no",
                kind="secondary",
            ),
        ]

    if block.get("paused"):
        return []

    if items:
        actions.extend(
            [
                _chip("expense_done", label="Done", label_bn="শেষ", message="done", kind="primary"),
                _chip(
                    "expense_submit",
                    label="Submit",
                    label_bn="জমা দিন",
                    message="joma daw",
                    kind="secondary",
                ),
            ]
        )
    actions.append(
        _chip("expense_cancel", label="Cancel", label_bn="বাতিল", message="cancel expense")
    )
    return actions[:MAX_ACTIONS]


def _leave_actions(workflow_state: dict[str, Any]) -> list[dict[str, str]]:
    if not is_leave_in_progress(workflow_state):
        return []

    if is_awaiting_leave_confirmation(workflow_state):
        return [
            _chip("leave_confirm_yes", label="Submit", label_bn="জমা দিন", message="yes", kind="primary"),
            _chip("leave_confirm_edit", label="Edit", label_bn="বদলান", message="edit", kind="secondary"),
            _chip("leave_confirm_cancel", label="Cancel", label_bn="বাতিল", message="cancel", kind="secondary"),
        ]

    step = (pending_step(workflow_state) or "").strip()
    if step in (SLOT_LEAVE_TYPE, "leave_type"):
        return [
            _chip("leave_sick", label="Sick leave", label_bn="Sick leave", message="sick leave", kind="primary"),
            _chip("leave_annual", label="Annual leave", label_bn="Annual leave", message="annual leave", kind="secondary"),
            _chip("leave_unpaid", label="Without pay", label_bn="Without pay", message="leave without pay", kind="secondary"),
        ]
    if step == SLOT_SCOPE:
        return [
            _chip("leave_full", label="Full day", label_bn="পুরো দিন", message="full day", kind="primary"),
            _chip("leave_half", label="Half day", label_bn="হাফ দিন", message="half day", kind="secondary"),
        ]
    if step == SLOT_HALF_PERIOD:
        return [
            _chip("leave_first_half", label="First half", label_bn="প্রথম অর্ধ", message="first half", kind="primary"),
            _chip("leave_second_half", label="Second half", label_bn="দ্বিতীয় অর্ধ", message="second half", kind="secondary"),
        ]
    if step == SLOT_EDIT_MENU:
        return [
            _chip("leave_edit_type", label="Leave type", label_bn="ছুটির ধরন", message="type"),
            _chip("leave_edit_scope", label="Full/Half", label_bn="পুরো/হাফ", message="scope"),
            _chip("leave_edit_date", label="Date", label_bn="তারিখ", message="date"),
            _chip("leave_edit_reason", label="Reason", label_bn="কারণ", message="reason"),
            _chip("leave_edit_back", label="Back", label_bn="ফিরে যান", message="back"),
        ]

    st = read_leave_state(workflow_state)
    if st.get("review_pending"):
        return [
            _chip("leave_confirm_yes", label="Submit", label_bn="জমা দিন", message="yes", kind="primary"),
            _chip("leave_confirm_edit", label="Edit", label_bn="বদলান", message="edit", kind="secondary"),
        ]
    return []


def build_ui_actions(workflow_state: dict[str, Any] | None) -> list[dict[str, str]]:
    """Return ordered action chips for the latest bot turn."""
    ws = workflow_state or {}
    if is_expense_in_progress(ws) and is_leave_in_progress(ws):
        # Expense takes priority when both are active (mis-sync guard).
        exp = _expense_actions(ws)
        return exp if exp else _leave_actions(ws)
    if is_expense_in_progress(ws):
        return _expense_actions(ws)
    if is_leave_in_progress(ws):
        return _leave_actions(ws)
    return []
