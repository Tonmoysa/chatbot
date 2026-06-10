"""
Suspend / restore workflows when the user temporarily switches context.

Leave and expense wizards are mutually exclusive while active, but a switch
(e.g. finish expense, then continue leave) must not discard in-progress data.
Snapshots live in ``suspended_leave`` / ``suspended_expense`` on workflow_state.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense_workflow import clone_workflow_state as clone_expense_wf
from chat.services.leave_fsm import (
    ACTIVE_FLOW_LEAVE,
    KEY_EDIT_SNAPSHOT,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    apply_leave_state,
    clear_leave_flow,
    normalize_workflow_state,
    read_leave_state,
)

KEY_SUSPENDED_LEAVE = "suspended_leave"
KEY_SUSPENDED_EXPENSE = "suspended_expense"
KEY_RESTORE_LEAVE_AFTER_EXPENSE = "restore_leave_after_expense_submit"

# Banglish / Bengali / English — user phrasing varies; keep patterns broad.
_NAV_VERB = (
    r"(?:back|return|resume|asho|as[o]|ferot|ফির|আস|চালু|continue|jao|যা|"
    r"e\s+back|e\s+asho|e\s+as[o])"
)
_RESUME_SUSPENDED_LEAVE_RE = re.compile(
    r"(?:"
    rf"(?:leave|chuti|chhuti|chutti|ছুটি)(?:\s*(?:request|form|abedon|application|আবেদন))?"
    rf".{{0,35}}{_NAV_VERB}(?:\s*(?:koro|kor|কর|দাও|dao|daw))?|"
    rf"{_NAV_VERB}.{{0,30}}(?:leave|chuti|chhuti|ছুটি)|"
    r"leave\s+request.{0,35}(?:back|return|resume|e\s+back|again|abar|asho|as[o])|"
    r"(?:ছুটি|leave).{0,30}(?:back|ferot|return|e\s+back|আবার|ফির|আসো|আস)|"
    r"back\s+to\s+leave"
    r")",
    re.I | re.UNICODE,
)


def wants_resume_suspended_leave(message: str) -> bool:
    """User wants to return to a parked leave draft (any phrasing)."""
    t = (message or "").strip()
    if not t:
        return False
    try:
        from chat.services.leave_confirm import (
            is_confirmation_yes,
            wants_defer_expense_for_leave_submit,
        )

        if is_confirmation_yes(t):
            return False
        if wants_defer_expense_for_leave_submit(t):
            return True
    except Exception:
        pass
    return bool(_RESUME_SUSPENDED_LEAVE_RE.search(t))


def switch_active_expense_to_suspended_leave(workflow_state: dict[str, Any]) -> dict[str, Any]:
    """Park the current expense wizard and restore the suspended leave snapshot."""
    wf = dict(workflow_state or {})
    if (wf.get("expense_request") or {}).get("active"):
        wf = suspend_expense_for_workflow_switch(wf)
    return restore_suspended_leave(wf, force_active=True)


def has_suspended_leave(workflow_state: dict[str, Any] | None) -> bool:
    sl = (workflow_state or {}).get(KEY_SUSPENDED_LEAVE) or {}
    if not isinstance(sl, dict) or not sl:
        return False
    if sl.get("review_pending"):
        return True
    if sl.get("step"):
        return True
    draft = sl.get("draft") or {}
    return bool(draft)


def has_suspended_expense(workflow_state: dict[str, Any] | None) -> bool:
    se = (workflow_state or {}).get(KEY_SUSPENDED_EXPENSE) or {}
    if not isinstance(se, dict):
        return False
    block = se.get("expense_request") if "expense_request" in se else se
    if not isinstance(block, dict):
        return False
    return bool(block.get("active")) or bool(block.get("items")) or bool(
        block.get("pending_line")
    )


def suspend_leave_for_workflow_switch(workflow_state: dict[str, Any]) -> dict[str, Any]:
    """Park in-progress leave while another workflow runs."""
    wf = normalize_workflow_state(workflow_state)
    st = read_leave_state(wf)
    if st.get("active_flow") != ACTIVE_FLOW_LEAVE:
        return wf
    snapshot = {
        "draft": dict(st.get("draft") or {}),
        "step": st.get("step"),
        "status": st.get("status"),
        "review_pending": bool(st.get("review_pending")),
        "crm_draft_id": str(st.get("crm_draft_id") or ""),
    }
    if wf.get(KEY_EDIT_SNAPSHOT):
        snapshot[KEY_EDIT_SNAPSHOT] = dict(wf.get(KEY_EDIT_SNAPSHOT) or {})
    wf = clear_leave_flow(wf)
    wf[KEY_SUSPENDED_LEAVE] = snapshot
    return wf


def restore_suspended_leave(
    workflow_state: dict[str, Any],
    *,
    force_active: bool = False,
) -> dict[str, Any]:
    wf = dict(workflow_state or {})
    sl = wf.pop(KEY_SUSPENDED_LEAVE, None)
    if not isinstance(sl, dict) or not sl:
        return wf
    status = str(sl.get("status") or STATUS_ACTIVE)
    if force_active or status == STATUS_PAUSED:
        status = STATUS_ACTIVE
    crm_id = str(sl.get("crm_draft_id") or "").strip() or None
    wf = apply_leave_state(
        wf,
        draft=dict(sl.get("draft") or {}),
        step=sl.get("step"),
        status=status,
        review_pending=bool(sl.get("review_pending")),
        crm_draft_id=crm_id,
    )
    snap_edit = sl.get(KEY_EDIT_SNAPSHOT)
    if snap_edit:
        wf[KEY_EDIT_SNAPSHOT] = snap_edit
    return wf


def clear_suspended_leave(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = dict(workflow_state or {})
    wf.pop(KEY_SUSPENDED_LEAVE, None)
    return wf


def suspend_expense_for_workflow_switch(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = clone_expense_wf(workflow_state)
    block = wf.get("expense_request")
    if not isinstance(block, dict) or not block.get("active"):
        return wf
    wf[KEY_SUSPENDED_EXPENSE] = {"expense_request": dict(block)}
    wf.pop("expense_request", None)
    return wf


def restore_suspended_expense(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = dict(workflow_state or {})
    se = wf.pop(KEY_SUSPENDED_EXPENSE, None)
    if not isinstance(se, dict):
        return wf
    block = se.get("expense_request")
    if not isinstance(block, dict):
        return wf
    block = dict(block)
    block["active"] = True
    block.pop("paused", None)
    wf["expense_request"] = block
    return wf


def clear_suspended_expense(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = dict(workflow_state or {})
    wf.pop(KEY_SUSPENDED_EXPENSE, None)
    return wf


def mark_restore_leave_after_expense_submit(workflow_state: dict[str, Any]) -> dict[str, Any]:
    """User explicitly asked to finish expense before continuing leave."""
    wf = dict(workflow_state or {})
    wf[KEY_RESTORE_LEAVE_AFTER_EXPENSE] = True
    return wf


def should_restore_leave_after_expense_submit(workflow_state: dict[str, Any] | None) -> bool:
    return bool((workflow_state or {}).get(KEY_RESTORE_LEAVE_AFTER_EXPENSE))


def clear_restore_leave_after_expense_submit(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = dict(workflow_state or {})
    wf.pop(KEY_RESTORE_LEAVE_AFTER_EXPENSE, None)
    return wf
