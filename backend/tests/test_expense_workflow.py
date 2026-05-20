"""Enterprise expense workflow: extraction, corrections, confirmation."""

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM
from chat.services.expense_extraction import extract_expense_items
from chat.services.expense_workflow import (
    format_expense_summary,
    is_expense_collecting,
    process_expense_turn,
)
from chat.services.orchestrator import ChatOrchestrator

COMPANY_ID = "company-a"


def test_extract_multi_item_bangla():
    msg = (
        "আজ lunch এ 100 টাকা খরচ করেছি, "
        "bus এ 50 টাকা, rickshaw এ 20 টাকা"
    )
    ext = extract_expense_items(msg)
    cats = {i.category for i in ext.items}
    assert "Lunch" in cats
    assert "Bus" in cats
    assert "Rickshaw" in cats
    assert sum(i.amount for i in ext.items) == 170


def test_correction_update_amount():
    wf = {"expense_request": {"active": True, "stage": "confirming", "items": [
        {"category": "Bus", "amount": 50},
        {"category": "Lunch", "amount": 100},
    ]}}
    pack = process_expense_turn(workflow_state=wf, message="bus 50 না 70 হবে")
    items = pack["items"]
    bus = next(r for r in items if r["category"] == "Bus")
    assert bus["amount"] == 70


def test_correction_remove_item():
    wf = {"expense_request": {"active": True, "stage": "confirming", "items": [
        {"category": "Lunch", "amount": 100},
        {"category": "Bus", "amount": 50},
    ]}}
    pack = process_expense_turn(workflow_state=wf, message="lunch remove করো")
    cats = [r["category"] for r in pack["items"]]
    assert "Lunch" not in cats
    assert "Bus" in cats


@pytest.mark.django_db
def test_orchestrator_expense_confirm_submit():
    orch = ChatOrchestrator()
    emp = "expense-wf-pytest"
    first = orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 100, bus 50, rickshaw 20",
        session_id=None,
        employee_id=emp,
        trace_id="exp-wf-1",
    )
    assert first["intent"] == INTENT_EXPENSE_CLAIM
    assert "expense summary" in first["response"]["message"].lower() or "summary" in first["response"]["message"]
    assert is_expense_collecting(
        orch.memory.get_or_create_session(
            company_id=COMPANY_ID, employee_id=emp, session_id=first["_session_id"]
        ).workflow_state
    )

    second = orch.run_chat(
        company_id=COMPANY_ID,
        message="হ্যাঁ",
        session_id=first["_session_id"],
        employee_id=emp,
        trace_id="exp-wf-2",
    )
    assert "submit" in second["response"]["message"].lower() or "Reference" in second["response"]["message"]
    assert second["decision"]["outcome"] == "SUBMITTED"
    ref = second["response"]["request_id"] or ""
    assert ref.startswith("EXP-")
