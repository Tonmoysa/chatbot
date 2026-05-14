from unittest.mock import MagicMock, patch

import pytest

from knowledge_base.services.rag_pipeline import try_hr_policy_rag


@pytest.mark.django_db
def test_try_hr_policy_rag_returns_none_when_disabled(settings):
    settings.KB_RAG_ENABLED = False
    assert try_hr_policy_rag("leave policy?", "t1") is None


@pytest.mark.django_db
def test_try_hr_policy_rag_runs_for_full_handbook_phrase(settings):
    """Broad 'show all rules' queries must still hit RAG (no static handbook skip)."""
    settings.KB_RAG_ENABLED = True
    hit = MagicMock()
    hit.payload = {"chunk_text": "Section A …", "section_title": "Overview"}
    hit.score = 0.7
    with patch(
        "knowledge_base.services.rag_pipeline.retrieve_for_query",
        return_value=([hit], 1),
    ):
        with patch("knowledge_base.services.rag_pipeline.LLMClient") as m_llm:
            inst = m_llm.return_value
            inst.is_configured.return_value = True
            inst.chat_json.return_value = {
                "answer": "Here are highlights from the indexed policies.",
                "insufficient_evidence": False,
            }
            out = try_hr_policy_rag("show me all rules and regulations", "t2")
    assert out and out.get("hit")
    assert "highlights" in out["text"].lower()


@pytest.mark.django_db
def test_try_hr_policy_rag_grounded_answer(settings):
    settings.KB_RAG_ENABLED = True
    hit = MagicMock()
    hit.payload = {"chunk_text": "LWOP requires manager approval.", "section_title": "Leave"}
    hit.score = 0.9
    with patch(
        "knowledge_base.services.rag_pipeline.retrieve_for_query",
        return_value=([hit], 12),
    ):
        with patch("knowledge_base.services.rag_pipeline.LLMClient") as m_llm:
            inst = m_llm.return_value
            inst.is_configured.return_value = True
            inst.chat_json.return_value = {
                "answer": "LWOP needs manager approval per handbook.",
                "insufficient_evidence": False,
            }
            out = try_hr_policy_rag("What about LWOP?", "t3")
    assert out and out.get("hit")
    assert "LWOP" in out["text"]
    assert out["sources"] and out["sources"][0]["score"] == 0.9


@pytest.mark.django_db
def test_try_hr_policy_rag_insufficient_evidence_message(settings):
    settings.KB_RAG_ENABLED = True
    hit = MagicMock()
    hit.payload = {"chunk_text": "Unrelated text about parking.", "document_title": "Doc"}
    hit.score = 0.88
    with patch(
        "knowledge_base.services.rag_pipeline.retrieve_for_query",
        return_value=([hit], 5),
    ):
        with patch("knowledge_base.services.rag_pipeline.LLMClient") as m_llm:
            inst = m_llm.return_value
            inst.is_configured.return_value = True
            inst.chat_json.return_value = {
                "answer": "",
                "insufficient_evidence": True,
            }
            out = try_hr_policy_rag("parking policy?", "t4")
    assert out and "could not find" in out["text"].lower()
