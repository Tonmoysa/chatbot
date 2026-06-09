"""Phase 3: contextual UI action chips."""

import pytest

from chat.services.expense.expense_fsm import read_expense_block
from chat.services.ui_actions import build_ui_actions
from chat.services.orchestrator import ChatOrchestrator, _attach_ui_actions

COMPANY_ID = "company-a"


def test_expense_review_actions_include_yes_remove_lines():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "items": [
                {"category": "Bus", "amount": 50},
                {"category": "Lunch", "amount": 100},
                {"category": "Train", "amount": 400},
            ],
        }
    }
    actions = build_ui_actions(wf)
    ids = [a["id"] for a in actions]
    assert "expense_review_yes" in ids
    assert "expense_review_no" in ids
    assert any(a["message"] == "remove train" for a in actions)
    assert any(a["message"] == "remove lunch" for a in actions)
    assert len(actions) <= 12


def test_expense_submit_confirm_actions():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "submit_confirm",
            "items": [{"category": "Bus", "amount": 50}],
        }
    }
    actions = build_ui_actions(wf)
    assert actions[0]["id"] == "expense_submit_yes"
    assert actions[0]["message"] == "yes"


def test_expense_collecting_actions_when_items_present():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "items": [{"category": "Lunch", "amount": 100}],
        }
    }
    actions = build_ui_actions(wf)
    msgs = {a["message"] for a in actions}
    assert "done" in msgs
    assert "joma daw" in msgs


def test_leave_payment_step_actions():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {"start_date": "2026-06-08", "reason": "sick"},
        "step": "leave_payment_category",
    }
    actions = build_ui_actions(wf)
    assert {a["message"] for a in actions} == {"paid", "unpaid"}


def test_leave_scope_step_actions():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {
            "start_date": "2026-06-08",
            "reason": "sick",
            "leave_payment_category": "paid",
        },
        "step": "day_scope",
    }
    actions = build_ui_actions(wf)
    assert {a["message"] for a in actions} == {"full day", "half day"}


def test_leave_review_confirmation_actions():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "review_pending": True,
        "draft": {
            "start_date": "2026-06-08",
            "reason": "sick",
            "leave_payment_category": "paid",
            "day_scope": "full",
        },
    }
    actions = build_ui_actions(wf)
    assert actions[0]["message"] == "yes"
    assert any(a["message"] == "edit" for a in actions)


def test_attach_ui_actions_on_envelope():
    env = {"response": {"message": "hello", "status": "success"}}
    out = _attach_ui_actions(
        env,
        {"expense_request": {"active": True, "stage": "review", "items": []}},
    )
    assert "actions" in out["response"]
    assert out["response"]["actions"][0]["id"] == "expense_review_yes"


@pytest.mark.django_db
def test_orchestrator_returns_actions_on_expense_review(monkeypatch):
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    emp = "p3-actions"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 100, bus 50, train 400",
        session_id=None,
        employee_id=emp,
        trace_id="p3-a1",
    )
    sid = r1["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    block = read_expense_block(session.workflow_state)
    block["stage"] = "review"
    block["items"] = [
        {"category": "Lunch", "amount": 100},
        {"category": "Bus", "amount": 50},
        {"category": "Train", "amount": 400},
    ]
    wf = dict(session.workflow_state or {})
    wf["expense_request"] = block
    session.workflow_state = wf
    session.save(update_fields=["workflow_state"])

    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="no",
        session_id=sid,
        employee_id=emp,
        trace_id="p3-a2",
    )
    actions = r2["response"].get("actions") or []
    assert any(a.get("message") == "remove train" for a in actions)
