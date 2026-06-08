"""Apply parsed leave field updates to a draft."""

from __future__ import annotations

import re
from typing import Any


def _is_iso_date(s: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", (s or "").strip()))

from chat.services.leave.reason_value import extract_reason_value
from chat.services.leave.turn_schema import LeaveFieldUpdate
from chat.services.leave_slots import SLOT_DATES, SLOT_PAYMENT, SLOT_REASON, SLOT_SCOPE
from chat.services.leave.normalization import normalize_leave_draft


def apply_leave_field_update(
    draft: dict[str, Any],
    update: LeaveFieldUpdate,
    *,
    message: str = "",
) -> bool:
    """Apply one slot update; returns True when draft changed."""
    if not update or not update.slot:
        return False

    slot = update.slot
    val = update.value
    raw = (update.raw_value or message or "").strip()
    changed = False

    if slot == SLOT_REASON:
        reason = str(val or "").strip()
        if not reason:
            reason = extract_reason_value(raw, edit_context=True) or ""
        if len(reason) >= 3:
            draft["reason"] = reason[:2000]
            draft.pop("_reason_implied", None)
            changed = True

    elif slot == SLOT_SCOPE:
        from chat.services.leave_workflow import _force_scope_from_message

        if val in ("full", "half"):
            draft["day_scope"] = val
            changed = True
        elif _force_scope_from_message(raw or str(val), draft):
            changed = True

    elif slot == SLOT_PAYMENT:
        from chat.services.leave_workflow import _infer_payment_category

        if val in ("paid", "lwop"):
            draft["leave_payment_category"] = val
            changed = True
        else:
            before = draft.get("leave_payment_category")
            _infer_payment_category(raw or str(val), draft, force=True)
            changed = draft.get("leave_payment_category") != before or before is None

    elif slot == SLOT_DATES:
        from chat.services.leave_slot_extraction import extract_leave_slots
        from chat.services.leave_slots import prefill_draft_from_extraction

        if val and _is_iso_date(str(val)):
            draft["start_date"] = str(val).split("T")[0]
            draft.setdefault("end_date", draft["start_date"])
            changed = True
        else:
            ex = extract_leave_slots(raw, skip_leave_phrase_gate=True)
            before = (draft.get("start_date"), draft.get("end_date"))
            prefill_draft_from_extraction(draft, ex, overwrite=True)
            after = (draft.get("start_date"), draft.get("end_date"))
            changed = before != after

    if changed:
        normalize_leave_draft(draft)
    return changed


def apply_leave_inline_edit(
    draft: dict[str, Any],
    slot: str,
    message: str,
    entities: dict[str, Any] | None = None,
) -> bool:
    """Rules-first inline edit used at review and edit-menu turns."""
    from chat.services.leave.turn_parser import extract_inline_field_value

    val = extract_inline_field_value(
        slot, message, entities=entities, edit_context=True
    )
    if val:
        return apply_leave_field_update(
            draft,
            LeaveFieldUpdate(slot=slot, value=val, raw_value=message),
            message=message,
        )

    if slot == SLOT_REASON:
        return False

    from chat.services.leave_confirm import try_apply_inline_edit_value

    return try_apply_inline_edit_value(draft, slot, message)
