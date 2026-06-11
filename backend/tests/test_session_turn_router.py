"""
Golden tests for session_turn_router priority matrix (TURN_ROUTER_SPEC §9).

These run without the orchestrator — fast regression guard for routing conflicts.
"""

from __future__ import annotations

import datetime as dt

import pytest

from chat.constants import (
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_EXPENSE_STATUS,
    INTENT_HR_POLICY,
    INTENT_LEAVE_BALANCE,
    INTENT_LEAVE_REQUEST,
    INTENT_REQUEST_STATUS,
    INTENT_UNKNOWN,
)
from chat.services.leave_fsm import mark_submitted
from chat.services.session_snapshot import build_session_snapshot
from chat.services.session_turn_router import TurnKind, route_session_turn

FIXED_TODAY = dt.date(2026, 6, 10)


def _expense_wf(
    items: list[dict],
    *,
    stage: str = "collecting",
    pending_step: str = "",
    delete_verify_index: int | None = None,
) -> dict:
    block: dict = {
        "active": True,
        "stage": stage,
        "items": items,
    }
    if pending_step:
        block["pending_step"] = pending_step
    if delete_verify_index is not None:
        block["delete_verify_index"] = delete_verify_index
        block["pending_step"] = "delete_verify"
    return {"expense_request": block}


def _submitted_july10_leave_wf() -> dict:
    draft = {
        "start_date": "2026-07-10",
        "end_date": "2026-07-10",
        "leave_type": "casual",
        "reason": "ব্যক্তিগত কাজ",
        "days": 1.0,
    }
    return mark_submitted(
        {"draft": dict(draft)},
        draft=draft,
        submission_id="PHP-LEAVE-JUL10",
        idempotency_key="idem-jul10",
    )


def _leave_active_suspended_expense_wf(items: list[dict]) -> dict:
    return {
        "active_flow": "leave",
        "status": "active",
        "draft": {"start_date": "2026-07-11", "end_date": "2026-07-11"},
        "step": "reason",
        "review_pending": False,
        "suspended_expense": {
            "expense_request": {
                "active": True,
                "stage": "review",
                "items": items,
            }
        },
    }


def _route(message: str, wf: dict | None = None, **kwargs):
    state = wf or {}
    snap = build_session_snapshot(
        message,
        workflow_state=state,
        today=kwargs.pop("today", FIXED_TODAY),
        **kwargs,
    )
    return route_session_turn(snap, workflow_state=state), snap


def _assert_route(
    message: str,
    wf: dict | None,
    *,
    turn_kind: TurnKind,
    intent: str | None = None,
    reason_prefix: str | None = None,
    **kwargs,
):
    decision, _snap = _route(message, wf, **kwargs)
    assert decision.turn_kind == turn_kind, (
        f"expected {turn_kind}, got {decision.turn_kind} ({decision.reason})"
    )
    if intent is not None:
        assert decision.intent == intent, (
            f"expected intent {intent}, got {decision.intent}"
        )
    if reason_prefix:
        assert decision.reason.startswith(reason_prefix), decision.reason


# --- G01–G10 (scenario 40 critical paths) ---


def test_g01_bus_fare_correction():
    wf = _expense_wf(
        [
            {"amount": 80.0, "category": "Bus"},
            {"amount": 30.0, "category": "Bus"},
        ]
    )
    _assert_route(
        "বাস ভাড়া ৮০ টাকা না, ১২০ টাকা হবে।",
        wf,
        turn_kind=TurnKind.CORRECTION,
        intent=INTENT_EXPENSE_CLAIM,
        reason_prefix="P10",
    )


def test_g02_last_expense_ordinal_correction():
    wf = _expense_wf(
        [
            {"amount": 120.0, "category": "Bus"},
            {"amount": 30.0, "category": "Bus"},
            {"amount": 50.0, "category": "Snack"},
        ]
    )
    _assert_route(
        "শেষ expense টা ৫০ না, ৭০ টাকা হবে।",
        wf,
        turn_kind=TurnKind.CORRECTION,
        intent=INTENT_EXPENSE_CLAIM,
        reason_prefix="P10",
    )


def test_g03_duplicate_leave_july10():
    wf = _submitted_july10_leave_wf()
    _assert_route(
        "আবার ১০ জুলাই ছুটি চাই।",
        wf,
        turn_kind=TurnKind.DUPLICATE_LEAVE,
        intent=INTENT_LEAVE_REQUEST,
        reason_prefix="P20",
    )


