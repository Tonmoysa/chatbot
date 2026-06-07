"""Session expense ledger — submitted + pending breakdown."""

import datetime as dt

import pytest

from chat.constants import INTENT_EXPENSE_DAY_SUMMARY
from chat.services.expense.expense_fsm import save_expense_last_submission
from chat.services.expense.session_ledger import (
    build_session_expense_ledger,
    format_session_expense_ledger_message,
    wants_session_expense_ledger_query,
)
from chat.services.intent_detector import IntentDetector, _strong_expense_day_summary
from chat.services.orchestrator import ChatOrchestrator

COMPANY_ID = "company-a"


def test_wants_session_expense_ledger_query():
    assert wants_session_expense_ledger_query("ami koto expense ajke sara din add korchi")
    assert wants_session_expense_ledger_query("ajker expense history daw")
    assert wants_session_expense_ledger_query("expense history")
    assert not wants_session_expense_ledger_query("lunch 100 taka")


def test_strong_expense_day_summary_add_korchi():
    assert _strong_expense_day_summary("ami koto expense ajke sara din add korchi")
    assert _strong_expense_day_summary("total cost koto dilam sara din e")


def test_save_expense_last_submission_appends_history():
    wf: dict = {}
    wf = save_expense_last_submission(
        wf,
        reference_id="EXP-1",
        items=[{"category": "Lunch", "amount": 100}],
        incurred_date_iso="2026-06-07",
    )
    wf = save_expense_last_submission(
        wf,
        reference_id="EXP-2",
        items=[{"category": "Bus", "amount": 50}],
        incurred_date_iso="2026-06-07",
    )
    history = wf.get("expense_submissions_history") or []
    assert len(history) == 2
    refs = {h["reference_id"] for h in history}
    assert refs == {"EXP-1", "EXP-2"}


def test_merge_submitted_batches_dedupes_mock_and_exp_same_claim():
    """One user submit must not appear as MOCK + EXP twice in day summary."""
    items = [
        {"category": "Bus", "amount": 50, "from_location": "mirpur", "to_location": "motejheel"},
        {"category": "Bike", "amount": 150, "from_location": "motejheel", "to_location": "mirpur"},
        {"category": "Lunch", "amount": 50},
    ]
    wf = {
        "expense_submissions_history": [
            {
                "reference_id": "EXP-2026-CA4928",
                "items": items,
                "incurred_date_iso": "2026-06-07",
            }
        ],
    }
    ledger = build_session_expense_ledger(
        wf,
        crm_breakdown={
            "expense_day_entries": [
                {
                    "request_id": "MOCK-1FAAA97831",
                    "amount": 250,
                    "line_count": 3,
                    "outcome": "SUBMITTED",
                }
            ],
            "expense_day_items": items,
            "expense_day_logged_total": 250,
        },
        incurred_date_iso="2026-06-07",
    )
    batches = ledger.get("submitted_batches") or []
    assert len(batches) == 1
    assert batches[0]["reference_id"] == "EXP-2026-CA4928"
    assert ledger["submitted_total"] == 250
    msg = format_session_expense_ledger_message(ledger)
    assert "MOCK-" not in msg
    assert "500" not in msg


@pytest.mark.django_db
def test_single_expense_submit_day_summary_not_doubled(monkeypatch):
    """Regression: one yes/yes submit → one line in expense summary."""
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
    emp = "ledger-single-submit"
    msg = (
        "amar ajke expense hoyeche 100 taka bus e mirpur to motejheel "
        "then bike e 100 taka cost hoyeche motejheel to mirpur "
        "then lunch e 50 taka expense hoyeche"
    )
    p1 = orch.run_chat(
        company_id=COMPANY_ID,
        message=msg,
        session_id=None,
        employee_id=emp,
        trace_id="ss-1",
    )
    sid = p1["_session_id"]
    for turn, text in enumerate(("yes", "yes"), start=2):
        orch.run_chat(
            company_id=COMPANY_ID,
            message=text,
            session_id=sid,
            employee_id=emp,
            trace_id=f"ss-{turn}",
        )
    summary = orch.run_chat(
        company_id=COMPANY_ID,
        message="expense summery ta bolo",
        session_id=sid,
        employee_id=emp,
        trace_id="ss-sum",
    )
    body = summary["response"]["message"] or ""
    assert body.count("250") >= 1
    assert "500" not in body
    assert body.count("জমা হয়েছে") == 1 or body.count("✅") >= 1
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        session_id=sid,
        employee_id=emp,
    )
    history = (session.workflow_state or {}).get("expense_submissions_history") or []
    assert len(history) == 1


