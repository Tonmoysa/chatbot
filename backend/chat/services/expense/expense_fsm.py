"""
Expense workflow FSM — active/paused/stage on workflow_state["expense_request"].
"""

from __future__ import annotations

from datetime import date
from typing import Any

from chat.services.expense.slots import (
    STAGE_COLLECTING,
    STAGE_REVIEW,
    STAGE_SUBMIT_CONFIRM,
)
from chat.services.expense.workflow_schema import get_expense_workflow_schema

WORKFLOW_TYPE_EXPENSE = "expense_request"
KEY_EXPENSE_BLOCK = "expense_request"
KEY_EXPENSE_LAST_SUBMISSION = "expense_last_submission"
KEY_EXPENSE_SUBMISSIONS_HISTORY = "expense_submissions_history"


def clone_workflow_state(state: dict[str, Any] | None) -> dict[str, Any]:
    return dict(state or {})


def read_expense_block(workflow_state: dict[str, Any] | None) -> dict[str, Any]:
    return (workflow_state or {}).get(KEY_EXPENSE_BLOCK) or {}


def normalize_expense_stage(stage: str) -> str:
    return get_expense_workflow_schema().normalize_stage(stage)


def is_expense_collecting(workflow_state: dict[str, Any] | None) -> bool:
    block = read_expense_block(workflow_state)
    return bool(block.get("active")) and not bool(block.get("paused"))


def is_expense_paused(workflow_state: dict[str, Any] | None) -> bool:
    block = read_expense_block(workflow_state)
    return bool(block.get("active")) and bool(block.get("paused"))


def is_expense_in_progress(workflow_state: dict[str, Any] | None) -> bool:
    block = read_expense_block(workflow_state)
    return bool(block.get("active"))


def is_expense_review(block: dict[str, Any]) -> bool:
    return normalize_expense_stage(str(block.get("stage") or "")) == STAGE_REVIEW


def is_expense_submit_confirm(block: dict[str, Any]) -> bool:
    return (
        normalize_expense_stage(str(block.get("stage") or "")) == STAGE_SUBMIT_CONFIRM
    )


def pause_expense_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = clone_workflow_state(workflow_state)
    block = wf.setdefault(KEY_EXPENSE_BLOCK, {})
    block["active"] = True
    block["paused"] = True
    return wf


def resume_expense_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = clone_workflow_state(workflow_state)
    block = wf.get(KEY_EXPENSE_BLOCK) or {}
    if not block.get("active"):
        return wf
    block.pop("paused", None)
    block["active"] = True
    wf[KEY_EXPENSE_BLOCK] = block
    return wf


def save_expense_last_submission(
    workflow_state: dict[str, Any],
    *,
    reference_id: str,
    items: list[dict[str, Any]],
    incurred_date_iso: str = "",
) -> dict[str, Any]:
    wf = clone_workflow_state(workflow_state)
    batch = {
        "reference_id": str(reference_id or "").strip(),
        "items": [dict(x) for x in items],
        "incurred_date_iso": str(incurred_date_iso or "").strip(),
        "submitted_at": date.today().isoformat(),
    }
    wf[KEY_EXPENSE_LAST_SUBMISSION] = batch
    history = list(wf.get(KEY_EXPENSE_SUBMISSIONS_HISTORY) or [])
    ref = batch["reference_id"]
    if ref and not any(str(h.get("reference_id") or "") == ref for h in history if isinstance(h, dict)):
        history.append(dict(batch))
    elif not ref:
        history.append(dict(batch))
    wf[KEY_EXPENSE_SUBMISSIONS_HISTORY] = history
    return wf


def deactivate_expense_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = clone_workflow_state(workflow_state)
    wf.pop(KEY_EXPENSE_BLOCK, None)
    return wf


def finalize_expense_submission(
    workflow_state: dict[str, Any],
    *,
    reference_id: str,
    items: list[dict[str, Any]],
    incurred_date_iso: str = "",
) -> dict[str, Any]:
    """Persist submission archive and clear the in-chat draft (terminal lock)."""
    from chat.services.expense.session_action_memory import record_expense_submitted

    wf = save_expense_last_submission(
        workflow_state,
        reference_id=reference_id,
        items=items,
        incurred_date_iso=incurred_date_iso,
    )
    wf = record_expense_submitted(
        wf,
        items=items,
        reference_id=reference_id,
        incurred_date_iso=incurred_date_iso,
    )
    return deactivate_expense_session(wf)


def set_expense_stage(block: dict[str, Any], stage: str) -> None:
    block["stage"] = normalize_expense_stage(stage)


def ensure_expense_block_active(block: dict[str, Any]) -> None:
    block["active"] = True
    block.setdefault("workflow_type", WORKFLOW_TYPE_EXPENSE)
    block.setdefault("stage", STAGE_COLLECTING)
