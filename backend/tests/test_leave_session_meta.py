"""Leave session meta: typo slot, status/details/summary after submit."""

from __future__ import annotations

import datetime as dt

import pytest

from chat.constants import (
    INTENT_EXPENSE_STATUS,
    INTENT_LEAVE_REQUEST,
    INTENT_REQUEST_STATUS,
)
from chat.services.leave.normalization import parse_wizard_leave_type_answer
from chat.services.leave.reason_value import (
    extract_reason_value,
    is_boilerplate_leave_reason,
    looks_like_wizard_slot_label,
)
from chat.services.leave.session_action_memory import (
    format_leave_meta_answer,
    record_leave_submitted,
    wants_leave_meta_question,
)
from chat.services.leave_meta_queries import (
    wants_leave_session_summary,
    wants_leave_submission_status,
    wants_submitted_leave_details,
)
from chat.services.leave_fsm import STATUS_ACTIVE, apply_leave_state, mark_submitted
from chat.services.leave_workflow import process_leave_turn
from chat.services.leave_fsm import read_leave_state
from chat.services.leave_slots import SLOT_LEAVE_TYPE, SLOT_REASON, get_missing_slots
from chat.services.session_snapshot import build_session_snapshot
from chat.services.session_turn_router import route_session_turn, _leave_wizard_token
from chat.services.workflow_navigation import is_leave_application_message


def test_anual_leave_typo_parsed_as_annual() -> None:
    assert parse_wizard_leave_type_answer("anual leave") == "annual"
    assert parse_wizard_leave_type_answer("anual") == "annual"
    assert _leave_wizard_token("anual leave")
    assert looks_like_wizard_slot_label("anual leave")
    assert is_boilerplate_leave_reason("anual leave")
    assert extract_reason_value("anual leave") is None


def test_leave_submit_status_answer_not_submitted() -> None:
    wf = apply_leave_state(
        {},
        draft={"start_date": "2026-07-10"},
        step="leave_type",
        status=STATUS_ACTIVE,
    )
    msg = format_leave_meta_answer(wf, "amar leave ki submit hoyeche?")
    assert "এখনো জমা হয়নি" in msg or "জমা হয়নি" in msg
    assert "Full day" not in msg


def test_status_question_not_leave_application() -> None:
    msg = "ami ki kono leave apply korechi?"
    assert wants_leave_submission_status(msg)
    assert wants_leave_meta_question(msg)
    assert not is_leave_application_message(msg)


def test_submitted_details_predicate() -> None:
    msg = "sei leave er information daw"
    assert wants_submitted_leave_details(msg)
    assert wants_leave_meta_question(msg)
    assert not is_leave_application_message(msg)


def test_leave_summery_ta_daw_is_summary_not_balance() -> None:
    msg = "leave summery ta daw"
    assert wants_leave_session_summary(msg)
    assert wants_leave_meta_question(msg)


def test_anual_leave_wizard_turn_not_reason(monkeypatch) -> None:
    from chat.services.leave.entity_pipeline import LeaveEntityPipeline
    from chat.constants import INTENT_LEAVE_REQUEST

    fixed = dt.date(2026, 6, 11)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)

    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": "leave_type",
        "draft": {"start_date": "2026-08-15", "end_date": "2026-08-15"},
    }
    pack = process_leave_turn(
        workflow_state=wf,
        message="anual leave",
        entities={},
        company_id="default",
        trace_id="anual-slot",
    )
    draft = dict(read_leave_state(pack["workflow_state"]).get("draft") or {})
    assert draft.get("leave_type") == "annual"
    assert draft.get("reason") not in ("anual leave", "annual leave")
    assert not is_boilerplate_leave_reason(str(draft.get("reason") or "")) or not draft.get(
        "reason"
    )


