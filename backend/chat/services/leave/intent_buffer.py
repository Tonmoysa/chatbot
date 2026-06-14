"""
Stash leave slots from a compound application message when routing defers leave start.

When the user later sends a shorter follow-up (e.g. ``ami leave nite chai``), merged
buffer fields fill gaps without inventing dates from LLM defaults.
"""

from __future__ import annotations

from typing import Any

KEY_LEAVE_INTENT_BUFFER = "leave_intent_buffer"

_BUFFER_KEYS: tuple[str, ...] = (
    "start_date",
    "end_date",
    "days",
    "leave_type",
    "day_scope",
    "half_day_period",
    "reason",
    "leave_payment_category",
)


def _read_buffer(workflow_state: dict[str, Any] | None) -> dict[str, Any]:
    raw = (workflow_state or {}).get(KEY_LEAVE_INTENT_BUFFER) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _write_buffer(workflow_state: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    wf = dict(workflow_state or {})
    buf = _read_buffer(wf)
    for key in _BUFFER_KEYS:
        val = patch.get(key)
        if val is None or val == "":
            continue
        buf[key] = val
    if buf:
        wf[KEY_LEAVE_INTENT_BUFFER] = buf
    else:
        wf.pop(KEY_LEAVE_INTENT_BUFFER, None)
    return wf


def extract_leave_intent_patch(message: str) -> dict[str, Any]:
    """Deterministic slots from a leave-application utterance (no LLM dates)."""
    from chat.services.leave.normalization import (
        message_explicitly_states_leave_date,
        parse_day_scope_answer,
        parse_wizard_leave_type_answer,
    )
    from chat.services.leave.reason_value import extract_reason_value
    from chat.services.leave_slot_extraction import extract_leave_slots

    text = (message or "").strip()
    if not text:
        return {}

    patch: dict[str, Any] = {}
    ex = extract_leave_slots(text, skip_leave_phrase_gate=True)

    if message_explicitly_states_leave_date(text):
        if ex.start_date.confidence == "high" and ex.start_date.value:
            patch["start_date"] = str(ex.start_date.value).split("T")[0]
        if ex.end_date.confidence == "high" and ex.end_date.value:
            patch["end_date"] = str(ex.end_date.value).split("T")[0]
        elif patch.get("start_date"):
            patch["end_date"] = patch["start_date"]

    lt = parse_wizard_leave_type_answer(text)
    if lt:
        patch["leave_type"] = lt
    elif ex.leave_type.confidence == "high" and ex.leave_type.value:
        patch["leave_type"] = ex.leave_type.value

    scope = parse_day_scope_answer(text)
    if scope:
        patch["day_scope"] = scope
    elif ex.day_scope.confidence == "high" and ex.day_scope.value:
        patch["day_scope"] = ex.day_scope.value

    reason = extract_reason_value(text)
    if reason:
        patch["reason"] = reason[:2000]
    elif ex.reason.confidence == "high" and ex.reason.value:
        patch["reason"] = str(ex.reason.value)[:2000]

    if ex.days.confidence == "high" and ex.days.value:
        patch["days"] = ex.days.value

    return patch


def capture_leave_intent_buffer(
    workflow_state: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    """Merge newly extracted slots into the session leave intent buffer."""
    from chat.services.workflow_navigation import is_leave_application_message

    if not is_leave_application_message(message):
        return dict(workflow_state or {})
    patch = extract_leave_intent_patch(message)
    if not patch:
        return dict(workflow_state or {})
    return _write_buffer(workflow_state, patch)


def consume_leave_intent_buffer(
    workflow_state: dict[str, Any],
    draft: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply buffered slots to draft (only empty fields) and clear the buffer."""
    wf = dict(workflow_state or {})
    buf = _read_buffer(wf)
    if not buf:
        return wf, draft

    out_draft = dict(draft)
    for key in _BUFFER_KEYS:
        if out_draft.get(key):
            continue
        val = buf.get(key)
        if val is not None and val != "":
            out_draft[key] = val

    wf.pop(KEY_LEAVE_INTENT_BUFFER, None)
    return wf, out_draft


def clear_leave_intent_buffer(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = dict(workflow_state or {})
    wf.pop(KEY_LEAVE_INTENT_BUFFER, None)
    return wf
