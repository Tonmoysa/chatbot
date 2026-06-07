"""General calendar / festival questions must not trigger RAG or invented answers."""

import pytest

from chat.constants import INTENT_HR_POLICY, INTENT_UNKNOWN
from chat.services.orchestrator import ChatOrchestrator
from chat.services.policy_intent_helpers import (
    build_out_of_scope_message,
    is_general_knowledge_out_of_scope,
    is_hr_assistant_in_scope,
    is_off_topic_for_hr_assistant,
    is_policy_handbook_complaint,
    is_policy_kb_query,
    is_rules_query,
)


@pytest.mark.parametrize(
    "message",
    [
        "eid kobe",
        "bijoy debosh kobe?",
        "durga puja kobe",
        "26 march ki dibosh?",
        "26 march ki disbosh",
        "25th december eta ki din?",
        "boro din kobe",
    ],
)
def test_general_knowledge_out_of_scope_detected(message: str) -> None:
    assert is_general_knowledge_out_of_scope(message)


@pytest.mark.parametrize(
    "message",
    [
        "25th december eta ki din?",
        "what is the weather in dhaka today?",
        "who won the cricket match yesterday?",
    ],
)
def test_dynamic_off_topic_without_static_festival_list(message: str) -> None:
    assert is_off_topic_for_hr_assistant(message)
    assert not is_policy_kb_query(message)


def test_country_name_off_topic_even_during_wizard() -> None:
    assert is_off_topic_for_hr_assistant("amader desher nam ki?", wizard_active=True)
    assert not is_hr_assistant_in_scope("amader desher nam ki?")


def test_short_gk_question_off_topic_during_wizard() -> None:
    assert is_off_topic_for_hr_assistant("python ki?", wizard_active=True)
    assert is_off_topic_for_hr_assistant("python ki", wizard_active=True)


def test_policy_kb_query_for_named_policy_ask() -> None:
    assert is_policy_kb_query("what is the leave policy")
    assert is_policy_kb_query("leave policy ta bolo amake")


@pytest.mark.parametrize(
    "message",
    [
        "eid er chuti policy koto din",
        "company er eid leave policy bolo",
        "what is the divali celebration leave policy",
    ],
)
def test_company_policy_about_occasion_stays_in_scope(message: str) -> None:
    assert not is_general_knowledge_out_of_scope(message)


def test_policy_handbook_complaint_not_rules_query() -> None:
    msg = "ei dhoroner kono besoy toh policy te nai....tahole tumi eta kivabe pele?"
    assert is_policy_handbook_complaint(msg)
    assert not is_rules_query(msg)


@pytest.mark.django_db
def test_orchestrator_eid_kobe_out_of_scope_not_rag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    rag_called = {"n": 0}

    def _rag(*_a, **_k):
        rag_called["n"] += 1
        return {"hit": True, "text": "Wrong policy snippet.", "sources": [], "mode": "rag"}

    monkeypatch.setattr("chat.services.orchestrator.try_hr_policy_rag", _rag)
    monkeypatch.setattr(
        "chat.services.orchestrator.conversational_reply",
        lambda **_k: "আপনি কখন ঈদ পালন করতে চান?",
    )

    orch = ChatOrchestrator()
    out = orch.run_chat(
        company_id="company-a",
        message="eid kobe",
        session_id=None,
        employee_id="oos-eid-emp",
        trace_id="oos-eid-1",
    )
    text = (out.get("response") or {}).get("message") or ""
    assert rag_called["n"] == 0
    assert out.get("intent") == INTENT_UNKNOWN
    assert "পালন" not in text
    assert "ছুটি" in text or "পলিসি" in text or "HR" in text
    assert (
        "বাইরে" in text
        or "সাহায্য" in text
        or "scope" in text.lower()
        or "general knowledge" in text.lower()
    )


@pytest.mark.django_db
def test_orchestrator_policy_complaint_no_rag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    rag_called = {"n": 0}

    def _rag(*_a, **_k):
        rag_called["n"] += 1
        return {
            "hit": True,
            "text": "Job Termination Policy\nNotice Period: 30–90 days",
            "sources": [],
            "mode": "rag",
        }

    monkeypatch.setattr("chat.services.orchestrator.try_hr_policy_rag", _rag)

    orch = ChatOrchestrator()
    out = orch.run_chat(
        company_id="company-a",
        message="ei dhoroner kono besoy toh policy te nai....tahole tumi eta kivabe pele?",
        session_id=None,
        employee_id="oos-complaint-emp",
        trace_id="oos-complaint-1",
    )
    text = (out.get("response") or {}).get("message") or ""
    assert rag_called["n"] == 0
    assert "Termination" not in text
    assert "Notice Period" not in text
    assert "মিলছিল না" in text or "could not find" in text.lower()


def test_out_of_scope_message_bn() -> None:
    msg = build_out_of_scope_message("eid kobe", lang="bn", trace_id=None)
    assert "HR" in msg or "ছুটি" in msg or "পলিসি" in msg


def test_out_of_scope_message_rotates_wording() -> None:
    pool_msgs = {
        build_out_of_scope_message("eid kobe", lang="bn", trace_id=None)
        for _ in range(24)
    }
    assert len(pool_msgs) >= 2


def test_out_of_scope_avoids_immediate_repeat() -> None:
    first = build_out_of_scope_message("weather?", lang="en", trace_id=None)
    ctx = [f"Assistant: {first}"]
    second = build_out_of_scope_message("travel?", lang="en", context_lines=ctx, trace_id=None)
    assert second != first


@pytest.mark.django_db
def test_orchestrator_december_ki_din_hr_policy_intent_still_declines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM may label trivia as HR_POLICY; RAG must not run."""
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    rag_called = {"n": 0}

    def _rag(*_a, **_k):
        rag_called["n"] += 1
        return {
            "hit": True,
            "text": "Leave Policy\nTypes of Leave:\nCasual Leave",
            "sources": [],
            "mode": "rag",
        }

    monkeypatch.setattr("chat.services.orchestrator.try_hr_policy_rag", _rag)

    orch = ChatOrchestrator()

    def _force_hr_policy(self, message: str, trace_id: str):
        return {"intent": INTENT_HR_POLICY, "confidence": 0.95, "source": "test"}

    orch.intents.detect = _force_hr_policy.__get__(orch.intents, type(orch.intents))

    out = orch.run_chat(
        company_id="company-a",
        message="25th december eta ki din?",
        session_id=None,
        employee_id="oos-dec-emp",
        trace_id="oos-dec-1",
    )
    text = (out.get("response") or {}).get("message") or ""
    assert rag_called["n"] == 0
    assert "Casual Leave" not in text
    assert (
        "উত্তর" in text
        or "বাইরে" in text
        or "নয়" in text
        or "scope" in text.lower()
        or "outside" in text.lower()
    )
    assert "ছুটি" in text or "HR" in text or "leave" in text.lower()
