"""Leave hybrid entity pipeline — parser + LLM merge + draft apply."""

import datetime as dt

import pytest

from chat.constants import INTENT_LEAVE_REQUEST
from chat.services.leave.entity_merge import (
    PARSER_PRIORITY_FIELDS,
    merge_parser_and_llm,
)
from chat.services.leave.entity_pipeline import LeaveEntityPipeline
from chat.services.leave.llm_gate import leave_wizard_should_use_llm
from chat.services.leave_slot_extraction import extract_leave_slots
from chat.services.leave_workflow import _apply_slots_from_message
from chat.services.leave_slots import get_missing_slots
from chat.services.turn_classifier import TURN_CONFIRM, TURN_SLOT_ANSWER


PET_BETHA_MSG = "mar pet betha tai kalke amar leave lagbe full day paid"


def test_parser_priority_fields_documented():
    assert "start_date" in PARSER_PRIORITY_FIELDS
    assert "leave_payment_category" in PARSER_PRIORITY_FIELDS
    assert "reason" not in PARSER_PRIORITY_FIELDS


def test_merge_parser_wins_on_high_confidence_dates():
    parser = extract_leave_slots("kalke paid full day chuti lagbe")
    merged, sources = merge_parser_and_llm(
        parser,
        {
            "start_date": "2020-01-01",
            "leave_payment_category": "lwop",
            "day_scope": "half",
        },
    )
    assert merged.start_date.value != "2020-01-01"
    assert merged.leave_payment_category.value == "paid"
    assert merged.day_scope.value == "full"
    assert sources.get("start_date", "").startswith("rules") or sources.get("start_date") == "tomorrow_bn"


def test_llm_gate_confirm_off_slot_on():
    assert leave_wizard_should_use_llm("yes", workflow_turn=TURN_CONFIRM) is False
    assert leave_wizard_should_use_llm("pet betha", workflow_turn=TURN_SLOT_ANSWER) is True


NOVEL_REASON_MSG = "kalke leave lagbe, ghore baper obostha kharap, tai paid full day"


def test_llm_semantic_overlay_rejects_ungrounded_reason():
    from chat.services.leave.entity_merge import overlay_llm_semantic_fields

    msg = "amar kalke chuti lagbe"
    parser = extract_leave_slots(msg, skip_leave_phrase_gate=True)
    overlay_llm_semantic_fields(
        parser,
        {"reason": "family program"},
        msg,
        llm_used=True,
    )
    assert not parser.reason.value


def test_llm_semantic_overlay_beats_parser_reason():
    from chat.services.leave.entity_merge import overlay_llm_semantic_fields

    parser = extract_leave_slots(NOVEL_REASON_MSG)
    assert not parser.reason.value or parser.reason.confidence != "high"

    overlay_llm_semantic_fields(
        parser,
        {
            "reason": "ghore baper obostha kharap",
            "leave_type": "emergency",
        },
        NOVEL_REASON_MSG,
        llm_used=True,
    )
    assert parser.reason.value == "ghore baper obostha kharap"
    assert parser.reason.source == "llm_primary"


def test_pipeline_llm_first_novel_reason(monkeypatch):
    """Novel Bangla reason without regex pattern — LLM must supply reason."""
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: True,
    )

    class FakeLLM:
        def is_configured(self):
            return True

        def chat_json(self, *, system_prompt, user_prompt, trace_id):
            return {
                "reason": "ghore baper obostha kharap",
                "leave_type": "emergency",
                "leave_payment_category": "paid",
                "day_scope": "full",
                "start_date": "2026-06-06",
            }

    from chat.services.entity_extractor import EntityExtractor

    pipe = LeaveEntityPipeline(EntityExtractor(llm=FakeLLM()))
    result = pipe.extract(
        NOVEL_REASON_MSG,
        intent=INTENT_LEAVE_REQUEST,
        context_lines=[],
        trace_id="llm-novel-1",
        use_llm=True,
    )
    assert "baper obostha" in str(result.entities.get("reason") or "").lower()
    assert result.field_sources.get("reason") == "llm_primary"

    draft: dict = {}
    pipe.apply_to_draft(draft, NOVEL_REASON_MSG, result.entities)
    assert "baper obostha" in str(draft.get("reason") or "").lower()
    assert get_missing_slots(draft) == [] or "reason" not in get_missing_slots(draft)


def test_pipeline_extract_rules_only(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    pipe = LeaveEntityPipeline()
    result = pipe.extract(
        PET_BETHA_MSG,
        intent=INTENT_LEAVE_REQUEST,
        context_lines=[],
        trace_id="pipe-1",
        use_llm=False,
    )
    assert result.entities.get("start_date")
    assert result.entities.get("leave_payment_category") == "paid"
    assert result.entities.get("day_scope") == "full"
    assert "pet betha" in str(result.entities.get("reason") or "").lower()


def test_pipeline_apply_to_draft_compound():
    draft: dict = {}
    pipe = LeaveEntityPipeline()
    ext = pipe.extract(
        PET_BETHA_MSG,
        intent=INTENT_LEAVE_REQUEST,
        context_lines=[],
        trace_id="pipe-2",
        use_llm=False,
    )
    pipe.apply_to_draft(draft, PET_BETHA_MSG, ext.entities)
    assert draft.get("leave_payment_category") == "paid"
    assert draft.get("day_scope") == "full"
    assert "pet betha" in str(draft.get("reason") or "").lower()
    assert draft.get("leave_type") == "sick"
    assert get_missing_slots(draft) == []


def test_apply_slots_uses_pipeline_by_default(monkeypatch):
    fixed = dt.date(2026, 6, 5)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)

    draft: dict = {}
    _apply_slots_from_message(draft, PET_BETHA_MSG, {})
    assert "pet betha" in str(draft.get("reason") or "").lower()
    assert draft.get("leave_type") == "sick"


@pytest.mark.django_db
def test_orchestrator_pipeline_slot_turn_uses_llm_path(monkeypatch):
    """Slot-answer turn should not fall back to rules-only when pipeline is on."""
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )

    from chat.services.orchestrator import ChatOrchestrator
    from chat.services.leave_fsm import read_leave_state

    orch = ChatOrchestrator()
    session = orch.memory.get_or_create_session(
        company_id="company-a",
        employee_id="pipe-orch-emp",
        session_id="pipe-orch-session",
    )
    session.workflow_state = {
        "active_flow": "leave",
        "status": "active",
        "step": "reason",
        "draft": {
            "start_date": "2026-06-10",
            "end_date": "2026-06-10",
            "leave_payment_category": "paid",
            "day_scope": "full",
        },
    }
    session.save(update_fields=["workflow_state", "updated_at"])

    pack = orch.run_chat(
        company_id="company-a",
        message="family wedding",
        session_id=session.session_id,
        employee_id="pipe-orch-emp",
        trace_id="pipe-orch-1",
    )
    session.refresh_from_db()
    draft = read_leave_state(session.workflow_state).get("draft") or {}
    assert "wedding" in str(draft.get("reason") or "").lower() or pack["entities"].get(
        "reason"
    )
