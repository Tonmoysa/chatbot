"""Mandatory expense draft finalize — single invariant gate after mutations."""

from __future__ import annotations

from typing import Any


def finalize_expense_draft(
    block: dict[str, Any],
    items: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    Sanitize + rebalance committed lines and pending slots.

    Every expense draft mutation should pass through this before persisting.
    """
    from chat.services.expense.expense_draft_sanitize import sanitize_expense_draft_block

    work = dict(block or {})
    if items is not None:
        work["items"] = list(items)
    return sanitize_expense_draft_block(work)
