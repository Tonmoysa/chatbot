"""
Declarative leave workflow schema — required fields, ask order, missing detection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from chat.services.leave_draft_utils import (
    WIZARD_LEAVE_TYPES,
    apply_multi_day_scope_default,
    is_multi_day_leave,
    is_reason_skip_message,
    needs_half_day_period,
    normalize_end_equals_start_if_missing,
    supporting_document_needed,
    sync_payment_from_leave_type,
    validate_dates,
)
from chat.services.leave_policies import CompanyLeavePolicy
from chat.services.leave_slot_extraction import LeaveSlotExtraction
from chat.services.leave_slots import (
    SLOT_DATE_CLARIFY,
    SLOT_DATES,
    SLOT_DOCUMENT,
    SLOT_HALF_PERIOD,
    SLOT_LEAVE_TYPE,
    SLOT_REASON,
    SLOT_SCOPE,
)

_SCHEMA_SINGLETON: LeaveWorkflowSchema | None = None


@dataclass(frozen=True)
class LeaveWorkflowSchema:
    """Leave request workflow field contract."""

    workflow_type: str = "leave_request"

    required_fields: tuple[str, ...] = (
        "start_date",
        "leave_type",
        "day_scope",
    )

    optional_fields: tuple[str, ...] = (
        "leave_payment_category",
        "end_date",
        "days",
        "reason",
        "half_day_period",
        "document_text",
    )

    ask_order: tuple[str, ...] = (
        SLOT_DATE_CLARIFY,
        SLOT_DATES,
        SLOT_LEAVE_TYPE,
        SLOT_SCOPE,
        SLOT_HALF_PERIOD,
        SLOT_REASON,
        SLOT_DOCUMENT,
    )

    validation_rules: tuple[str, ...] = field(
        default_factory=lambda: (
            "dates_not_in_past",
            "dates_valid_range",
            "supporting_document_if_sick_long",
        )
    )

    def _order(self, slots: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for s in self.ask_order:
            if s in slots and s not in seen:
                out.append(s)
                seen.add(s)
        return out

    def reason_satisfied(self, draft: dict[str, Any]) -> bool:
        if draft.get("_reason_skipped"):
            return True
        if len(str(draft.get("reason") or "").strip()) >= 4:
            return True
        if draft.get("_reason_implied"):
            return True
        lt = str(draft.get("leave_type") or "").lower()
        if lt in ("sick", "medical") and draft.get("start_date"):
            draft.setdefault("reason", "অসুস্থতা / sick leave")
            draft["_reason_implied"] = True
            return True
        return False

    def missing_fields(
        self,
        draft: dict[str, Any],
        *,
        policy: CompanyLeavePolicy | None = None,
        extraction: LeaveSlotExtraction | None = None,
        date_error: str | None = None,
    ) -> list[str]:
        """Compare collected draft against required_fields; return ordered missing slots."""
        del policy  # reserved for tenant-specific required fields later
        missing: list[str] = []

        sync_payment_from_leave_type(draft)
        apply_multi_day_scope_default(draft)

        if extraction and extraction.vague_date and not draft.get("start_date"):
            missing.append(SLOT_DATE_CLARIFY)

        if date_error:
            if SLOT_DATES not in missing:
                missing.append(SLOT_DATES)

        lt = str(draft.get("leave_type") or "").strip().lower()
        if lt not in WIZARD_LEAVE_TYPES:
            missing.append(SLOT_LEAVE_TYPE)

        if not is_multi_day_leave(draft) and not draft.get("day_scope"):
            missing.append(SLOT_SCOPE)

        if needs_half_day_period(draft) and not draft.get("half_day_period"):
            missing.append(SLOT_HALF_PERIOD)

        if not draft.get("start_date"):
            missing.append(SLOT_DATES)

        normalize_end_equals_start_if_missing(draft)
        if draft.get("start_date"):
            ok, err = validate_dates(draft)
            if not ok and err:
                if SLOT_DATES not in missing:
                    missing.append(SLOT_DATES)

        if not self.reason_satisfied(draft) and not draft.get("_reason_asked"):
            missing.append(SLOT_REASON)

        if supporting_document_needed(draft):
            if not draft.get("supporting_document_waived"):
                doc = str(draft.get("document_text") or "").strip()
                if not doc:
                    missing.append(SLOT_DOCUMENT)

        return self._order(missing)

    def is_complete(
        self,
        draft: dict[str, Any],
        *,
        policy: CompanyLeavePolicy | None = None,
        extraction: LeaveSlotExtraction | None = None,
        date_error: str | None = None,
    ) -> bool:
        return not self.missing_fields(
            draft,
            policy=policy,
            extraction=extraction,
            date_error=date_error,
        )


def mark_reason_asked(draft: dict[str, Any]) -> None:
    draft["_reason_asked"] = True


def apply_reason_skip(draft: dict[str, Any], message: str) -> bool:
    if is_reason_skip_message(message):
        draft["_reason_skipped"] = True
        draft.pop("reason", None)
        draft.pop("_reason_implied", None)
        mark_reason_asked(draft)
        return True
    return False


def get_leave_workflow_schema() -> LeaveWorkflowSchema:
    global _SCHEMA_SINGLETON
    if _SCHEMA_SINGLETON is None:
        _SCHEMA_SINGLETON = LeaveWorkflowSchema()
    return _SCHEMA_SINGLETON
