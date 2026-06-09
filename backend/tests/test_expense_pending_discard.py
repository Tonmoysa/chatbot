"""Discard incomplete pending expense lines with yes/no confirmation."""

import datetime as dt

import pytest

from chat.services.expense.pending_discard import (
    has_pending_discard_confirm,
    try_handle_pending_discard_turn,
    wants_discard_incomplete_pending,
)
from chat.services.expense.session_ledger import draft_line_rows_for_block
from chat.services.expense_workflow import (
    format_expense_resume_message,
    read_expense_block,
)
from chat.services.orchestrator import ChatOrchestrator

COMPANY_ID = "company-a"


def _disable_llm(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )


@pytest.mark.parametrize(
    "message",
    [
        "vule diyechi",
        "ei expense baad eta lagbe nah",
        "200 taka baad",
        "eta baad dite chai",
    ],
)
def test_wants_discard_incomplete_pending(message):
    assert wants_discard_incomplete_pending(message)


def test_discard_unit_flow():
    wf: dict = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "incurred_date_iso": "2026-06-09",
            "items": [{"category": "Lunch", "amount": 100}],
            "pending_line": {"amount": 200, "category": ""},
            "pending_step": "category",
        }
    }
    block = wf["expense_request"]
    items = list(block["items"])

    ask = try_handle_pending_discard_turn(
        wf, block, items, "vule diyechi", inc_iso="2026-06-09", lang="bn"
    )
    assert ask is not None
    assert has_pending_discard_confirm(block)

    cancel = try_handle_pending_discard_turn(
        wf, block, items, "no", inc_iso="2026-06-09", lang="bn"
    )
    assert cancel is not None
    assert not has_pending_discard_confirm(block)
    assert any(round(float(r.get("amount") or 0)) == 200 for r in draft_line_rows_for_block(block))

    try_handle_pending_discard_turn(
        wf, block, items, "200 taka baad", inc_iso="2026-06-09", lang="bn"
    )
    assert has_pending_discard_confirm(block)

    done = try_handle_pending_discard_turn(
        wf, block, items, "yes", inc_iso="2026-06-09", lang="bn"
    )
    assert done is not None
    assert not has_pending_discard_confirm(block)
    assert not any(round(float(r.get("amount") or 0)) == 200 for r in draft_line_rows_for_block(block))
    assert (done.get("question") or "").find("সরিয়ে") >= 0 or "remove" in (done.get("question") or "").lower()


def _patch_dates(monkeypatch, fixed: dt.date):
    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.expense_incurred_date.date",
        "chat.services.decision_engine.date",
        "chat.services.orchestrator.date",
        "chat.services.expense_workflow.date",
    ):
        monkeypatch.setattr(mod, type("D", (dt.date,), {"today": classmethod(lambda cls: fixed)}))


@pytest.mark.django_db
def test_discard_incomplete_pending_confirm_flow(monkeypatch):
    _disable_llm(monkeypatch)
    fixed = dt.date(2026, 6, 9)
    _patch_dates(monkeypatch, fixed)
    orch = ChatOrchestrator()
    emp = "discard-flow-emp"
    start = orch.run_chat(
        company_id=COMPANY_ID,
        message="amar ajke 200 taka expense hoyeche",
        session_id=None,
        employee_id=emp,
        trace_id="dc-start",
    )
    sid = start["_session_id"]

    ask = orch.run_chat(
        company_id=COMPANY_ID,
        message="vule diyechi ei expense ta",
        session_id=sid,
        employee_id=emp,
        trace_id="dc-ask",
    )
    msg = ask["response"]["message"] or ""
    assert "delete" in msg.lower() or "মুছে" in msg or "remove" in msg.lower()
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    session.refresh_from_db()
    block = read_expense_block(session.workflow_state)
    assert has_pending_discard_confirm(block)

    kept = orch.run_chat(
        company_id=COMPANY_ID,
        message="no",
        session_id=sid,
        employee_id=emp,
        trace_id="dc-no",
    )
    kept_msg = kept["response"]["message"] or ""
    assert "rakha" in kept_msg.lower() or "রাখা" in kept_msg or "kept" in kept_msg.lower()
    session.refresh_from_db()
    block = read_expense_block(session.workflow_state)
    assert not has_pending_discard_confirm(block)
    assert any(round(float(r.get("amount") or 0)) == 200 for r in draft_line_rows_for_block(block))

    ask2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="200 taka baad",
        session_id=sid,
        employee_id=emp,
        trace_id="dc-ask2",
    )
    assert "delete" in (ask2["response"]["message"] or "").lower() or "মুছে" in ask2["response"]["message"] or ""

    done = orch.run_chat(
        company_id=COMPANY_ID,
        message="yes",
        session_id=sid,
        employee_id=emp,
        trace_id="dc-yes",
    )
    session.refresh_from_db()
    block = read_expense_block(session.workflow_state)
    assert not has_pending_discard_confirm(block)
    assert not any(round(float(r.get("amount") or 0)) == 200 for r in draft_line_rows_for_block(block))


@pytest.mark.django_db
def test_resume_shows_full_draft_with_incomplete_line(monkeypatch):
    _disable_llm(monkeypatch)
    fixed = dt.date(2026, 6, 9)
    _patch_dates(monkeypatch, fixed)

    orch = ChatOrchestrator()
    emp = "resume-draft-emp"
    start = orch.run_chat(
        company_id=COMPANY_ID,
        message="amar ajke 200 taka expense hoyeche",
        session_id=None,
        employee_id=emp,
        trace_id="rs-1",
    )
    sid = start["_session_id"]
    orch.run_chat(
        company_id=COMPANY_ID,
        message="okay..amar ajke 100 taka lunch e expense hoyeche",
        session_id=sid,
        employee_id=emp,
        trace_id="rs-2",
    )
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    session.refresh_from_db()
    resume = format_expense_resume_message(
        session.workflow_state,
        user_message="expense e back asho",
    )
    assert resume
    assert "200" in resume
    assert "100" in resume or "Lunch" in resume
    assert "draft" in resume.lower() or "Draft" in resume or "প্রথম" in resume or "category" in resume.lower()