def test_build_session_expense_ledger_submitted_and_pending():
    wf = {
        "expense_submissions_history": [
            {
                "reference_id": "EXP-A",
                "items": [{"category": "Lunch", "amount": 200}],
                "incurred_date_iso": "2026-06-07",
            }
        ],
        "expense_request": {
            "active": True,
            "stage": "review",
            "incurred_date_iso": "2026-06-07",
            "items": [{"category": "Bus", "amount": 50, "from_location": "a", "to_location": "b"}],
        },
    }
    ledger = build_session_expense_ledger(
        wf,
        crm_breakdown={"expense_day_entries": [], "expense_day_items": []},
        incurred_date_iso="2026-06-07",
    )
    assert ledger["submitted_total"] == 200
    assert ledger["pending_total"] == 50
    assert ledger["combined_total"] == 250
    msg = format_session_expense_ledger_message(ledger)
    assert "EXP-A" in msg
    assert "Pending" in msg
    assert "250" in msg


@pytest.mark.django_db
def test_expense_submit_does_not_auto_resume_leave(monkeypatch):
    """After expense CRM submit, suspended leave stays parked until explicit resume."""
    fixed = dt.date(2026, 5, 7)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)

    from chat.services.leave_workflow import process_leave_turn
    from chat.services.leave_fsm import is_leave_in_progress
    from chat.services.workflow_suspend import has_suspended_leave

    orch = ChatOrchestrator()
    emp = "ledger-no-leave-resume"
    wf: dict = {}
    pack = process_leave_turn(
        workflow_state=wf,
        message="ami kalke sick leave nite chai",
        entities={},
        company_id=COMPANY_ID,
    )
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID,
        session_id="ledger-leave-sess",
        employee_id=emp,
    )
    session.workflow_state = pack["workflow_state"]
    session.save(update_fields=["workflow_state", "updated_at"])

    orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 100 taka, bus 50 office to badda",
        session_id=session.session_id,
        employee_id=emp,
        trace_id="lnr-exp-start",
    )
    session.refresh_from_db()
    assert has_suspended_leave(session.workflow_state)

    for _ in range(12):
        session.refresh_from_db()
        stage = (session.workflow_state.get("expense_request") or {}).get("stage")
        if stage == "submit_confirm":
            break
        orch.run_chat(
            company_id=COMPANY_ID,
            message="শেষ" if stage != "review" else "yes",
            session_id=session.session_id,
            employee_id=emp,
            trace_id=f"lnr-exp-loop-{_}",
        )

    pack = orch.run_chat(
        company_id=COMPANY_ID,
        message="yes",
        session_id=session.session_id,
        employee_id=emp,
        trace_id="lnr-exp-submit",
    )
    assert pack["decision"]["outcome"] == "SUBMITTED"
    body = pack["response"]["message"] or ""
    assert "still in progress" not in body.lower()
    assert "জমা দেবেন" not in body
    session.refresh_from_db()
    assert not is_leave_in_progress(session.workflow_state)
    assert has_suspended_leave(session.workflow_state)
    sl = session.workflow_state.get("suspended_leave") or {}
    assert sl.get("draft", {}).get("leave_type") == "sick"


@pytest.mark.django_db
def test_session_ledger_day_summary_during_draft(monkeypatch):
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
    emp = "ledger-day-sum"
    pack = orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 200 taka",
        session_id=None,
        employee_id=emp,
        trace_id="lds-1",
    )
    sid = pack["_session_id"]

    resp = orch.run_chat(
        company_id=COMPANY_ID,
        message="ami koto expense ajke sara din add korchi",
        session_id=sid,
        employee_id=emp,
        trace_id="lds-2",
    )
    assert resp["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    body = resp["response"]["message"] or ""
    assert "Pending" in body or "pending" in body.lower()
    assert "200" in body


def test_ajker_expense_list_intent_still_works():
    from chat.services.intent_detector import _strong_expense_day_summary

    assert _strong_expense_day_summary("amake ajker expense er list ta daw")
