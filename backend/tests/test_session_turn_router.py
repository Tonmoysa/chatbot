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
    INTENT_HR_POLICY,
    INTENT_LEAVE_REQUEST,
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
    snap = build_session_snapshot(
        message,
        workflow_state=wf or {},
        today=kwargs.pop("today", FIXED_TODAY),
        **kwargs,
    )
    return route_session_turn(snap), snap


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
