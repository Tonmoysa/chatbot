"""Leave collecting: compound slot answers must not hit suspended-leave correction."""

from __future__ import annotations

from chat.services.leave_fsm import read_leave_state
from chat.services.leave_slots import SLOT_LEAVE_TYPE, get_missing_slots
from chat.services.leave_workflow import process_leave_turn
from chat.services.session_snapshot import build_session_snapshot
from chat.services.session_turn_router import TurnKind, route_session_turn
from chat.services.workflow_suspend import KEY_SUSPENDED_LEAVE


def _active_leave_with_suspended_other_draft() -> dict:
    return {
        "active_flow": "leave",
        "status": "active",
        "step": SLOT_LEAVE_TYPE,
        "draft": {
            "start_date": "2026-06-12",
            "end_date": "2026-06-12",
        },
        KEY_SUSPENDED_LEAVE: {
            "draft": {
                "start_date": "2026-08-20",
                "end_date": "2026-08-20",
                "reason": "ব্যক্তিগত কাজ",
                "leave_type": "annual",
                "day_scope": "full",
            },
        },
        "expense_request": {"active": True, "stage": "collecting", "items": []},
    }


def test_annual_leave_full_day_routes_to_slot_not_p11() -> None:
    wf = _active_leave_with_suspended_other_draft()
    snap = build_session_snapshot("Annual leave and full day", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason != "P11_suspended_leave_correction"
    assert decision.turn_kind in (TurnKind.SLOT_ANSWER, TurnKind.CORRECTION)
    assert decision.target_workflow == "leave"


def test_annual_leave_full_day_updates_active_draft_not_suspended() -> None:
    wf = _active_leave_with_suspended_other_draft()
    pack = process_leave_turn(
        workflow_state=wf,
        message="Annual leave and full day",
        entities={},
        company_id="default",
    )
    draft = read_leave_state(pack["workflow_state"]).get("draft") or {}
    assert draft.get("leave_type") == "annual"
    assert draft.get("day_scope") == "full"
    assert draft.get("start_date") == "2026-06-12"
    suspended = (pack["workflow_state"].get(KEY_SUSPENDED_LEAVE) or {}).get("draft") or {}
    assert suspended.get("start_date") == "2026-08-20"
    assert "day_scope" not in get_missing_slots(draft) or draft.get("day_scope")


def test_submit_then_compound_answer_then_submit_reaches_review(monkeypatch) -> None:
    import datetime as dt

    fixed = dt.date(2026, 6, 11)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)

    wf = _active_leave_with_suspended_other_draft()
    wf["draft"]["reason"] = "family visit"

    pack = process_leave_turn(
        workflow_state=wf,
        message="leave submit koro",
        entities={},
        company_id="default",
    )
    q1 = pack.get("question") or ""
    assert "Full Day" in q1 or "Annual" in q1 or "Select Leave" in q1

    pack = process_leave_turn(
        workflow_state=pack["workflow_state"],
        message="Annual leave and full day",
        entities={},
        company_id="default",
    )
    draft = read_leave_state(pack["workflow_state"]).get("draft") or {}
    assert draft.get("leave_type") == "annual"
    assert draft.get("day_scope") == "full"

    pack = process_leave_turn(
        workflow_state=pack["workflow_state"],
        message="leave submit koro ekhon",
        entities={},
        company_id="default",
    )
    draft = read_leave_state(pack["workflow_state"]).get("draft") or {}
    assert draft.get("leave_type") == "annual"
    assert draft.get("day_scope") == "full"
    q2 = pack.get("question") or ""
    assert "2026-08-20" not in q2
    assert "ব্যক্তিগত কাজ" not in q2
    assert (
        "confirm" in q2.lower()
        or "ঠিক" in q2
        or "সঠিক" in q2
        or read_leave_state(pack["workflow_state"]).get("review_pending")
    )