@pytest.mark.django_db
def test_post_submit_meta_answers(monkeypatch) -> None:
    draft = {
        "leave_type": "sick",
        "day_scope": "full",
        "start_date": "2026-08-15",
        "end_date": "2026-08-15",
        "reason": "অসুস্থতা",
    }
    wf = mark_submitted(
        {},
        draft=draft,
        submission_id="PHP-LEAVE-TEST123",
    )
    wf = record_leave_submitted(
        wf,
        submission_id="PHP-LEAVE-TEST123",
        draft=draft,
    )

    status_msg = format_leave_meta_answer(wf, "amar kono leave submit hoyeche?")
    assert "জমা হয়েছে" in status_msg
    assert "PHP-LEAVE-TEST123" in status_msg

    details_msg = format_leave_meta_answer(wf, "sei leave er information daw")
    assert "PHP-LEAVE-TEST123" in details_msg
    assert "2026-08-15" in details_msg
    assert "leave information" not in details_msg.lower()

    summary_msg = format_leave_meta_answer(wf, "leave summery ta daw")
    assert "সারাংশ" in summary_msg or "PHP-LEAVE-TEST123" in summary_msg


def test_router_p43_leave_submit_status_not_submit_command() -> None:
    wf = apply_leave_state(
        {},
        draft={"start_date": "2026-07-10"},
        step="leave_type",
        status=STATUS_ACTIVE,
    )
    snap = build_session_snapshot(
        "amar leave ki submit hoyeche?",
        workflow_state=wf,
    )
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason == "P43_leave_meta"
    assert decision.intent == INTENT_REQUEST_STATUS


def test_router_p43_expense_submit_status_dual_session() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": "leave_type",
        "draft": {"start_date": "2026-07-10"},
        "expense_request": {
            "active": True,
            "stage": "review",
            "items": [{"category": "Lunch", "amount": 200}],
        },
    }
    snap = build_session_snapshot("amar expense ki submit hoyeche?", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason == "P43_expense_meta"
    assert decision.intent == INTENT_EXPENSE_STATUS


def test_router_p54_dual_submit_disambiguation() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": "leave_type",
        "draft": {"start_date": "2026-07-10"},
        "expense_request": {
            "active": True,
            "stage": "review",
            "items": [{"category": "Lunch", "amount": 200}],
        },
    }
    snap = build_session_snapshot("okay submit koro", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason == "P54_dual_workflow_submit"
    assert "clarification_prompt" in (decision.flags or {})


def test_router_p43_leave_meta_after_submit() -> None:
    wf = mark_submitted(
        {},
        draft={"start_date": "2026-08-15", "leave_type": "sick"},
        submission_id="PHP-LEAVE-RTR",
    )
    snap = build_session_snapshot(
        "ami ki kono leave apply korechi?",
        workflow_state=wf,
    )
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason == "P43_leave_meta"
    assert decision.intent == INTENT_REQUEST_STATUS


def test_okay_submit_koro_dual_session_asks_which_workflow() -> None:
    from chat.services.leave_confirm import wants_leave_submit_command

    msg = "okay submit koro"
    assert wants_leave_submit_command(msg)

    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {
            "leave_type": "annual",
            "day_scope": "full",
            "start_date": "2026-08-15",
            "end_date": "2026-08-15",
            "reason": "family program",
        },
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "items": [{"amount": 50.0, "category": "Lunch"}],
        },
    }
    snap = build_session_snapshot(msg, workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason == "P54_dual_workflow_submit"
    from chat.constants import INTENT_UNKNOWN

    assert decision.intent == INTENT_UNKNOWN


def test_okay_submit_koro_one_shot_review_or_submit(monkeypatch) -> None:
    """Complete sick-leave draft + okay submit koro must not open expense wizard."""
    from chat.services.leave_workflow import process_leave_turn
    from chat.services.leave_fsm import is_awaiting_leave_confirmation

    fixed = dt.date(2026, 6, 11)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)

    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {
            "leave_type": "sick",
            "day_scope": "full",
            "start_date": "2026-08-15",
            "end_date": "2026-08-15",
            "reason": "onek osusto",
        },
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "items": [{"amount": 50.0, "category": "Lunch"}],
        },
    }
    pack = process_leave_turn(
        workflow_state=wf,
        message="okay submit koro",
        entities={},
        company_id="default",
        trace_id="one-shot-submit",
    )
    question = str(pack.get("question") or "").lower()
    assert "expense" not in question
    assert "lunch" not in question
    assert "add more lines" not in question
    assert is_awaiting_leave_confirmation(pack["workflow_state"]) or pack.get(
        "confirmed_submit"
    )


