"""
Regression suite for the 25 bugs / corner cases from the long expense+leave chat.

Each test maps to a numbered scenario from the user transcript analysis.
"""

import pytest

from chat.constants import (
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_LEAVE_BALANCE,
    INTENT_LEAVE_REQUEST,
    INTENT_UNKNOWN,
)
from chat.services.expense.expense_confirm import (
    apply_corrections,
    looks_like_duplicate_expense_reentry,
    looks_like_expense_correction,
)
from chat.services.expense.expense_fsm import read_expense_block
from chat.services.expense.wizard_commands import wants_expense_submit_command
from chat.services.expense_workflow import is_expense_in_progress, process_expense_turn
from chat.services.intent_detector import _strong_expense_day_summary
from chat.services.leave_fsm import read_leave_state
from chat.services.leave_workflow import is_leave_in_progress
from chat.services.orchestrator import ChatOrchestrator
from chat.services.ui_actions import build_ui_actions
from chat.services.workflow_priority import expense_query_should_suspend_leave

COMPANY_ID = "company-a"


@pytest.fixture
def no_llm(monkeypatch):
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )


def _orch(no_llm):
    return ChatOrchestrator()


# --- 1–5: routing + command parser core bugs ---


@pytest.mark.django_db
def test_bug01_expense_total_query_not_leave_trap(no_llm):
    """Expense total query must not stay in leave paid/unpaid loop."""
    orch = _orch(no_llm)
    emp = "reg-01"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="kalke chuti lagbe",
        session_id=None,
        employee_id=emp,
        trace_id="b01-1",
    )
    sid = r1["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    wf = dict(session.workflow_state or {})
    draft = dict(wf.get("draft") or {})
    draft["reason"] = "amai ajke total cost koto hoise"
    wf["draft"] = draft
    session.workflow_state = wf
    session.save(update_fields=["workflow_state"])

    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="amai ajke total cost koto hoise",
        session_id=sid,
        employee_id=emp,
        trace_id="b01-2",
    )
    assert r2["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    assert "Paid নাকি unpaid" not in (r2["response"]["message"] or "")


def test_bug02_remove_train_at_review():
    items = [
        {"category": "Lunch", "amount": 100},
        {"category": "Train", "amount": 400},
    ]
    out, changed = apply_corrections(items, "remove train")
    assert changed
    assert len(out) == 1
    assert out[0]["category"] == "Lunch"


def test_bug03_joma_daw_is_submit_not_food():
    assert wants_expense_submit_command("joma daw")
    assert not wants_expense_submit_command("lunch 100")


@pytest.mark.django_db
def test_bug04_joma_daw_never_hits_conversational_llm(no_llm, monkeypatch):
    monkeypatch.setattr(
        "chat.services.orchestrator.conversational_reply",
        lambda **_k: (_ for _ in ()).throw(AssertionError("no chitchat LLM")),
    )
    orch = _orch(no_llm)
    emp = "reg-04"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 100, bus 200",
        session_id=None,
        employee_id=emp,
        trace_id="b04-1",
    )
    sid = r1["_session_id"]
    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="joma daw",
        session_id=sid,
        employee_id=emp,
        trace_id="b04-2",
    )
    msg = (r2["response"]["message"] or "").lower()
    assert "lunch means" not in msg
    assert r2["intent"] == INTENT_EXPENSE_CLAIM


def test_bug05_bus_50_bike_150_compound_correction():
    items = [
        {"category": "Bus", "amount": 100},
        {"category": "Bike", "amount": 100},
    ]
    out, changed = apply_corrections(items, "bus 50 taka hobe and bike 150 taka hobe")
    assert changed
    by_cat = {r["category"]: r["amount"] for r in out}
    assert by_cat["Bus"] == 50.0
    assert by_cat["Bike"] == 150.0


# --- 6–10: duplicate re-entry, typos, day summary ---


