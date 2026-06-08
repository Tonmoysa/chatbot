"""
Dynamic slot-filling: missing-slot detection and natural question generation.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.leave_policies import CompanyLeavePolicy
from chat.services.leave_slot_extraction import LeaveSlotExtraction

# Priority order for asking (only missing slots are asked)
SLOT_LEAVE_TYPE = "leave_type"
SLOT_PAYMENT = "leave_payment_category"
SLOT_SCOPE = "day_scope"
SLOT_DATES = "leave_dates"
SLOT_REASON = "reason"
SLOT_DOCUMENT = "supporting_document"
SLOT_DATE_CLARIFY = "date_clarification"

def prefill_draft_from_extraction(
    draft: dict[str, Any],
    extraction: LeaveSlotExtraction,
    *,
    external_entities: dict[str, Any] | None = None,
    overwrite: bool = False,
) -> None:
    """Apply high-confidence slots into workflow draft."""
    if external_entities:
        from chat.services.leave_workflow import merge_extractor_entities

        merge_extractor_entities(
            draft, external_entities, overwrite=overwrite, message=""
        )

    for field, slot_name in (
        ("leave_type", "leave_type"),
        ("start_date", "start_date"),
        ("end_date", "end_date"),
        ("days", "days"),
        ("leave_payment_category", "leave_payment_category"),
        ("day_scope", "day_scope"),
        ("reason", "reason"),
    ):
        sv = getattr(extraction, slot_name)
        if sv.confidence != "high" or sv.value is None:
            continue
        if draft.get(field) and not overwrite:
            continue
        val = sv.value
        if field == "leave_payment_category":
            draft[field] = "paid" if val == "paid" else "lwop"
        elif field == "day_scope":
            draft[field] = "half" if val == "half" else "full"
        else:
            draft[field] = val
        if field == "reason" and sv.source.startswith("implied"):
            draft["_reason_implied"] = True

    if (
        extraction.start_date.confidence == "high"
        and not draft.get("end_date")
        and draft.get("start_date")
    ):
        draft["end_date"] = draft.get("start_date")

    if (
        extraction.days.confidence == "high"
        and extraction.days.value
        and not draft.get("days")
    ):
        draft["days"] = extraction.days.value

    from chat.services.leave_draft_utils import apply_duration_end_date

    apply_duration_end_date(draft)


def apply_wizard_answer(
    draft: dict[str, Any],
    *,
    pending_slot: str,
    message: str,
) -> None:
    """Parse a direct answer to the last asked slot (short replies like paid / full)."""
    from chat.services.leave_workflow import (
        _infer_day_scope,
        _infer_payment_category,
        _reason_from_message,
    )

    msg = (message or "").strip()
    if pending_slot == SLOT_LEAVE_TYPE:
        _infer_payment_category(msg, draft, force=True)
    elif pending_slot == SLOT_PAYMENT:
        _infer_payment_category(msg, draft, force=True)
    elif pending_slot == SLOT_SCOPE:
        _infer_day_scope(msg, draft)
    elif pending_slot == SLOT_DATE_CLARIFY:
        from chat.services.leave_slot_extraction import extract_leave_slots

        ex = extract_leave_slots(msg, skip_leave_phrase_gate=True)
        prefill_draft_from_extraction(draft, ex, external_entities=None)
    elif pending_slot == SLOT_DATES:
        from chat.services.leave_slot_extraction import extract_leave_slots

        ex = extract_leave_slots(msg, skip_leave_phrase_gate=True)
        prefill_draft_from_extraction(draft, ex)
    elif pending_slot == SLOT_REASON:
        edit_ctx = len(re.findall(r"\S+", msg)) > 4 or bool(
            re.search(r"\b(change|update|kor[eo]|hobe|ashole)\b", msg, re.I)
        )
        r = _reason_from_message(msg, edit_context=edit_ctx)
        if r:
            draft["reason"] = r
            draft.pop("_reason_implied", None)
    elif pending_slot == SLOT_DOCUMENT:
        from chat.services.leave.document_turn_parser import apply_document_answer

        apply_document_answer(draft, msg, use_llm=False)
    else:
        _infer_payment_category(msg, draft)
        _infer_day_scope(msg, draft)


def get_missing_slots(
    draft: dict[str, Any],
    *,
    policy: CompanyLeavePolicy | None = None,
    extraction: LeaveSlotExtraction | None = None,
    date_error: str | None = None,
) -> list[str]:
    """Return ordered list of slots still needed (dynamic, not fixed 1..5)."""
    from chat.services.leave.workflow_schema import get_leave_workflow_schema

    return get_leave_workflow_schema().missing_fields(
        draft,
        policy=policy,
        extraction=extraction,
        date_error=date_error,
    )


def generate_question(
    slot: str,
    draft: dict[str, Any],
    *,
    remaining: int,
    date_error: str | None = None,
    extraction: LeaveSlotExtraction | None = None,
    missing: list[str] | None = None,
) -> str:
    """Natural, contextual prompts — no rigid Question X/Y numbering."""
    from chat.services.leave.conversation_manager import LeaveConversationManager

    slots_missing = list(missing) if missing is not None else get_missing_slots(
        draft, extraction=extraction, date_error=date_error
    )
    return LeaveConversationManager().build_follow_up(
        draft,
        primary_slot=slot,
        missing=slots_missing,
        date_error=date_error,
        extraction=extraction,
    )


def summarize_captured(draft: dict[str, Any]) -> str:
    """Deprecated — keep empty so UX stays natural (no debug-style acks)."""
    return ""
