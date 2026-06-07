"""Session action memory — meta questions, vague add, expense history."""

import datetime as dt

import pytest

from chat.constants import INTENT_EXPENSE_DAY_SUMMARY, INTENT_EXPENSE_STATUS
from chat.services.expense.session_action_memory import (
    format_expense_history_message,
    format_meta_question_answer,
    is_vague_expense_add,
    record_expense_corrected,
    record_expense_lines_added,
    record_expense_submitted,
    record_vague_add_prompt,
    wants_expense_history_query,
    wants_expense_meta_question,
)
from chat.services.expense_workflow import process_expense_turn
from chat.services.intent_detector import IntentDetector
from chat.services.orchestrator import ChatOrchestrator

COMPANY_ID = "company-a"


def test_wants_expense_meta_question():
    assert wants_expense_meta_question("ki add korcho?")
    assert wants_expense_meta_question("submit kora hoise ki?")
    assert not wants_expense_meta_question("expense history daw")
    assert not wants_expense_meta_question("lunch 200 taka")


def test_wants_expense_history_query():
    assert wants_expense_history_query("expense history daw")
    assert wants_expense_history_query("ajker expense history")
    assert not wants_expense_history_query("ami koto expense add korchi")


def test_is_vague_expense_add():
    assert is_vague_expense_add("add koro")
    assert is_vague_expense_add("aro add koro")
    assert not is_vague_expense_add("lunch 200")
    assert not is_vague_expense_add("bus 50 mirpur to motijheel")


def test_format_meta_after_line_added():
    wf: dict = {}
    wf = record_expense_lines_added(
        wf,
        new_items=[{"category": "Lunch", "amount": 200}],
        all_items=[{"category": "Lunch", "amount": 200}],
        stage="collecting",
    )
    wf["expense_request"] = {
        "active": True,
        "stage": "collecting",
        "items": [{"category": "Lunch", "amount": 200}],
    }
    ans = format_meta_question_answer(wf, "ki add korcho?")
    assert ans
    assert "Lunch" in ans
    assert "200" in ans
    assert "submit" not in ans.lower() or "submit হয়নি" in ans or "not submitted" in ans.lower()


def test_format_meta_submit_probe_not_submitted():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "items": [{"category": "Bus", "amount": 50}],
        }
    }
    ans = format_meta_question_answer(wf, "submit kora hoise ki?")
    assert ans
    assert "submit" in ans.lower() or "জমা" in ans


def test_format_meta_submit_probe_submitted():
    wf = record_expense_submitted(
        {},
        items=[{"category": "Lunch", "amount": 100}],
        reference_id="EXP-99",
        incurred_date_iso="2026-06-07",
    )
    wf["expense_last_submission"] = {
        "reference_id": "EXP-99",
        "items": [{"category": "Lunch", "amount": 100}],
    }
    ans = format_meta_question_answer(wf, "submit kora hoise ki?")
    assert ans
    assert "EXP-99" in ans


def test_format_meta_vague_add_prompt():
    wf = record_vague_add_prompt({})
    ans = format_meta_question_answer(wf, "ki add korcho?")
    assert ans
    assert "add" in ans.lower()
    assert "line add করিনি" in ans or "did **not** add" in ans


def test_format_expense_history_includes_timeline():
    ledger = {
        "incurred_date_iso": "2026-06-07",
        "submitted_total": 100,
        "pending_total": 0,
        "combined_total": 100,
        "submitted_batches": [
            {
                "reference_id": "EXP-1",
                "items": [{"category": "Lunch", "amount": 100}],
            }
        ],
        "pending_items": [],
    }
    wf = record_expense_submitted(
        {},
        items=[{"category": "Lunch", "amount": 100}],
        reference_id="EXP-1",
        incurred_date_iso="2026-06-07",
    )
    msg = format_expense_history_message(ledger, wf)
    assert "Expense history" in msg
    assert "EXP-1" in msg
    assert "Session timeline" in msg or "Recent session actions" in msg


def test_vague_add_koro_prompts_clarification():
    pack = process_expense_turn(
        workflow_state={},
        message="add koro",
    )
    assert pack.get("question")
    assert "Category" in pack["question"] or "category" in pack["question"].lower()
    assert pack["workflow_state"].get("last_bot_action", {}).get("action_type") == (
        "expense_vague_add_prompt"
    )


def test_intent_detector_meta_override():
    det = IntentDetector()
    out = det.detect("ki add korcho?", trace_id="t-meta")
    assert out["intent"] == INTENT_EXPENSE_STATUS


@pytest.mark.django_db
def test_orchestrator_meta_during_expense_wizard(monkeypatch):
    fixed = dt.date(2026, 6, 7)
    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.expense_incurred_date.date",
        "chat.services.decision_engine.date",
        "chat.services.orchestrator.date",
        "chat.services.expense_workflow.date",
    ):
        monkeypatch.setattr(mod, type("D", (dt.date,), {"today": classmethod(lambda cls: fixed)}))

    orch = ChatOrchestrator()
    emp = "meta-wizard-emp"
    pack = orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 200 taka",
        session_id=None,
        employee_id=emp,
        trace_id="meta-w-start",
    )
    sid = pack["_session_id"]
    resp = orch.run_chat(
        company_id=COMPANY_ID,
        message="ki add korcho?",
        session_id=sid,
        employee_id=emp,
        trace_id="meta-w-ask",
    )
    last = resp["response"]["message"] or ""
    assert "Lunch" in last or "200" in last or "draft" in last.lower()


@pytest.mark.django_db
def test_orchestrator_expense_history_view(monkeypatch):
    fixed = dt.date(2026, 6, 7)
    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.expense_incurred_date.date",
        "chat.services.decision_engine.date",
        "chat.services.orchestrator.date",
        "chat.services.expense_workflow.date",
    ):
        monkeypatch.setattr(mod, type("D", (dt.date,), {"today": classmethod(lambda cls: fixed)}))

    orch = ChatOrchestrator()
    emp = "history-emp"
    pack = orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 100 taka",
        session_id=None,
        employee_id=emp,
        trace_id="hist-start",
    )
    sid = pack["_session_id"]
    out = orch.run_chat(
        company_id=COMPANY_ID,
        message="expense history daw",
        session_id=sid,
        employee_id=emp,
        trace_id="hist-query",
    )
    msg = out["response"]["message"] or ""
    assert "history" in msg.lower() or "History" in msg
    assert out.get("intent") == INTENT_EXPENSE_DAY_SUMMARY