def test_g04_expense_summary_while_leave_active_suspended_expense():
    wf = _leave_active_suspended_expense_wf(
        [
            {"amount": 120.0, "category": "Bus"},
            {"amount": 30.0, "category": "Bus"},
            {"amount": 70.0, "category": "Snack"},
        ]
    )
    _assert_route(
        "expense summary দেখাও।",
        wf,
        turn_kind=TurnKind.SUMMARY,
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        reason_prefix="P41",
    )


def test_g05_pre_submit_review_again_show():
    wf = _expense_wf(
        [
            {"amount": 30.0, "category": "Bus"},
            {"amount": 70.0, "category": "Snack"},
            {"amount": 180.0, "category": "Lunch"},
        ],
        stage="submit_confirm",
    )
    _assert_route(
        "expense টা আরেকবার দেখাও।",
        wf,
        turn_kind=TurnKind.PRE_SUBMIT_REVIEW,
        intent=INTENT_EXPENSE_CLAIM,
        reason_prefix="P40",
    )


def test_g06_reimbursement_policy():
    _assert_route(
        "reimbursement policy কী?",
        {},
        turn_kind=TurnKind.POLICY_QUERY,
        intent=INTENT_HR_POLICY,
        reason_prefix="P70",
    )


def test_g07_python_out_of_scope():
    decision, _ = _route("python কি?", {})
    assert decision.turn_kind == TurnKind.OUT_OF_SCOPE
    assert decision.intent == INTENT_UNKNOWN


def test_g08_leave_submit_command():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {"start_date": "2026-07-10"},
        "review_pending": True,
    }
    _assert_route(
        "leave submit করো।",
        wf,
        turn_kind=TurnKind.SUBMIT_COMMAND,
        intent=INTENT_LEAVE_REQUEST,
        reason_prefix="P03",
    )


def test_g09_new_leave_after_duplicate_prompt():
    from chat.services.leave.duplicate_choice import mark_duplicate_leave_choice_pending

    wf = mark_duplicate_leave_choice_pending(
        _submitted_july10_leave_wf(),
        target_start="2026-07-10",
    )
    _assert_route(
        "নতুন leave request খুলে দাও।",
        wf,
        turn_kind=TurnKind.SLOT_ANSWER,
        intent=INTENT_LEAVE_REQUEST,
        reason_prefix="P01",
    )


def test_g10_delete_confirm_yes():
    from chat.services.expense.expense_confirm import mark_expense_delete_verify

    block = {
        "active": True,
        "stage": "review",
        "items": [{"amount": 120.0, "category": "Bus"}],
    }
    mark_expense_delete_verify(block, 0)
    wf = {"expense_request": block}
    _assert_route(
        "হ্যাঁ",
        wf,
        turn_kind=TurnKind.DELETE_CONFIRM,
        intent=INTENT_EXPENSE_CLAIM,
        reason_prefix="P02",
    )


# --- G11–G15 (invariants — must NOT mis-route) ---


def test_g11_last_expense_not_done_collecting():
    wf = _expense_wf(
        [
            {"amount": 120.0, "category": "Bus"},
            {"amount": 30.0, "category": "Bus"},
            {"amount": 50.0, "category": "Snack"},
        ]
    )
    decision, _ = _route("শেষ expense টা ৫০ না, ৭০ টাকা হবে।", wf)
    assert decision.turn_kind != TurnKind.DONE_COLLECTING
    assert decision.reason != "P11_suspended_leave_correction"
    assert decision.turn_kind == TurnKind.CORRECTION


def test_g12_duplicate_leave_not_context_clarification():
    wf = _submitted_july10_leave_wf()
    decision, _ = _route("আবার ১০ জুলাই ছুটি চাই।", wf)
    assert decision.turn_kind != TurnKind.CONTEXT_CLARIFICATION
    assert decision.turn_kind == TurnKind.DUPLICATE_LEAVE


def test_g13_reimbursement_policy_not_expense_claim():
    decision, _ = _route("reimbursement policy", {})
    assert decision.intent != INTENT_EXPENSE_CLAIM
    assert decision.turn_kind == TurnKind.POLICY_QUERY


def test_g14_shesh_alone_is_done_not_correction():
    wf = _expense_wf([{"amount": 100.0, "category": "Lunch"}])
    decision, _ = _route("শেষ", wf)
    assert decision.turn_kind == TurnKind.DONE_COLLECTING
    assert decision.turn_kind != TurnKind.CORRECTION


