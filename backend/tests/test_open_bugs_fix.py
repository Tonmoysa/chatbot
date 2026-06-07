"""Regression tests for the 5 remaining open bugs."""

from datetime import date, timedelta

import pytest

from chat.constants import INTENT_EXPENSE_STATUS, INTENT_HR_POLICY
from chat.services.entity_extractor import EntityExtractor
from chat.services.expense.expense_confirm import apply_corrections
from chat.services.expense_extraction import extract_expense_items
from chat.services.expense_incurred_date import (
    infer_expense_incurred_date_iso,
    message_has_relative_date_signal,
)
from chat.services.expense_workflow import process_expense_turn
from chat.services.leave.entity_pipeline import LeaveEntityPipeline
from chat.services.leave_confirm import process_confirmation_turn
from chat.services.leave_fsm import read_leave_state
from chat.services.orchestrator import (
    ChatOrchestrator,
    _asks_expense_ref_or_status,
    _latest_expense_submission_from_session,
)
from chat.services.policy_intent_helpers import (
    is_general_knowledge_out_of_scope,
    is_hr_today_date_query,
)

COMPANY_ID = "company-a"


def test_expense_date_ajke_beats_llm_hint():
    today = date(2026, 6, 7)
    wrong = (today - timedelta(days=1)).isoformat()
    inc = infer_expense_incurred_date_iso(
        message="amar ajke bus 50 taka",
        hints={"expense_incurred_date": wrong},
        today=today,
    )
    assert inc == today.isoformat()
    assert message_has_relative_date_signal("amar ajke bus 50 taka")


def test_expense_date_kalke_is_yesterday():
    today = date(2026, 6, 7)
    inc = infer_expense_incurred_date_iso(
        message="goto kal er expense summary",
        hints={"expense_incurred_date": today.isoformat()},
        today=today,
    )
    assert inc == (today - timedelta(days=1)).isoformat()


def test_metro_train_duplicate_collapsed():
    msg = (
        "ami ajke mirpur theke uttora aschi metroral e expense hoyeche 40 taka"
    )
    result = extract_expense_items(msg)
    cats = [it.category for it in result.items]
    assert "Metro Rail" in cats
    assert "Train" not in cats


def test_travel_cost_remove_at_review():
    items = [
        {"category": "Bus", "amount": 50},
        {"category": "Bike", "amount": 150},
        {"category": "Lunch", "amount": 50},
    ]
    out, changed = apply_corrections(items, "travel cost remove koro")
    assert changed
    assert len(out) == 1
    assert out[0]["category"] == "Lunch"

    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "items": items,
        }
    }
    pack = process_expense_turn(workflow_state=wf, message="travel cost remove koro")
    assert len(pack["items"]) == 1


def test_leave_review_weather_does_not_change_date():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "review_pending": True,
        "draft": {
            "start_date": "2026-06-08",
            "end_date": "2026-06-08",
            "leave_payment_category": "paid",
            "day_scope": "full",
            "reason": "amar paye betha",
        },
    }
    pack = process_confirmation_turn(
        workflow_state=wf,
        message="ajke onek gorom porche",
        entities={"start_date": "2026-06-07", "date": "2026-06-07"},
    )
    draft = read_leave_state(pack["workflow_state"]).get("draft") or {}
    assert draft.get("start_date") == "2026-06-08"


def test_leave_pipeline_preserves_date_on_casual_review_text():
    draft = {
        "start_date": "2026-06-08",
        "end_date": "2026-06-08",
        "reason": "fever",
        "leave_payment_category": "paid",
        "day_scope": "full",
    }
    LeaveEntityPipeline().apply_to_draft(
        draft,
        "ajke onek gorom",
        {"start_date": "2026-06-07"},
        overwrite=True,
    )
    assert draft["start_date"] == "2026-06-08"


def test_exp_ref_id_parsed():
    ext = EntityExtractor()
    ent = ext.extract_rules_only("expense status for EXP-2026-ABC123")
    assert ent.get("request_id") == "EXP-2026-ABC123"


def test_latest_expense_submission_from_history():
    wf = {
        "expense_submissions_history": [
            {"reference_id": "EXP-2026-OLD111", "items": []},
            {"reference_id": "EXP-2026-NEW222", "items": [{"category": "Bus", "amount": 50}]},
        ]
    }
    last = _latest_expense_submission_from_session(wf)
    assert last["reference_id"] == "EXP-2026-NEW222"


def test_asks_expense_ref_or_status_heuristic():
    assert _asks_expense_ref_or_status("amar expense er ref id ki")
    assert _asks_expense_ref_or_status("expense status EXP-2026-ABC123")


def test_ajker_date_not_out_of_scope():
    assert is_hr_today_date_query("ajker date?")
    assert not is_general_knowledge_out_of_scope("ajker date?")


@pytest.mark.django_db
def test_orchestrator_ajker_date_reply(monkeypatch):
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    r = orch.run_chat(
        company_id=COMPANY_ID,
        message="ajker date?",
        session_id=None,
        employee_id="open-bug-date",
        trace_id="ob-date",
    )
    assert r["intent"] == INTENT_HR_POLICY
    assert "2026" in (r["response"]["message"] or "")


@pytest.mark.django_db
def test_orchestrator_expense_ref_status_uses_session_history(monkeypatch):
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    emp = "open-bug-ref"
    r1 = orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 100",
        session_id=None,
        employee_id=emp,
        trace_id="ob-ref-1",
    )
    sid = r1["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    wf = dict(session.workflow_state or {})
    wf["expense_submissions_history"] = [
        {
            "reference_id": "EXP-2026-TEST99",
            "incurred_date_iso": "2026-06-07",
            "items": [{"category": "Lunch", "amount": 100}],
        }
    ]
    wf["expense_last_submission"] = wf["expense_submissions_history"][0]
    session.workflow_state = wf
    session.save(update_fields=["workflow_state"])

    r2 = orch.run_chat(
        company_id=COMPANY_ID,
        message="amar expense er status ki",
        session_id=sid,
        employee_id=emp,
        trace_id="ob-ref-2",
    )
    assert r2["intent"] == INTENT_EXPENSE_STATUS
    msg = r2["response"]["message"] or ""
    assert "EXP-2026-TEST99" in msg or "জমা হয়েছে" in msg