def test_travel_reason_keeps_annual_leave_type() -> None:
    from chat.services.leave.normalization import normalize_leave_draft

    draft = {
        "leave_type": "annual",
        "day_scope": "full",
        "start_date": "2026-08-15",
        "end_date": "2026-08-15",
        "reason": "travel",
        "_last_user_message": "travel",
    }
    normalize_leave_draft(draft)
    assert draft.get("leave_type") == "annual"


def test_kalke_chuti_lagbe_does_not_invent_family_program(monkeypatch) -> None:
    """LLM must not fabricate reason when user only states they need leave."""
    fixed = dt.date(2026, 6, 11)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)

    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {},
    }
    invented = {"reason": "family program", "start_date": "2026-06-12"}
    pack = process_leave_turn(
        workflow_state=wf,
        message="amar kalke chuti lagbe",
        entities=invented,
        company_id="default",
        trace_id="no-invent-reason",
    )
    draft = dict(read_leave_state(pack["workflow_state"]).get("draft") or {})
    assert draft.get("reason") not in ("family program", "family")
    assert SLOT_REASON in get_missing_slots(draft)
    q = pack.get("question") or ""
    assert "family program" not in q.lower()


def test_router_p49_post_submit_leave_nav_not_balance() -> None:
    wf = mark_submitted(
        {},
        draft={
            "leave_type": "annual",
            "day_scope": "full",
            "start_date": "2026-06-12",
            "end_date": "2026-06-12",
            "reason": "family program",
        },
        submission_id="PHP-LEAVE-881199F981DA",
    )
    snap = build_session_snapshot("leave e jao", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason == "P49_post_submit_leave_nav"
    assert decision.intent == INTENT_REQUEST_STATUS
    from chat.services.leave_balance_intent import is_leave_balance_query

    assert not is_leave_balance_query("leave e jao")


def test_router_anual_token_during_collecting() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": "leave_type",
        "draft": {"start_date": "2026-08-15"},
    }
    snap = build_session_snapshot("anual leave", workflow_state=wf)
    decision = route_session_turn(snap, workflow_state=wf)
    assert decision.reason == "P80_leave_slot_token"
    assert decision.intent == INTENT_LEAVE_REQUEST


@pytest.mark.django_db
def test_g32_what_is_life_during_leave_no_wizard_append(monkeypatch) -> None:
    """Out-of-scope side question must not append leave wizard prompt or action chips."""
    from chat.services.orchestrator import ChatOrchestrator
    from chat.services.leave_workflow import is_leave_in_progress

    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.message_polish_llm.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.message_polish.polish_outbound_message",
        lambda body, **_k: body,
    )

    orch = ChatOrchestrator()
    emp = "g32-leave-oos"
    r1 = orch.run_chat(
        company_id="company-a",
        message="10 july leave chai",
        session_id=None,
        employee_id=emp,
        trace_id="g32-1",
    )
    sid = r1["_session_id"]
    wf = orch.memory.get_or_create_session(
        company_id="company-a", employee_id=emp, session_id=sid
    ).workflow_state
    assert is_leave_in_progress(wf)

    r2 = orch.run_chat(
        company_id="company-a",
        message="what is life?",
        session_id=sid,
        employee_id=emp,
        trace_id="g32-2",
    )
    body = (r2.get("response") or {}).get("message") or ""
    actions = (r2.get("response") or {}).get("actions") or []
    rules = list((r2.get("decision") or {}).get("rules_applied") or [])

    assert "OUT_OF_SCOPE_GENERAL" in rules
    assert r2["decision"]["outcome"] == "INFORMATIONAL"
    assert "Select Leave" not in body
    assert "ছুটির তারিখ" not in body
    assert "Full day" not in body
    assert not actions
    assert is_leave_in_progress(
        orch.memory.get_or_create_session(
            company_id="company-a", employee_id=emp, session_id=sid
        ).workflow_state
    )