def test_g15_expense_correction_during_leave_collecting_not_leave():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {"start_date": "2026-07-10"},
        "step": "reason",
        "review_pending": False,
        "suspended_expense": {
            "expense_request": {
                "active": True,
                "stage": "review",
                "items": [
                    {"amount": 80.0, "category": "Bus"},
                    {"amount": 30.0, "category": "Bus"},
                ],
            }
        },
    }
    decision, snap = _route("বাস ভাড়া ৮০ টাকা না, ১২০ টাকা হবে।", wf)
    assert snap.expense_domain_active
    assert decision.turn_kind == TurnKind.CORRECTION
    assert decision.intent == INTENT_EXPENSE_CLAIM
    assert decision.target_workflow == "expense"


def test_g16_lunch_during_from_to_pending_not_p10_correction():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "items": [],
            "pending_step": "from_to",
            "pending_line": {
                "amount": 100.0,
                "category": "Bus",
                "from_location": "",
                "to_location": "",
            },
        }
    }
    decision, _ = _route("lunch 150 taka", wf)
    assert decision.turn_kind != TurnKind.CORRECTION
    assert decision.turn_kind == TurnKind.SLOT_ANSWER
    assert decision.reason.startswith("P81")


def test_g17_new_leave_cold_start():
    _assert_route(
        "agami 15 august leave chai",
        {},
        turn_kind=TurnKind.NEW_LEAVE,
        intent=INTENT_LEAVE_REQUEST,
        reason_prefix="P50c",
    )


def test_g18_ordinal_amount_confirm_yes():
    from chat.services.expense.expense_confirm import mark_ordinal_amount_confirm

    block = {
        "active": True,
        "stage": "review",
        "items": [{"amount": 100.0, "category": "Bus"}],
    }
    mark_ordinal_amount_confirm(block, index=0, amount=200.0)
    _assert_route(
        "ha",
        {"expense_request": block},
        turn_kind=TurnKind.CONFIRM_YES,
        intent=INTENT_EXPENSE_CLAIM,
        reason_prefix="P02b",
    )


# --- Snapshot invariants ---


def test_expense_domain_active_includes_suspended_draft():
    wf = _leave_active_suspended_expense_wf([{"amount": 50.0, "category": "Lunch"}])
    snap = build_session_snapshot("test", workflow_state=wf, today=FIXED_TODAY)
    assert not snap.expense_active
    assert snap.has_suspended_expense
    assert snap.has_expense_draft
    assert snap.expense_domain_active


def test_skip_llm_intent_on_correction():
    wf = _expense_wf([{"amount": 80.0, "category": "Bus"}])
    decision, _ = _route("বাস ভাড়া ৮০ টাকা না, ১২০ টাকা হবে।", wf)
    assert decision.skip_llm_intent()


# --- Pre-router navigation rows N50–N55 (router-driven navigation) ---

from chat.services.expense.expense_fsm import (  # noqa: E402
    is_expense_in_progress,
    is_expense_paused,
)
from chat.services.leave_fsm import (  # noqa: E402
    is_leave_in_progress,
    is_leave_paused,
)
from chat.services.session_turn_router import plan_pre_router_navigation  # noqa: E402
from chat.services.workflow_suspend import (  # noqa: E402
    has_suspended_expense,
    has_suspended_leave,
)


def _paused_leave_wf() -> dict:
    return {
        "active_flow": "leave",
        "status": "paused",
        "draft": {"start_date": "2026-07-10"},
        "step": "reason",
        "review_pending": False,
    }


def _suspended_leave_only_wf() -> dict:
    return {
        "suspended_leave": {
            "draft": {"start_date": "2026-07-10"},
            "step": "reason",
            "status": "active",
            "review_pending": False,
        }
    }


def _plan(message: str, wf: dict, *, is_cancel: bool = False):
    return plan_pre_router_navigation(message, wf, is_cancel=is_cancel)


def test_n50_paused_leave_resumes_on_continuation():
    steps = _plan("ব্যক্তিগত কাজ", _paused_leave_wf())
    assert [s.rule for s in steps] == ["N50_resume_paused_leave"]
    assert steps[0].log_step == "leave_wizard_auto_resumed"
    assert is_leave_in_progress(steps[-1].state)
    assert not is_leave_paused(steps[-1].state)


def test_n50_policy_interrupt_keeps_leave_paused():
    steps = _plan("payslip কোথায় পাবো?", _paused_leave_wf())
    assert steps == []


