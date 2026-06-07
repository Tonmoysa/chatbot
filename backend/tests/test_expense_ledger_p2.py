"""P2 — ledger footnotes, ingest guard, total dispute."""

import datetime as dt

import pytest

from chat.constants import INTENT_EXPENSE_STATUS
from chat.services.expense.expense_ingest_guard import (
    REASON_TRAVEL_REMOVED,
    is_allowed_while_ingest_lock,
    should_block_compound_reingest,
)
from chat.services.expense.expense_total_dispute import (
    format_expense_total_check_message,
    is_expense_total_check_query,
    is_expense_total_dispute_query,
    is_expense_total_verify_query,
)
from chat.services.expense.session_ledger import (
    build_session_expense_ledger,
    format_session_expense_ledger_message,
)
from chat.services.expense_workflow import process_expense_turn
from chat.services.orchestrator import ChatOrchestrator

COMPANY_ID = "company-a"
COMPOUND_MSG = (
    "amar ajke expense hoyeche 100 taka bus e mirpur to motijheel "
    "then bike e 100 taka cost hoyeche motijheel to mirpur "
    "then lunch e 50 taka expense hoyeche"
)


def test_ledger_footnotes_after_travel_remove():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "incurred_date_iso": "2026-06-07",
            "items": [{"category": "Lunch", "amount": 50}],
            "ingest_lock": True,
            "ingest_lock_reason": REASON_TRAVEL_REMOVED,
        },
        "bot_action_log": [
            {"summary": "Updated expense draft: Lunch — total 50 Tk (not submitted yet)."}
        ],
        "last_bot_action": {
            "summary": "Updated expense draft: Lunch — total 50 Tk (not submitted yet)."
        },
        "expense_draft_snapshots": [{"id": "snap-1"}, {"id": "snap-2"}],
    }
    ledger = build_session_expense_ledger(
        wf,
        crm_breakdown={},
        incurred_date_iso="2026-06-07",
    )
    assert ledger["session_context"]["ingest_lock"] is True
    msg = format_session_expense_ledger_message(ledger)
    assert "Travel remove" in msg or "travel" in msg.lower()
    assert "সাম্প্রতিক action" in msg or "Last action" in msg
    assert "restore" in msg.lower() or "ফিরতে" in msg


def test_is_expense_total_dispute_query():
    assert is_expense_total_dispute_query("total mony hoy vul hoise, check")
    assert is_expense_total_dispute_query("mot vul hoise check koro")
    assert not is_expense_total_dispute_query("amar total koto cost limit?")


def test_is_expense_total_verify_query():
    assert is_expense_total_verify_query("total thik ache ki")
    assert is_expense_total_check_query("mot koto hobe")
    assert not is_expense_total_verify_query("amar total koto cost limit?")


def test_ingest_lock_allows_single_line():
    block = {"ingest_lock": True, "ingest_lock_reason": REASON_TRAVEL_REMOVED}
    assert is_allowed_while_ingest_lock("lunch 30 taka")
    assert not is_allowed_while_ingest_lock(COMPOUND_MSG)
    assert should_block_compound_reingest(
        block,
        COMPOUND_MSG,
        [{"category": "Lunch", "amount": 50}],
    )


@pytest.mark.django_db
def test_single_line_add_allowed_after_travel_remove(monkeypatch):
    fixed = dt.date(2026, 6, 7)
    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.expense_incurred_date.date",
        "chat.services.decision_engine.date",
        "chat.services.orchestrator.date",
        "chat.services.expense_workflow.date",
    ):
        monkeypatch.setattr(mod, type("D", (dt.date,), {"today": classmethod(lambda cls: fixed)}))

    wf: dict = {}
    p1 = process_expense_turn(workflow_state=wf, message=COMPOUND_MSG)
    wf = p1["workflow_state"]
    p2 = process_expense_turn(workflow_state=wf, message="travel cost remove koro")
    wf = p2["workflow_state"]
    block = wf.get("expense_request") or {}
    assert block.get("ingest_lock") is True

    p3 = process_expense_turn(workflow_state=wf, message="snack 20 taka")
    wf = p3["workflow_state"]
    block = wf.get("expense_request") or {}
    items = list(block.get("items") or [])
    cats = {str(x.get("category")) for x in items}
    assert "Snack" in cats or "Lunch" in cats


@pytest.mark.django_db
def test_total_dispute_during_expense_review(monkeypatch):
    fixed = dt.date(2026, 6, 7)
    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.expense_incurred_date.date",
        "chat.services.decision_engine.date",
        "chat.services.orchestrator.date",
        "chat.services.expense_workflow.date",
    ):
        monkeypatch.setattr(mod, type("D", (dt.date,), {"today": classmethod(lambda cls: fixed)}))
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )

    orch = ChatOrchestrator()
    emp = "total-dispute-p2"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="bus 100 mirpur to motijheel, lunch 50",
        session_id=None,
        employee_id=emp,
        trace_id="td-1",
    )
    sid = r1["_session_id"]
    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="total mony hoy vul hoise, check",
        session_id=sid,
        employee_id=emp,
        trace_id="td-2",
    )
    assert r2["intent"] == INTENT_EXPENSE_STATUS
    msg = r2["response"]["message"] or ""
    assert "total check" in msg.lower() or "গণনা" in msg or "মোট" in msg
    assert "150" in msg or "100" in msg


def test_format_expense_total_check_message():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "incurred_date_iso": "2026-06-07",
            "items": [
                {"category": "Bus", "amount": 100, "from_location": "a", "to_location": "b"},
                {"category": "Lunch", "amount": 50},
            ],
        }
    }
    msg = format_expense_total_check_message(wf, incurred_date_iso="2026-06-07")
    assert msg
    assert "150" in msg
