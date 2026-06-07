"""Phase 1: wizard guardrails, command registry, stale leave handling."""

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM, INTENT_EXPENSE_DAY_SUMMARY, INTENT_UNKNOWN
from chat.services.expense.expense_confirm import apply_corrections, looks_like_expense_correction
from chat.services.expense.routing import looks_like_expense_wizard_continuation
from chat.services.expense.wizard_commands import (
    wants_expense_submit_command,
    wants_expense_done_command,
)
from chat.services.expense_workflow import is_expense_in_progress, process_expense_turn
from chat.services.intent_detector import IntentDetector, _strong_expense_day_summary
from chat.services.leave_fsm import read_leave_state
from chat.services.leave_workflow import is_leave_in_progress
from chat.services.orchestrator import (
    ChatOrchestrator,
    _any_wizard_active,
    _detect_intent_during_expense_workflow,
)
from chat.services.workflow_priority import (
    expense_query_should_suspend_leave,
    leave_draft_looks_misrouted,
    should_clear_misrouted_leave,
)

COMPANY_ID = "company-a"


def test_strong_expense_day_summary_amai_hoise():
    assert _strong_expense_day_summary("amai ajke total cost koto hoise")
    assert _strong_expense_day_summary("amar ajke total cost koto hoyeche")


def test_expense_query_should_suspend_leave():
    assert expense_query_should_suspend_leave("amai ajke total cost koto hoise")
    assert expense_query_should_suspend_leave("expense summary for today")


def test_misrouted_leave_detection():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {"reason": "amai ajke total cost koto hoise", "start_date": "2026-06-07"},
        "step": "leave_payment",
    }
    assert leave_draft_looks_misrouted(wf)
    assert should_clear_misrouted_leave("sick leave daw submit", wf)


def test_joma_daw_is_submit_command():
    assert wants_expense_submit_command("joma daw")
    assert wants_expense_submit_command("submit koro")
    assert wants_expense_submit_command("sumit it")
    assert not wants_expense_submit_command("lunch 100")


def test_joma_daw_is_wizard_continuation_not_chitchat():
    assert looks_like_expense_wizard_continuation("joma daw")
    assert looks_like_expense_wizard_continuation("remove train")


def test_remove_train_correction():
    assert looks_like_expense_correction("remove train")
    items = [
        {"category": "Lunch", "amount": 100},
        {"category": "Train", "amount": 400},
    ]
    out, changed = apply_corrections(items, "remove train")
    assert changed
    assert len(out) == 1
    assert out[0]["category"] == "Lunch"


def test_expense_workflow_gate_joma_daw():
    out = _detect_intent_during_expense_workflow(
        "joma daw",
        {"expense_request": {"active": True, "stage": "collecting", "items": []}},
        balance_probe=False,
    )
    assert out["intent"] == INTENT_EXPENSE_CLAIM
    assert "command" in out["source"]


@pytest.mark.django_db
def test_expense_total_query_not_trapped_in_leave_wizard(monkeypatch):
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    emp = "p1-leave-trap"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="kalke chuti lagbe",
        session_id=None,
        employee_id=emp,
        trace_id="p1-l1",
    )
    sid = r1["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    draft = read_leave_state(session.workflow_state).get("draft") or {}
    draft["reason"] = "amai ajke total cost koto hoise"
    wf = dict(session.workflow_state or {})
    wf["draft"] = draft
    session.workflow_state = wf
    session.save(update_fields=["workflow_state"])

    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="amai ajke total cost koto hoise",
        session_id=sid,
        employee_id=emp,
        trace_id="p1-l2",
    )
    assert r2["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    msg = r2["response"]["message"] or ""
    assert "ছুটির তারিখ" not in msg
    assert "Paid নাকি unpaid" not in msg


@pytest.mark.django_db
def test_joma_daw_during_expense_never_calls_conversational_llm(monkeypatch):
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )

    def _boom(**_k):
        raise AssertionError("conversational LLM must not run during expense wizard")

    monkeypatch.setattr(
        "chat.services.orchestrator.conversational_reply",
        _boom,
    )
    orch = ChatOrchestrator()
    emp = "p1-joma"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 100, bus 200",
        session_id=None,
        employee_id=emp,
        trace_id="p1-j1",
    )
    sid = r1["_session_id"]
    assert is_expense_in_progress(
        orch.memory.get_or_create_session(
            company_id=COMPANY_ID, employee_id=emp, session_id=sid
        ).workflow_state
    )

    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="joma daw",
        session_id=sid,
        employee_id=emp,
        trace_id="p1-j2",
    )
    msg = (r2["response"]["message"] or "").lower()
    assert "lunch" not in msg or "means" not in msg
    assert r2["intent"] == INTENT_EXPENSE_CLAIM


def test_any_wizard_active_helper():
    assert _any_wizard_active({"expense_request": {"active": True}})
    assert not _any_wizard_active({})
