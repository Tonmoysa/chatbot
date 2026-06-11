"""Smoke fixes: leave summary routing, submit gate, reason→type reselect, date span, parallel leave."""

from __future__ import annotations

from chat.services.leave.date_correction import try_apply_leave_date_correction
from chat.services.leave.reason_bucket_classifier import apply_leave_semantic_reconcile
from chat.services.leave_confirm import process_confirmation_turn
from chat.services.leave_draft_utils import calendar_span_days, reconcile_leave_type_from_reason
from chat.services.leave_meta_queries import (
    session_has_leave_summary_context,
    should_block_parallel_leave_application,
    wants_leave_session_summary,
)
from chat.services.leave_fsm import mark_submitted
from chat.services.leave_slots import get_missing_slots
from chat.services.policy_intent_helpers import is_off_topic_for_hr_assistant
from chat.services.session_snapshot import build_session_snapshot
from chat.services.session_turn_router import TurnKind, route_session_turn


def test_p42_leave_summary_after_expense_submit_context() -> None:
    wf = mark_submitted(
        {"draft": {"start_date": "2026-06-25", "end_date": "2026-06-25", "leave_type": "sick"}},
        draft={"start_date": "2026-06-25", "reason": "family"},
        submission_id="PHP-TEST",
        idempotency_key="k",
    )
    wf["expense_request"] = {"active": False, "items": []}
    assert session_has_leave_summary_context(wf)
    snap = build_session_snapshot("leave summary দেখাও", workflow_state=wf)
    assert snap.leave_summary_available
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.turn_kind == TurnKind.SUMMARY
    assert decision.reason.startswith("P42")
    assert wants_leave_session_summary("leave summary দেখাও")


def test_expense_summary_not_leave_summary_message() -> None:
    from chat.services.expense_workflow import wants_expense_summary

    assert not wants_expense_summary("leave summary দেখাও")


def test_p72_spring_boot_during_expense_wizard() -> None:
    assert is_off_topic_for_hr_assistant("spring boot ki?", wizard_active=True)
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "items": [{"amount": 70, "category": "Bus"}],
        }
    }
    snap = build_session_snapshot("spring boot ki?", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.turn_kind == TurnKind.CHITCHAT
    assert decision.reason == "P72_chitchat"


def test_r03_sick_to_family_clears_leave_type() -> None:
    draft = {"reason": "onek osusto", "leave_type": "sick", "leave_payment_category": "paid"}
    draft["reason"] = "পারিবারিক কাজ"
    reconcile_leave_type_from_reason(draft)
    apply_leave_semantic_reconcile(draft, message="পারিবারিক কাজ", use_llm=False)
    assert draft.get("leave_type") is None
    assert "leave_type" in get_missing_slots(draft)


def test_r03_sick_to_personal_work_clears_stated_sick_and_asks_select_leave() -> None:
    from chat.services.leave.conversation_manager import LeaveConversationManager
    from chat.services.leave_slots import SLOT_LEAVE_TYPE
    from chat.services.leave_workflow import apply_leave_state, process_leave_turn
    from chat.services.leave_fsm import STATUS_ACTIVE, read_leave_state
    from chat.services.leave_slots import SLOT_REASON

    draft = {
        "start_date": "2026-06-12",
        "leave_type": "sick",
        "_stated_leave_type": "sick",
        "reason": "fever",
        "day_scope": "full",
    }
    reconcile_leave_type_from_reason({**draft, "reason": "personal work"})
    draft["reason"] = "personal work"
    reconcile_leave_type_from_reason(draft)
    apply_leave_semantic_reconcile(draft, message="personal work", use_llm=False)
    assert draft.get("leave_type") is None
    assert draft.get("_stated_leave_type") is None
    assert draft.get("_leave_type_reselect_required") is True
    assert SLOT_LEAVE_TYPE in get_missing_slots(draft)

    wf = apply_leave_state(
        {},
        draft={
            "start_date": "2026-06-12",
            "leave_type": "sick",
            "_stated_leave_type": "sick",
            "reason": "fever",
            "day_scope": "full",
        },
        step=SLOT_REASON,
        status=STATUS_ACTIVE,
    )
    pack = process_leave_turn(
        workflow_state=wf,
        message="personal work",
        entities={},
        company_id="default",
    )
    out = dict(read_leave_state(pack["workflow_state"]).get("draft") or {})
    assert out.get("leave_type") is None
    assert out.get("reason") == "personal work"
    question = str(pack.get("question") or "").lower()
    assert "annual leave" in question
    assert "sick leave" not in question or "select leave" in question
    prompt = LeaveConversationManager().build_follow_up(
        out,
        primary_slot=SLOT_LEAVE_TYPE,
        missing=get_missing_slots(out),
    ).lower()
    assert "annual leave" in prompt
    assert "sick leave" not in prompt


def test_date_range_span_two_days() -> None:
    draft = {"start_date": "2026-06-10", "end_date": "2026-06-10", "days": 1.0}
    changed = try_apply_leave_date_correction(
        draft, "২৫ জুন থেকে ২৬ জুন", use_llm=False
    )
    assert changed
    assert draft["start_date"] == "2026-06-25"
    assert draft["end_date"] == "2026-06-26"
    assert calendar_span_days(draft) == 2
    assert float(draft["days"]) == 2.0


def test_block_parallel_leave_during_review() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {
            "start_date": "2026-06-10",
            "leave_type": "sick",
            "reason": "osusto",
            "day_scope": "full",
        },
        "review_pending": True,
    }
    assert should_block_parallel_leave_application(
        "আগামী সপ্তাহে দুই দিনের ছুটি নিতে চাই", wf
    )


def test_incomplete_submit_blocked_at_review_yes() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {
            "leave_type": "sick",
            "reason": "অসুস্থতা",
            "day_scope": "full",
        },
        "review_pending": True,
    }
    pack = process_confirmation_turn(
        workflow_state=wf,
        message="হ্যাঁ submit করো",
        draft=wf["draft"],
    )
    assert not pack.get("confirmed_submit")
    assert pack.get("question")
    assert "তারিখ" in (pack.get("question") or "") or pack.get("workflow_state", {}).get(
        "step"
    ) == "dates"
