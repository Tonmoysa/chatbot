"""
Declarative expense workflow schema — required fields, ask order, missing detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from chat.services.expense.normalization import pending_line_ready
from chat.services.expense.slots import (
    SLOT_AMOUNT,
    SLOT_CATEGORY,
    SLOT_FROM_TO,
    SLOT_ITEMS,
    SLOT_INCURRED_DATE,
    SLOT_MORE_LINES,
    SLOT_REVIEW,
    SLOT_SUBMIT_CONFIRM,
    SLOT_ASK_ORDER,
    STAGE_COLLECTING,
    STAGE_REVIEW,
    STAGE_SUBMIT_CONFIRM,
)
from chat.services.expense_extraction import is_travel_category

_SCHEMA_SINGLETON: ExpenseWorkflowSchema | None = None


@dataclass(frozen=True)
class ExpenseWorkflowSchema:
    """Expense request workflow field contract."""

    workflow_type: str = "expense_request"

    required_global_fields: tuple[str, ...] = (
        "incurred_date_iso",
        "items",
    )

    required_line_fields: tuple[str, ...] = (
        "category",
        "amount",
    )

    travel_line_fields: tuple[str, ...] = (
        "from_location",
        "to_location",
    )

    ask_order: tuple[str, ...] = SLOT_ASK_ORDER

    validation_rules: tuple[str, ...] = field(
        default_factory=lambda: (
            "items_non_empty",
            "positive_amounts",
            "travel_from_to_required",
            "incurred_date_not_future",
        )
    )

    @staticmethod
    def normalize_stage(stage: str) -> str:
        s = (stage or STAGE_COLLECTING).strip().lower()
        if s == "confirming":
            return STAGE_REVIEW
        return s or STAGE_COLLECTING

    def _order(self, slots: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for s in self.ask_order:
            if s in slots and s not in seen:
                out.append(s)
                seen.add(s)
        return out

    @staticmethod
    def has_pending_line(block: dict[str, Any]) -> bool:
        pending = block.get("pending_line")
        if isinstance(pending, dict) and pending.get("amount"):
            return True
        return bool(block.get("pending_queue"))

    def pending_line_missing(
        self,
        pending: dict[str, Any],
        *,
        pending_step: str = "category",
    ) -> list[str]:
        missing: list[str] = []
        try:
            amt = float(pending.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if amt <= 0:
            missing.append(SLOT_AMOUNT)

        step = (pending_step or "category").strip().lower()
        cat = str(pending.get("category") or "").strip()

        if step == "category" and not cat:
            missing.append(SLOT_CATEGORY)
            return self._order(missing)

        if cat and is_travel_category(cat):
            frm = str(pending.get("from_location") or "").strip()
            to = str(pending.get("to_location") or "").strip()
            if not frm or not to:
                missing.append(SLOT_FROM_TO)

        return self._order(missing)

    def missing_fields(
        self,
        block: dict[str, Any],
        items: list[dict[str, Any]] | None = None,
    ) -> list[str]:
        """Return ordered wizard slots still needed for the current stage."""
        items = list(items or block.get("items") or [])
        stage = self.normalize_stage(str(block.get("stage") or STAGE_COLLECTING))
        missing: list[str] = []

        if stage == STAGE_SUBMIT_CONFIRM:
            return [SLOT_SUBMIT_CONFIRM]
        if stage == STAGE_REVIEW:
            return [SLOT_REVIEW]

        if not str(block.get("incurred_date_iso") or "").strip():
            missing.append(SLOT_INCURRED_DATE)

        pending = block.get("pending_line")
        if isinstance(pending, dict) and pending.get("amount"):
            step = str(block.get("pending_step") or "category")
            missing.extend(self.pending_line_missing(pending, pending_step=step))
            return self._order(missing)

        if self.has_pending_line(block):
            missing.append(SLOT_CATEGORY)
            return self._order(missing)

        if not items:
            missing.append(SLOT_ITEMS)
            return self._order(missing)

        missing.append(SLOT_MORE_LINES)
        return self._order(missing)

    def primary_slot(
        self,
        block: dict[str, Any],
        items: list[dict[str, Any]] | None = None,
    ) -> str | None:
        missing = self.missing_fields(block, items)
        return missing[0] if missing else None

    def is_collecting_complete(
        self,
        block: dict[str, Any],
        items: list[dict[str, Any]] | None = None,
    ) -> bool:
        """True when no pending line remains and at least one item is collected."""
        items = list(items or block.get("items") or [])
        if self.has_pending_line(block):
            return False
        return bool(items)

    def can_finalize_pending(self, pending: dict[str, Any]) -> bool:
        return pending_line_ready(pending)


def get_expense_workflow_schema() -> ExpenseWorkflowSchema:
    global _SCHEMA_SINGLETON
    if _SCHEMA_SINGLETON is None:
        _SCHEMA_SINGLETON = ExpenseWorkflowSchema()
    return _SCHEMA_SINGLETON