def test_n51_resume_leave_while_expense_active_switches():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "items": [{"amount": 100.0, "category": "Lunch"}],
        },
        **_suspended_leave_only_wf(),
    }
    steps = _plan("back to leave", wf)
    assert steps[0].rule == "N51_switch_expense_to_suspended_leave"
    assert steps[0].log_step == "expense_suspended_resume_leave_nav"
    final = steps[-1].state
    assert is_leave_in_progress(final)
    assert not is_expense_in_progress(final)
    assert has_suspended_expense(final)


def test_n52b_paused_expense_resumes_on_show_request():
    wf = {
        "expense_request": {
            "active": True,
            "paused": True,
            "stage": "collecting",
            "items": [{"amount": 100.0, "category": "Lunch"}],
        }
    }
    steps = _plan("previous expense show koro", wf)
    assert [s.rule for s in steps] == ["N52b_resume_paused_expense"]
    assert steps[0].log_step == "expense_wizard_auto_resumed"
    assert is_expense_in_progress(steps[-1].state)
    assert not is_expense_paused(steps[-1].state)


def test_n53_suspended_leave_restored_when_nothing_active():
    steps = _plan("back to leave", _suspended_leave_only_wf())
    assert [s.rule for s in steps] == ["N53_restore_suspended_leave"]
    assert steps[0].log_step == "suspended_leave_restored"
    final = steps[-1].state
    assert is_leave_in_progress(final)
    assert not has_suspended_leave(final)


def test_n54_suspended_expense_restored_when_nothing_active():
    wf = {
        "suspended_expense": {
            "expense_request": {
                "active": True,
                "stage": "collecting",
                "items": [{"amount": 100.0, "category": "Lunch"}],
            }
        }
    }
    steps = _plan("previous expense show koro", wf)
    assert [s.rule for s in steps] == ["N54_restore_suspended_expense"]
    assert steps[0].log_step == "suspended_expense_restored"
    final = steps[-1].state
    assert is_expense_in_progress(final)
    assert not has_suspended_expense(final)


def test_n55a_active_leave_suspended_for_expense_query():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {"start_date": "2026-07-11"},
        "step": "reason",
        "review_pending": False,
    }
    steps = _plan("আজকের মোট খরচ কত?", wf)
    assert [s.rule for s in steps] == ["N55a_suspend_leave_for_expense_query"]
    assert steps[0].log_step == "leave_suspended_for_expense_query"
    final = steps[-1].state
    assert not is_leave_in_progress(final)
    assert has_suspended_leave(final)


def test_nav_cancel_blocks_all_navigation():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "items": [{"amount": 100.0, "category": "Lunch"}],
        },
        **_suspended_leave_only_wf(),
    }
    assert _plan("back to leave", wf, is_cancel=True) == []


def test_nav_rules_thread_state_n50_then_n55a():
    """Paused leave + expense query: resume first (N50), then park for the query (N55a)."""
    steps = _plan("আজকের মোট খরচ কত?", _paused_leave_wf())
    assert [s.rule for s in steps] == [
        "N50_resume_paused_leave",
        "N55a_suspend_leave_for_expense_query",
    ]
    final = steps[-1].state
    assert not is_leave_in_progress(final)
    assert has_suspended_leave(final)


def test_nav_noop_on_empty_state():
    assert _plan("hello", {}) == []


def _submitted_expense_wf(
    items: list[dict],
    *,
    ref: str = "EXP-2026-BCA0E0",
    stale_active_draft: bool = False,
) -> dict:
    wf: dict = {
        "expense_last_submission": {
            "reference_id": ref,
            "items": items,
            "incurred_date_iso": "2026-06-11",
        },
        "last_bot_action": {
            "action_type": "expense_submitted",
            "reference_id": ref,
            "items": items,
        },
    }
    if stale_active_draft:
        wf["expense_request"] = {
            "active": True,
            "stage": "review",
            "items": items,
            "incurred_date_iso": "2026-06-11",
        }
    return wf


def test_g22_post_submit_lunch_koro_blocked_by_p48():
    items = [
        {"category": "Lunch", "amount": 120},
        {"category": "Bus", "amount": 100},
    ]
    _assert_route(
        "lunch 200 taka koro",
        _submitted_expense_wf(items),
        turn_kind=TurnKind.META_QUESTION,
        intent=INTENT_EXPENSE_STATUS,
        reason_prefix="P48",
    )