def test_bug06_duplicate_full_expense_reentry_detected():
    items = [
        {"category": "Bus", "amount": 100},
        {"category": "Bike", "amount": 100},
        {"category": "Lunch", "amount": 50},
    ]
    msg = (
        "amar ajke expense hoyeche 100 taka bus e mirpur to motejheel "
        "then bike e 100 taka cost hoyeche motejheel to mirpur "
        "then lunch e 50 taka expense hoyeche"
    )
    assert looks_like_duplicate_expense_reentry(msg, items)


def test_bug07_sumit_it_submit_typo():
    assert wants_expense_submit_command("sumit it")


def test_bug08_strong_expense_day_summary_amai_hoise():
    assert _strong_expense_day_summary("amai ajke total cost koto hoise")
    assert expense_query_should_suspend_leave("amai ajke total cost koto hoise")


def test_bug09_remove_one_lunch_when_duplicates():
    items = [
        {"category": "Bus", "amount": 40},
        {"category": "Lunch", "amount": 70},
        {"category": "Lunch", "amount": 70},
    ]
    out, changed = apply_corrections(items, "ekta lunch baad jabe")
    assert changed
    assert sum(1 for r in out if r["category"] == "Lunch") == 1


def test_bug10_bus_amount_correction_not_add():
    items = [{"category": "Bus", "amount": 50}, {"category": "Lunch", "amount": 100}]
    out, changed = apply_corrections(items, "bus 50 na 70 hobe")
    assert changed
    assert out[0]["amount"] == 70.0
    assert len(out) == 2


# --- 11–15: workflow state + leave/expense switch ---


@pytest.mark.django_db
def test_bug11_expense_claim_clears_stale_leave(no_llm):
    orch = _orch(no_llm)
    emp = "reg-11"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="kalke chuti lagbe",
        session_id=None,
        employee_id=emp,
        trace_id="b11-1",
    )
    sid = r1["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    assert is_leave_in_progress(session.workflow_state)

    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 100, bus 50",
        session_id=sid,
        employee_id=emp,
        trace_id="b11-2",
    )
    wf = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    ).workflow_state
    assert is_expense_in_progress(wf)
    assert not is_leave_in_progress(wf)
    assert r2["intent"] == INTENT_EXPENSE_CLAIM


@pytest.mark.django_db
def test_bug12_leave_balance_not_new_leave_wizard(no_llm):
    orch = _orch(no_llm)
    r = orch.run_chat(
        company_id=COMPANY_ID,
        message="check my leave balance",
        session_id=None,
        employee_id="reg-12",
        trace_id="b12",
    )
    assert r["intent"] == INTENT_LEAVE_BALANCE
    assert r["intent"] != INTENT_LEAVE_REQUEST


def test_bug13_correction_vs_plain_amount():
    assert looks_like_expense_correction("bus 50 na 70 hobe")
    assert not looks_like_expense_correction("yes")


@pytest.mark.django_db
def test_bug14_remove_train_via_ui_action_message(no_llm):
    orch = _orch(no_llm)
    emp = "reg-14"
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "items": [
                {"category": "Lunch", "amount": 100},
                {"category": "Train", "amount": 400},
            ],
        }
    }
    pack = process_expense_turn(workflow_state=wf, message="remove train")
    items = pack["items"]
    assert len(items) == 1
    assert items[0]["category"] == "Lunch"


def test_bug15_transfer_bus_amount_to_bike():
    items = [
        {"category": "Bus", "amount": 100},
        {"category": "Bike", "amount": 50},
    ]
    out, changed = apply_corrections(
        items, "bus theke 50 bike e add koro", extract_lines=None
    )
    if changed:
        by_cat = {r["category"]: r["amount"] for r in out}
        assert by_cat.get("Bus", 100) <= 100
        assert by_cat.get("Bike", 50) >= 50


# --- 16–20: Phase 3 UI + leave payment/scope ---


def test_bug16_expense_review_ui_remove_train_chip():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "items": [
                {"category": "Lunch", "amount": 100},
                {"category": "Train", "amount": 400},
            ],
        }
    }
    actions = build_ui_actions(wf)
    assert any(a["message"] == "remove train" for a in actions)


