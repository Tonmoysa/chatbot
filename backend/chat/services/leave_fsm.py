"""
Canonical leave workflow state on ConversationSession.workflow_state.

Single source of truth — no parallel ``leave_request`` block.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

WORKFLOW_SCHEMA_VERSION = 1

ACTIVE_FLOW_LEAVE = "leave"
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_SUBMITTED = "submitted"

# Keys owned by leave flow (top-level on workflow_state)
KEY_SCHEMA_VERSION = "workflow_schema_version"
KEY_ACTIVE_FLOW = "active_flow"
KEY_STATUS = "status"
KEY_DRAFT = "draft"
KEY_STEP = "step"
KEY_REVIEW_PENDING = "review_pending"
KEY_SUBMISSION_ID = "submission_id"
KEY_SUBMITTED_AT = "submitted_at"
KEY_IDEMPOTENCY_KEY = "idempotency_key"
KEY_CRM_DRAFT_ID = "crm_draft_id"
KEY_LOCKED = "locked"
KEY_EDIT_SNAPSHOT = "leave_edit_snapshot"
KEY_LEAVE_LAST_SUBMISSION = "leave_last_submission"


def deep_merge_draft(existing: dict[str, Any] | None, patch: dict[str, Any] | None) -> dict[str, Any]:
    """Merge slot updates without dropping filled fields."""
    out = dict(existing or {})
    for key, value in (patch or {}).items():
        if value is None or value == "":
            continue
        if key.startswith("_"):
            out[key] = value
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge_draft(out.get(key), value)
        else:
            out[key] = value
    return out


def normalize_workflow_state(workflow_state: dict[str, Any] | None) -> dict[str, Any]:
    """
    Return workflow_state with legacy ``leave_request`` migrated into canonical keys.
    Does not mutate the input dict.
    """
    wf = dict(workflow_state or {})
    if wf.get(KEY_ACTIVE_FLOW) == ACTIVE_FLOW_LEAVE or _has_canonical_leave(wf):
        return _ensure_schema_version(_canonical_from_top_level(wf))

    legacy = dict(wf.get("leave_request") or {})
    if not legacy:
        return wf

    stage = str(legacy.get("stage") or "")
    review = stage in ("review_pending", "awaiting_confirmation")
    status = STATUS_SUBMITTED if wf.get(KEY_LOCKED) else (
        STATUS_PAUSED if legacy.get("paused") and not legacy.get("active") else STATUS_ACTIVE
    )
    if wf.get(KEY_STATUS) == STATUS_SUBMITTED:
        status = STATUS_SUBMITTED

    out = dict(wf)
    out.pop("leave_request", None)
    out[KEY_SCHEMA_VERSION] = WORKFLOW_SCHEMA_VERSION
    out[KEY_ACTIVE_FLOW] = None if status == STATUS_SUBMITTED else ACTIVE_FLOW_LEAVE
    out[KEY_STATUS] = status
    out[KEY_DRAFT] = dict(legacy.get("draft") or out.get(KEY_DRAFT) or {})
    out[KEY_STEP] = legacy.get("pending_slot") or out.get(KEY_STEP)
    out[KEY_REVIEW_PENDING] = bool(review or out.get(KEY_REVIEW_PENDING))
    out[KEY_LOCKED] = bool(status == STATUS_SUBMITTED or out.get(KEY_LOCKED))
    return out


def _has_canonical_leave(wf: dict[str, Any]) -> bool:
    return KEY_DRAFT in wf and wf.get(KEY_ACTIVE_FLOW) == ACTIVE_FLOW_LEAVE


def _canonical_from_top_level(wf: dict[str, Any]) -> dict[str, Any]:
    out = dict(wf)
    out.pop("leave_request", None)
    if out.get(KEY_ACTIVE_FLOW) != ACTIVE_FLOW_LEAVE and out.get(KEY_STATUS) != STATUS_SUBMITTED:
        if not out.get(KEY_DRAFT):
            return out
    return _ensure_schema_version(out)


def _ensure_schema_version(wf: dict[str, Any]) -> dict[str, Any]:
    out = dict(wf)
    out.setdefault(KEY_SCHEMA_VERSION, WORKFLOW_SCHEMA_VERSION)
    return out


def read_leave_state(workflow_state: dict[str, Any] | None) -> dict[str, Any]:
    """Normalized view of leave-related fields."""
    wf = normalize_workflow_state(workflow_state)
    status = str(wf.get(KEY_STATUS) or "")
    return {
        "active_flow": wf.get(KEY_ACTIVE_FLOW),
        "status": status,
        "draft": dict(wf.get(KEY_DRAFT) or {}),
        "step": wf.get(KEY_STEP),
        "review_pending": bool(wf.get(KEY_REVIEW_PENDING)),
        "submission_id": str(wf.get(KEY_SUBMISSION_ID) or ""),
        "submitted_at": str(wf.get(KEY_SUBMITTED_AT) or ""),
        "idempotency_key": str(wf.get(KEY_IDEMPOTENCY_KEY) or ""),
        "crm_draft_id": str(wf.get(KEY_CRM_DRAFT_ID) or ""),
        "locked": bool(wf.get(KEY_LOCKED)) or status == STATUS_SUBMITTED,
    }


def read_leave_last_submission(workflow_state: dict[str, Any] | None) -> dict[str, Any]:
    """Archived submission — survives a new in-session leave draft."""
    wf = normalize_workflow_state(workflow_state)
    last = dict(wf.get(KEY_LEAVE_LAST_SUBMISSION) or {})
    if last.get("submission_id"):
        return last
    st = read_leave_state(wf)
    if st.get("submission_id") and (
        st.get("locked") or st.get("status") == STATUS_SUBMITTED
    ):
        return {
            "submission_id": str(st.get("submission_id") or ""),
            "submitted_at": str(st.get("submitted_at") or ""),
            "draft": dict(st.get("draft") or {}),
        }
    return {}


def is_leave_submission_locked(workflow_state: dict[str, Any] | None) -> bool:
    """True only when the *current* leave flow is terminal — not merely prior session history."""
    st = read_leave_state(workflow_state)
    return bool(st.get("locked")) and st.get("status") == STATUS_SUBMITTED


def has_leave_submission_history(workflow_state: dict[str, Any] | None) -> bool:
    """Whether any leave was submitted earlier in this session (archive may survive a new draft)."""
    return bool(read_leave_last_submission(workflow_state).get("submission_id"))


def is_leave_flow_active(workflow_state: dict[str, Any] | None) -> bool:
    st = read_leave_state(workflow_state)
    if st.get("locked"):
        return False
    return (
        st.get("active_flow") == ACTIVE_FLOW_LEAVE
        and st.get("status") == STATUS_ACTIVE
    )


def is_leave_collecting(workflow_state: dict[str, Any] | None) -> bool:
    st = read_leave_state(workflow_state)
    return (
        st.get("active_flow") == ACTIVE_FLOW_LEAVE
        and st.get("status") == STATUS_ACTIVE
        and not st.get("review_pending")
        and not st.get("locked")
    )


def is_leave_paused(workflow_state: dict[str, Any] | None) -> bool:
    st = read_leave_state(workflow_state)
    return (
        st.get("active_flow") == ACTIVE_FLOW_LEAVE
        and st.get("status") == STATUS_PAUSED
        and not st.get("locked")
    )


def is_leave_in_progress(workflow_state: dict[str, Any] | None) -> bool:
    st = read_leave_state(workflow_state)
    if st.get("locked"):
        return False
    return st.get("active_flow") == ACTIVE_FLOW_LEAVE and st.get("status") in (
        STATUS_ACTIVE,
        STATUS_PAUSED,
    )


def is_awaiting_leave_confirmation(workflow_state: dict[str, Any] | None) -> bool:
    st = read_leave_state(workflow_state)
    return (
        st.get("active_flow") == ACTIVE_FLOW_LEAVE
        and st.get("status") == STATUS_ACTIVE
        and bool(st.get("review_pending"))
        and not st.get("locked")
    )


def apply_leave_state(
    workflow_state: dict[str, Any],
    *,
    draft: dict[str, Any],
    step: str | None,
    status: str,
    review_pending: bool = False,
    crm_draft_id: str | None = None,
) -> dict[str, Any]:
    """Write canonical leave fields; clears legacy ``leave_request``."""
    wf = normalize_workflow_state(workflow_state)
    wf.pop("leave_request", None)
    wf[KEY_SCHEMA_VERSION] = WORKFLOW_SCHEMA_VERSION
    wf[KEY_DRAFT] = dict(draft)
    wf[KEY_STEP] = step
    wf[KEY_STATUS] = status
    wf[KEY_REVIEW_PENDING] = bool(review_pending)
    wf[KEY_LOCKED] = False
    if status == STATUS_SUBMITTED:
        wf[KEY_ACTIVE_FLOW] = None
        wf[KEY_LOCKED] = True
    else:
        wf[KEY_ACTIVE_FLOW] = ACTIVE_FLOW_LEAVE
    if crm_draft_id is not None:
        wf[KEY_CRM_DRAFT_ID] = crm_draft_id
    return wf


def mark_review_pending(workflow_state: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    return apply_leave_state(
        workflow_state,
        draft=draft,
        step=None,
        status=STATUS_ACTIVE,
        review_pending=True,
    )


def mark_submitted(
    workflow_state: dict[str, Any],
    *,
    draft: dict[str, Any],
    submission_id: str,
    idempotency_key: str = "",
) -> dict[str, Any]:
    """Terminal state after successful CRM submit."""
    wf = normalize_workflow_state(workflow_state)
    wf.pop("leave_request", None)
    wf[KEY_SCHEMA_VERSION] = WORKFLOW_SCHEMA_VERSION
    wf[KEY_ACTIVE_FLOW] = None
    wf[KEY_STATUS] = STATUS_SUBMITTED
    wf[KEY_DRAFT] = dict(draft)
    wf[KEY_STEP] = None
    wf[KEY_REVIEW_PENDING] = False
    wf[KEY_SUBMISSION_ID] = str(submission_id or "")
    wf[KEY_SUBMITTED_AT] = datetime.now(timezone.utc).isoformat()
    wf[KEY_LOCKED] = True
    if idempotency_key:
        wf[KEY_IDEMPOTENCY_KEY] = idempotency_key
    wf[KEY_LEAVE_LAST_SUBMISSION] = {
        "submission_id": str(submission_id or ""),
        "submitted_at": wf[KEY_SUBMITTED_AT],
        "draft": dict(draft),
    }
    return wf


def clear_leave_flow(workflow_state: dict[str, Any]) -> dict[str, Any]:
    """Remove leave workflow; preserve submission record if already submitted."""
    wf = dict(workflow_state or {})
    st = read_leave_state(wf)
    if st.get("status") == STATUS_SUBMITTED:
        return mark_submitted(
            wf,
            draft=st.get("draft") or {},
            submission_id=st.get("submission_id") or "",
            idempotency_key=st.get("idempotency_key") or "",
        )
    for key in (
        KEY_ACTIVE_FLOW,
        KEY_STATUS,
        KEY_DRAFT,
        KEY_STEP,
        KEY_REVIEW_PENDING,
        KEY_CRM_DRAFT_ID,
        KEY_LOCKED,
    ):
        wf.pop(key, None)
    wf.pop("leave_request", None)
    from chat.services.leave.intent_buffer import clear_leave_intent_buffer

    return clear_leave_intent_buffer(wf)

