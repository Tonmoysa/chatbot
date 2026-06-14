"""Add/modify choice and modify-target line updates."""

from __future__ import annotations

from chat.services.expense_workflow import process_expense_turn


def _two_lunch_draft() -> dict:
    pack = process_expense_turn(workflow_state={}, message="lunch 150 taka")
    wf = pack["workflow_state"]
    wf["expense_request"]["incurred_date_iso"] = "2026-06-12"
    pack = process_expense_turn(workflow_state=wf, message="lunch 120 taka")
    return pack["workflow_state"]


def test_two_lunch_triggers_add_modify_prompt():
    wf = _two_lunch_draft()
    pack = process_expense_turn(workflow_state=wf, message="lunch 150 taka")
    q = pack.get("question") or ""
    assert "modify" in q.lower() or "add" in q.lower()
    assert "150" in q


def test_modify_choice_updates_single_matching_line():
    wf = _two_lunch_draft()
    pack = process_expense_turn(workflow_state=wf, message="nasta 50 taka")
    wf = pack["workflow_state"]
    pack = process_expense_turn(workflow_state=wf, message="nasta 50 taka")
    pack = process_expense_turn(workflow_state=pack["workflow_state"], message="modify korbo")
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="1",
    )
    items = pack["items"]
    snacks = [float(r.get("amount") or 0) for r in items if str(r.get("category")).lower() == "snack"]
    assert 50.0 in snacks


def test_modify_two_lunch_asks_line_number():
    wf = _two_lunch_draft()
    pack = process_expense_turn(workflow_state=wf, message="lunch 150 taka")
    pack = process_expense_turn(workflow_state=pack["workflow_state"], message="modify korbo")
    q = pack.get("question") or ""
    assert "1" in q and "2" in q or "number" in q.lower() or "নম্বর" in q