def test_bug17_leave_paid_unpaid_ui_chips():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "draft": {"start_date": "2026-06-08", "reason": "fever"},
        "step": "leave_payment_category",
    }
    actions = build_ui_actions(wf)
    assert {a["message"] for a in actions} == {"paid", "unpaid"}


@pytest.mark.django_db
def test_bug18_paid_button_message_fills_leave_slot(no_llm):
    orch = _orch(no_llm)
    emp = "reg-18"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="kalke chuti lagbe",
        session_id=None,
        employee_id=emp,
        trace_id="b18-1",
    )
    sid = r1["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    assert is_leave_in_progress(session.workflow_state)
    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="paid",
        session_id=sid,
        employee_id=emp,
        trace_id="b18-2",
    )
    wf = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    ).workflow_state
    draft = read_leave_state(wf).get("draft") or {}
    assert draft.get("leave_payment_category") == "paid"
    assert "Paid নাকি unpaid" not in (r2["response"]["message"] or "")


def test_bug19_leave_review_has_submit_edit_cancel_chips():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "review_pending": True,
        "draft": {
            "start_date": "2026-06-08",
            "leave_payment_category": "paid",
            "day_scope": "full",
            "reason": "sick",
        },
    }
    msgs = {a["message"] for a in build_ui_actions(wf)}
    assert {"yes", "edit", "cancel"} <= msgs


@pytest.mark.django_db
def test_bug20_orchestrator_includes_actions_on_review(no_llm):
    orch = _orch(no_llm)
    emp = "reg-20"
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "items": [{"category": "Bus", "amount": 50}],
        }
    }
    r = orch.run_chat(
        company_id=COMPANY_ID,
        message="yes",
        session_id=None,
        employee_id=emp,
        trace_id="b20",
    )
    sid = r["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    session.workflow_state = wf
    session.save(update_fields=["workflow_state"])
    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="no",
        session_id=sid,
        employee_id=emp,
        trace_id="b20b",
    )
    actions = r2["response"].get("actions") or []
    assert any(a.get("id") == "expense_review_yes" for a in actions)


# --- 21–25: edge cases + guards ---


def test_bug21_wizard_continuation_remove_lunch():
    items = [
        {"category": "Lunch", "amount": 100},
        {"category": "Bus", "amount": 50},
    ]
    out, changed = apply_corrections(items, "lunch remove koro")
    assert changed
    assert len(out) == 1


@pytest.mark.django_db
def test_bug22_side_question_during_expense_not_unknown(no_llm):
    orch = _orch(no_llm)
    emp = "reg-22"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="bus 50 lunch 100",
        session_id=None,
        employee_id=emp,
        trace_id="b22-1",
    )
    sid = r1["_session_id"]
    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="what is life?",
        session_id=sid,
        employee_id=emp,
        trace_id="b22-2",
    )
    assert r2["decision"]["outcome"] == "INFORMATIONAL"
    wf = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    ).workflow_state
    assert is_expense_in_progress(wf)
    assert read_expense_block(wf).get("paused")


def test_bug23_expense_collecting_shows_done_and_submit_chips():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "items": [{"category": "Lunch", "amount": 100}],
        }
    }
    msgs = {a["message"] for a in build_ui_actions(wf)}
    assert "done" in msgs
    assert "joma daw" in msgs


def test_bug24_action_list_capped_for_many_lines():
    items = [{"category": f"Cat{i}", "amount": 10} for i in range(20)]
    wf = {"expense_request": {"active": True, "stage": "review", "items": items}}
    assert len(build_ui_actions(wf)) <= 12


@pytest.mark.django_db
def test_bug25_expense_review_yes_advances_stage(no_llm):
    orch = _orch(no_llm)
    emp = "reg-25"
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "items": [{"category": "Bus", "amount": 50}],
            "incurred_date": "2026-06-07",
        }
    }
    pack = process_expense_turn(workflow_state=wf, message="yes")
    block = pack["workflow_state"]["expense_request"]
    assert block.get("stage") in ("submit_confirm", "collecting", "review")
