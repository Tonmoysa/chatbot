"""Pending uncategorized amounts must survive new categorized expense lines."""

import datetime as dt

import pytest

from chat.services.expense.session_ledger import (
    build_session_expense_ledger,
    draft_line_rows_for_block,
)
from chat.services.expense.session_action_memory import format_meta_question_answer
from chat.services.expense_workflow import (
    _should_reset_pending_for_message,
    read_expense_block,
)
from chat.services.orchestrator import ChatOrchestrator

COMPANY_ID = "company-a"


def test_should_not_reset_pending_on_okay_prefix():
    msg = "okay..amar ajke 100 taka lunch e expense hoyeche"
    assert not _should_reset_pending_for_message(msg, pending_step="category")


@pytest.mark.django_db
def test_new_lunch_preserves_pending_200(monkeypatch):
    fixed = dt.date(2026, 6, 9)
    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.expense_incurred_date.date",
        "chat.services.decision_engine.date",
        "chat.services.orchestrator.date",
        "chat.services.expense_workflow.date",
    ):
        monkeypatch.setattr(mod, type("D", (dt.date,), {"today": classmethod(lambda cls: fixed)}))

    orch = ChatOrchestrator()
    emp = "pending-preserve-emp"
    start = orch.run_chat(
        company_id=COMPANY_ID,
        message="amar ajke 200 taka expense hoyeche",
        session_id=None,
        employee_id=emp,
        trace_id="pp-start",
    )
    sid = start["_session_id"]
    resp = orch.run_chat(
        company_id=COMPANY_ID,
        message="okay..amar ajke 100 taka lunch e expense hoyeche",
        session_id=sid,
        employee_id=emp,
        trace_id="pp-add",
    )
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    session.refresh_from_db()
    block = read_expense_block(session.workflow_state)
    rows = draft_line_rows_for_block(block)
    amounts = sorted(round(float(r.get("amount") or 0)) for r in rows)
    assert 100 in amounts
    assert 200 in amounts
    assert any(
        round(float(r.get("amount") or 0)) == 200 and not str(r.get("category") or "").strip()
        for r in rows
    )
    assert any(
        str(r.get("category") or "").lower() == "lunch"
        and round(float(r.get("amount") or 0)) == 100
        for r in rows
    )

    ledger = build_session_expense_ledger(
        session.workflow_state,
        crm_breakdown={},
        incurred_date_iso="2026-06-09",
    )
    pending = ledger.get("pending_draft") or {}
    pending_items = list(pending.get("items") or [])
    pending_amounts = sorted(round(float(r.get("amount") or 0)) for r in pending_items)
    assert 100 in pending_amounts
    assert 200 in pending_amounts

    meta = format_meta_question_answer(
        session.workflow_state,
        "age toh 200 taka chilo expense e seta kothai",
        lang="bn",
    )
    assert meta
    assert "200" in meta
    assert "100" in meta or "Lunch" in meta
    assert "মুছে যায়নি" in meta or "মুছে" in meta
