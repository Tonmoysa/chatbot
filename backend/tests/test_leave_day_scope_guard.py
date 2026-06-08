"""day_scope must not be invented — only explicit full/half or direct slot answers."""

import datetime as dt

import pytest

from chat.services.leave.entity_pipeline import LeaveEntityPipeline
from chat.services.leave.entity_merge import merge_parser_and_llm
from chat.services.leave_fsm import read_leave_state
from chat.services.leave_slot_extraction import extract_leave_slots
from chat.services.leave_slots import SLOT_SCOPE, get_missing_slots
from chat.services.leave_workflow import process_leave_turn
from chat.services.leave.normalization import message_explicitly_states_day_scope
from chat.services.orchestrator import ChatOrchestrator

SICK_NO_SCOPE = (
    "amar soril ta khubei kharap pet betha matha betha tai amar kalke chuti lagbe paid"
)
SICK_NO_SCOPE_V2 = (
    "amar soril ta khubei kharap pet betha tai amar kalke chuti lagbe paid"
)


def test_message_explicitly_states_day_scope():
    assert message_explicitly_states_day_scope("kalke full day paid chuti")
    assert message_explicitly_states_day_scope("half day lagbe")
    assert not message_explicitly_states_day_scope(SICK_NO_SCOPE)
    assert not message_explicitly_states_day_scope(SICK_NO_SCOPE_V2)


def test_llm_invented_scope_stripped_from_merge():
    parser = extract_leave_slots(SICK_NO_SCOPE, skip_leave_phrase_gate=True)
    merged, _ = merge_parser_and_llm(
        parser,
        {"day_scope": "full", "leave_payment_category": "paid"},
        message=SICK_NO_SCOPE,
    )
    assert merged.day_scope.confidence != "high" or merged.day_scope.value is None


def test_pipeline_apply_does_not_invent_scope():
    draft: dict = {}
    pipe = LeaveEntityPipeline()
    pipe.apply_to_draft(
        draft,
        SICK_NO_SCOPE,
        {
            "day_scope": "full",
            "start_date": "2026-06-07",
            "leave_payment_category": "paid",
            "reason": "pet betha",
        },
    )
    assert not draft.get("day_scope")
    assert "day_scope" in get_missing_slots(draft)


def test_process_leave_turn_repeat_while_pending_scope(monkeypatch):
    fixed = dt.date(2026, 6, 6)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": SLOT_SCOPE,
        "draft": {
            "start_date": "2026-06-07",
            "end_date": "2026-06-07",
            "leave_payment_category": "paid",
            "leave_type": "sick",
            "reason": "pet betha",
        },
    }
    pack = process_leave_turn(
        workflow_state=wf,
        message=SICK_NO_SCOPE_V2,
        entities={"day_scope": "full", "start_date": "2026-06-07"},
        company_id="company-a",
    )
    draft = read_leave_state(pack["workflow_state"]).get("draft") or {}
    assert not draft.get("day_scope")
    assert SLOT_SCOPE in get_missing_slots(draft)
    q = pack.get("question") or ""
    assert "Full Day" in q or "Half Day" in q or "full" in q.lower()


@pytest.mark.django_db
def test_orchestrator_repeat_compound_while_awaiting_scope(monkeypatch):
    fixed = dt.date(2026, 6, 6)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)

    llm_calls = {"n": 0}

    def _fake_json(self, *, system_prompt, user_prompt, trace_id):
        llm_calls["n"] += 1
        return {
            "start_date": "2026-06-07",
            "end_date": "2026-06-07",
            "leave_payment_category": "paid",
            "leave_type": "sick",
            "day_scope": "full",
            "reason": "pet betha",
        }

    monkeypatch.setattr(
        "chat.services.llm_client.LLMClient.is_configured",
        lambda self: True,
    )
    monkeypatch.setattr(
        "chat.services.llm_client.LLMClient.chat_json",
        _fake_json,
    )

    orch = ChatOrchestrator()
    emp = "scope-guard-emp"
    sid = None

    pack = orch.run_chat(
        company_id="company-a",
        message=SICK_NO_SCOPE,
        session_id=None,
        employee_id=emp,
        trace_id="scope-g-1",
    )
    sid = pack["_session_id"]
    body1 = pack["response"]["message"] or ""
    assert "Full Day" in body1 or "Half Day" in body1

    pack = orch.run_chat(
        company_id="company-a",
        message=SICK_NO_SCOPE_V2,
        session_id=sid,
        employee_id=emp,
        trace_id="scope-g-2",
    )
    body2 = pack["response"]["message"] or ""
    draft = read_leave_state(
        orch.memory.get_or_create_session(
            company_id="company-a", employee_id=emp, session_id=sid
        ).workflow_state
    ).get("draft") or {}
    assert not draft.get("day_scope")
    assert "Full Day" in body2 or "Half Day" in body2
    assert "জমা দেবেন" not in body2

    pack = orch.run_chat(
        company_id="company-a",
        message="full day",
        session_id=sid,
        employee_id=emp,
        trace_id="scope-g-3",
    )
    draft3 = read_leave_state(
        orch.memory.get_or_create_session(
            company_id="company-a", employee_id=emp, session_id=sid
        ).workflow_state
    ).get("draft") or {}
    assert draft3.get("day_scope") == "full"
    assert "জমা দেবেন" in (pack["response"]["message"] or "") or pack.get("decision")
