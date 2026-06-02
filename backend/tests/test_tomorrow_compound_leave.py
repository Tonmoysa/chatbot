"""Regression: tomorrow + unpaid + family in one message must prefill dates."""

import pytest

from chat.services.leave_fsm import read_leave_state
from chat.services.leave_slot_extraction import extract_leave_slots
from chat.services.leave_slots import get_missing_slots, prefill_draft_from_extraction
from chat.services.orchestrator import ChatOrchestrator


USER_MSG = (
    "tomorrow I need a leave for my family program that live will be unpaid"
)


def test_unpaid_in_long_sentence_is_not_payment_only():
    from chat.services.leave_slot_extraction import is_payment_only_message

    assert not is_payment_only_message(USER_MSG)
    assert is_payment_only_message("unpaid")
    assert is_payment_only_message("unpaid hobe")


def test_extract_tomorrow_from_compound_message():
    ex = extract_leave_slots(USER_MSG)
    assert ex.start_date.confidence == "high"
    assert ex.start_date.value
    assert ex.reason.confidence == "high"
    assert "family" in str(ex.reason.value or "").lower()
    draft: dict = {}
    prefill_draft_from_extraction(draft, ex)
    assert draft.get("start_date")
    assert draft.get("reason")
    missing = get_missing_slots(draft, extraction=ex)
    assert "leave_dates" not in missing
    assert "reason" not in missing


@pytest.mark.django_db
def test_orchestrator_first_turn_skips_date_question(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    orch = ChatOrchestrator()
    pack = orch.run_chat(
        company_id="company-a",
        message=USER_MSG,
        session_id=None,
        employee_id="tomorrow-compound-pytest",
        trace_id="tc-leave-1",
    )
    body = pack["response"]["message"] or ""
    assert "কোন তারিখ" not in body, body
    session = orch.memory.get_or_create_session(
        company_id="company-a",
        employee_id="tomorrow-compound-pytest",
        session_id=pack["_session_id"],
    )
    draft = read_leave_state(session.workflow_state).get("draft") or {}
    assert draft.get("start_date"), draft