def test_g23_post_submit_edit_question_meta_not_correction():
    items = [{"category": "Lunch", "amount": 120}]
    _assert_route(
        "can i edit expense after submit?",
        _submitted_expense_wf(items),
        turn_kind=TurnKind.META_QUESTION,
        intent=INTENT_EXPENSE_STATUS,
        reason_prefix="P43",
    )


def test_g24_n56_purges_stale_draft_before_router_snapshot():
    from chat.services.session_turn_router import plan_pre_router_navigation

    items = [{"category": "Lunch", "amount": 120}]
    wf = _submitted_expense_wf(items, stale_active_draft=True)
    steps = plan_pre_router_navigation("lunch 200 taka koro", wf, is_cancel=False)
    assert any(s.rule == "N56_purge_stale_expense_after_submit" for s in steps)
    final = steps[-1].state if steps else wf
    assert "expense_request" not in final
    snap = build_session_snapshot("lunch 200 taka koro", workflow_state=final)
    assert snap.expense_submission_locked
    assert not snap.expense_active
    assert not snap.expense_domain_active


def test_g32_what_is_life_during_leave_is_out_of_scope():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": "leave_type",
        "draft": {"start_date": "2026-07-10", "day_scope": "full"},
    }
    _assert_route(
        "what is life?",
        wf,
        turn_kind=TurnKind.OUT_OF_SCOPE,
        intent=INTENT_UNKNOWN,
        reason_prefix="P73",
    )


def test_g26_post_submit_leave_nav_not_balance():
    wf = mark_submitted(
        {},
        draft={
            "leave_type": "annual",
            "start_date": "2026-06-12",
            "reason": "family program",
        },
        submission_id="PHP-LEAVE-881199F981DA",
    )
    _assert_route(
        "leave e jao",
        wf,
        turn_kind=TurnKind.META_QUESTION,
        intent=INTENT_REQUEST_STATUS,
        reason_prefix="P49",
    )


def test_g25_n56_keeps_fresh_post_submit_draft_for_summary():
    """Post-submit new claim must not be purged when user asks for expense summary."""
    from chat.services.expense.session_action_memory import record_expense_lines_added
    from chat.services.session_turn_router import plan_pre_router_navigation

    items = [{"category": "Bus", "amount": 100, "from_location": "mirpur", "to_location": "motekheel"}]
    wf = _submitted_expense_wf(items, ref="EXP-2026-EFD18D")
    wf = record_expense_lines_added(
        {
            **wf,
            "expense_request": {
                "active": True,
                "stage": "collecting",
                "incurred_date_iso": "2026-06-11",
                "items": [{"category": "Lunch", "amount": 150}],
            },
        },
        new_items=[{"category": "Lunch", "amount": 150}],
        all_items=[{"category": "Lunch", "amount": 150}],
        incurred_date_iso="2026-06-11",
    )
    steps = plan_pre_router_navigation("okay ekhon expense summery ta daw", wf, is_cancel=False)
    assert not any(s.rule == "N56_purge_stale_expense_after_submit" for s in steps)
    final = steps[-1].state if steps else wf
    assert list((final.get("expense_request") or {}).get("items") or [])
    snap = build_session_snapshot("okay ekhon expense summery ta daw", workflow_state=final)
    assert snap.expense_submission_locked
    assert snap.expense_active
    assert snap.expense_domain_active


def _leave_collecting_wf(*, step: str = "leave_type") -> dict:
    return {
        "active_flow": "leave",
        "status": "active",
        "draft": {"start_date": "2026-08-15", "reason": "travel"},
        "step": step,
        "review_pending": False,
    }


def test_g19_balance_during_leave_collecting_not_slot():
    _assert_route(
        "amar leave koyta?",
        _leave_collecting_wf(step="reason"),
        turn_kind=TurnKind.BALANCE_QUERY,
        intent=INTENT_LEAVE_BALANCE,
        reason_prefix="P45",
    )


def test_g20_sick_balance_during_leave_type_not_slot():
    _assert_route(
        "amar sick leave koyta ache?",
        _leave_collecting_wf(step="leave_type"),
        turn_kind=TurnKind.BALANCE_QUERY,
        intent=INTENT_LEAVE_BALANCE,
        reason_prefix="P45",
    )


def test_g21_sick_token_alone_still_slot_answer():
    _assert_route(
        "sick leave",
        _leave_collecting_wf(step="leave_type"),
        turn_kind=TurnKind.SLOT_ANSWER,
        intent=INTENT_LEAVE_REQUEST,
        reason_prefix="P80",
    )
