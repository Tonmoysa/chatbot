"""Expense draft snapshots — restore menu + compound re-ingest guard."""

import datetime as dt

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM
from chat.services.expense.expense_confirm import (
    looks_like_compound_expense_claim,
    looks_like_duplicate_expense_reentry,
)
from chat.services.expense.expense_draft_snapshots import (
    is_awaiting_restore_selection,
    items_fingerprint,
    parse_restore_selection,
    push_expense_snapshot,
    read_snapshots,
    wants_restore_expense_version,
)
from chat.services.expense_workflow import process_expense_turn
from chat.services.orchestrator import ChatOrchestrator, _detect_intent_during_expense_workflow

COMPANY_ID = "company-a"

COMPOUND_MSG = (
    "amar ajke expense hoyeche 100 taka bus e mirpur to motijheel "
    "then bike e 100 taka cost hoyeche motijheel to mirpur "
    "then lunch e 50 taka expense hoyeche"
)


@pytest.mark.parametrize(
    "message",
    [
        "ager expense information thik chilo otatei back koro",
        "ager thik chilo restore koro",
        "previous version back koro",
        "আগেরটা ঠিক ছিল",
    ],
)
def test_wants_restore_expense_version(message):
    assert wants_restore_expense_version(message)


def test_wants_restore_not_resume():
    from chat.services.expense_workflow import wants_resume_or_show_expense

    assert wants_restore_expense_version("ager thik chilo back koro")
    assert not wants_resume_or_show_expense("ager thik chilo back koro")


def test_push_snapshot_dedupes_same_fingerprint():
    items = [{"category": "Lunch", "amount": 50}]
    wf = push_expense_snapshot(
        {},
        items=items,
        stage="review",
        action_type="initial_review",
    )
    wf2 = push_expense_snapshot(
        wf,
        items=items,
        stage="review",
        action_type="before_correction",
    )
    assert len(read_snapshots(wf2)) == 1


def test_parse_restore_selection_number():
    snaps = [
        {"label": "Original — Bus 100", "fingerprint": "a"},
        {"label": "After travel remove", "fingerprint": "b"},
    ]
    assert parse_restore_selection("1", snaps) == 1
    assert parse_restore_selection("cancel", snaps) == -1
    assert parse_restore_selection("original", snaps) == 1


def test_restore_intent_during_expense_gate():
    wf = {"expense_request": {"active": True, "stage": "review", "items": []}}
    out = _detect_intent_during_expense_workflow(
        "ager thik chilo back koro",
        wf,
        balance_probe=False,
    )
    assert out["intent"] == INTENT_EXPENSE_CLAIM
    assert "restore" in out["source"]


def test_compound_duplicate_with_single_lunch_draft():
    items = [{"category": "Lunch", "amount": 50}]
    assert looks_like_compound_expense_claim(COMPOUND_MSG)
    assert looks_like_duplicate_expense_reentry(COMPOUND_MSG, items)


def _run_turn(wf, message, **kwargs):
    return process_expense_turn(workflow_state=wf, message=message, **kwargs)


@pytest.mark.django_db
def test_restore_menu_after_travel_remove(monkeypatch):
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
    p1 = _run_turn(wf, COMPOUND_MSG)
    wf = p1["workflow_state"]
    items = list((wf.get("expense_request") or {}).get("items") or [])
    assert len(items) >= 3

    p2 = _run_turn(
        wf,
        "bus theke 50 taka baad diye bike e 50 taka add koro",
    )
    wf = p2["workflow_state"]

    p3 = _run_turn(wf, "travel cost remove koro")
    wf = p3["workflow_state"]
    items_after = list((wf.get("expense_request") or {}).get("items") or [])
    assert len(items_after) == 1
    assert items_after[0]["category"] == "Lunch"
    assert read_snapshots(wf)

    p4 = _run_turn(wf, "ager expense information thik chilo otatei back koro")
    q4 = p4.get("question") or ""
    assert "restore" in q4.lower() or "ফিরে" in q4 or "version" in q4.lower()
    block = wf.get("expense_request") or {}
    assert is_awaiting_restore_selection(p4["workflow_state"].get("expense_request") or {})

    wf = p4["workflow_state"]
    p5 = _run_turn(wf, "1")
    wf = p5["workflow_state"]
    restored = list((wf.get("expense_request") or {}).get("items") or [])
    cats = {str(x.get("category")) for x in restored}
    assert "Lunch" in cats
    assert len(restored) >= 2
    assert "Bus" in cats or "Bike" in cats


@pytest.mark.django_db
def test_compound_reingest_blocked_after_travel_remove(monkeypatch):
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
    p1 = _run_turn(wf, COMPOUND_MSG)
    wf = p1["workflow_state"]
    p2 = _run_turn(wf, "travel cost remove koro")
    wf = p2["workflow_state"]
    block = wf.get("expense_request") or {}
    assert block.get("ingest_lock") is True

    p3 = _run_turn(wf, COMPOUND_MSG)
    q = p3.get("question") or ""
    assert "duplicate" in q.lower() or "add kori nai" in q or "যোগ করিনি" in q
    items = list((p3["workflow_state"].get("expense_request") or {}).get("items") or [])
    assert len(items) == 1


@pytest.mark.django_db
def test_orchestrator_restore_flow_end_to_end(monkeypatch):
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
    emp = "snap-restore-e2e"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message=COMPOUND_MSG,
        session_id=None,
        employee_id=emp,
        trace_id="snap-1",
    )
    sid = r1["_session_id"]
    orch.run_chat(
        company_id=COMPANY_ID,
        message="travel cost remove koro",
        session_id=sid,
        employee_id=emp,
        trace_id="snap-2",
    )
    r3 = orch.run_chat(
        company_id=COMPANY_ID,
        message="ager expense information thik chilo otatei back koro",
        session_id=sid,
        employee_id=emp,
        trace_id="snap-3",
    )
    body = r3["response"]["message"] or ""
    assert "1)" in body or "**1**" in body

    r4 = orch.run_chat(
        company_id=COMPANY_ID,
        message="1",
        session_id=sid,
        employee_id=emp,
        trace_id="snap-4",
    )
    msg = r4["response"]["message"] or ""
    assert "Bus" in msg or "Bike" in msg
    assert "restore" in msg.lower() or "Restore" in msg or "restore" in msg
