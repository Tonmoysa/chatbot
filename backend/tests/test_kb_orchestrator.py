from unittest.mock import patch

import types

import pytest

from chat.constants import INTENT_UNKNOWN
from chat.services.orchestrator import ChatOrchestrator


@pytest.mark.django_db
def test_orchestrator_hr_policy_uses_rag_when_hit(settings):
    settings.KB_RAG_ENABLED = True
    with patch(
        "chat.services.orchestrator.try_hr_policy_rag",
        return_value={
            "hit": True,
            "text": "Grounded snippet.",
            "sources": [{"document": "D", "section": "S", "snippet": "x", "score": 0.9}],
            "mode": "rag",
        },
    ):
        orch = ChatOrchestrator()
        out = orch.run_chat(
            message="What is the dress code policy?",
            session_id=None,
            employee_id="E1",
            trace_id="trace-rag-1",
        )
    assert out["intent"] == "HR_POLICY"
    assert "Grounded snippet" in out["response"]["message"]
    assert out.get("sources")


@pytest.mark.django_db
def test_orchestrator_hr_policy_rag_miss_skips_static_handbook(settings):
    """No RAG hit must not serve rules_handbook keyword sections (PDF is source of truth)."""
    settings.KB_RAG_ENABLED = True
    with patch(
        "chat.services.orchestrator.try_hr_policy_rag",
        return_value=None,
    ):
        orch = ChatOrchestrator()
        out = orch.run_chat(
            message="Attendance & Working Hours Policy",
            session_id=None,
            employee_id="E1",
            trace_id="trace-rag-miss",
        )
    assert out["intent"] == "HR_POLICY"
    msg = out["response"]["message"]
    assert "Joining Requirements" not in msg
    assert "could not find this policy" in msg.lower()


@pytest.mark.django_db
def test_orchestrator_unknown_policy_question_rag(settings):
    settings.KB_RAG_ENABLED = True

    def _force_unknown(self, message: str, trace_id: str):
        return {"intent": INTENT_UNKNOWN, "confidence": 1.0, "source": "test"}

    with patch(
        "chat.services.orchestrator.try_hr_policy_rag",
        return_value={
            "hit": True,
            "text": "From KB.",
            "sources": [{"document": "Handbook", "section": "", "snippet": "…", "score": 0.8}],
            "mode": "rag",
        },
    ):
        orch = ChatOrchestrator()
        orch.intents.detect = types.MethodType(_force_unknown, orch.intents)
        out = orch.run_chat(
            message="what is the policy on zebras at the office",
            session_id=None,
            employee_id="E1",
            trace_id="trace-rag-2",
        )
    assert out["intent"] == "UNKNOWN"
    assert "From KB." in out["response"]["message"]
    assert out.get("sources")
