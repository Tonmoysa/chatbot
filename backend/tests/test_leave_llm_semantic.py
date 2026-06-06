"""LLM-first semantic leave extraction (mocked — no live API in CI)."""

import datetime as dt

import pytest

from chat.constants import INTENT_LEAVE_REQUEST
from chat.services.leave.entity_pipeline import LeaveEntityPipeline
from chat.services.leave_workflow import _apply_slots_from_message
from chat.services.leave_slots import get_missing_slots


MATHA_BETHA_MSG = (
    "amar kalke leave lagbe ..onek matha betha..tai full day paid leave nite chai"
)


class _FakeLeaveLLM:
    def is_configured(self):
        return True

    def chat_json(self, *, system_prompt, user_prompt, trace_id):
        return {
            "reason": "onek matha betha",
            "leave_type": "sick",
            "leave_payment_category": "paid",
            "day_scope": "full",
            "start_date": "2026-06-06",
        }


@pytest.mark.django_db
def test_compound_leave_llm_extracts_reason_without_regex(monkeypatch):
    """LLM supplies reason even when regex patterns would miss a novel phrase."""
    fixed = dt.date(2026, 6, 5)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)

    from chat.services.entity_extractor import EntityExtractor

    pipe = LeaveEntityPipeline(EntityExtractor(llm=_FakeLeaveLLM()))
    result = pipe.extract(
        MATHA_BETHA_MSG,
        intent=INTENT_LEAVE_REQUEST,
        context_lines=[],
        trace_id="sem-1",
        use_llm=True,
    )
    assert result.field_sources.get("reason") == "llm_primary"
    assert "matha betha" in str(result.entities.get("reason") or "").lower()

    draft: dict = {}
    _apply_slots_from_message(draft, MATHA_BETHA_MSG, result.entities)
    assert "matha betha" in str(draft.get("reason") or "").lower()
    assert draft.get("leave_type") == "sick"
    assert get_missing_slots(draft) == []
