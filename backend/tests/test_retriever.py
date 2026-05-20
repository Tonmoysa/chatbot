"""Retriever: two-phase Qdrant search + embedding coercion."""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.django_db
def test_retrieve_relaxed_pass_applies_min_similarity(settings):
    settings.KB_RAG_ENABLED = True
    settings.QDRANT_VECTOR_SIZE = 3
    settings.RAG_TOP_K = 5
    settings.RAG_SCORE_THRESHOLD = 0.99
    settings.RAG_MIN_SIMILARITY = 0.3

    low = MagicMock()
    low.score = 0.42
    low.payload = {"chunk_text": "leave rules", "document_title": "Handbook"}

    with patch("knowledge_base.services.retriever.LLMClient") as m_llm:
        inst = m_llm.return_value
        inst.is_configured.return_value = True
        inst.is_embedding_configured.return_value = True
        inst.embed_texts.return_value = [[0.1, 0.2, 0.3]]
        with patch(
            "knowledge_base.services.retriever.search_vectors",
            side_effect=[[], [low]],
        ) as m_search:
            from knowledge_base.services.retriever import retrieve_for_query

            hits, emb = retrieve_for_query(
                "leave policy ki?",
                "t-relax",
                company_id="company-a",
            )

    assert emb >= 0
    assert len(hits) == 1
    assert hits[0].score == 0.42
    assert m_search.call_count == 2
    assert m_search.call_args_list[0].kwargs.get("payload_filter") is not None
    assert m_search.call_args_list[0].kwargs.get("score_threshold") == 0.99
    assert m_search.call_args_list[1].kwargs.get("score_threshold") is None


@pytest.mark.django_db
def test_retrieve_coerces_numpy_embedding(settings):
    pytest.importorskip("numpy")
    import numpy as np

    settings.KB_RAG_ENABLED = True
    settings.QDRANT_VECTOR_SIZE = 2
    settings.RAG_SCORE_THRESHOLD = 0.0
    settings.RAG_MIN_SIMILARITY = 0.0

    pt = MagicMock()
    pt.score = 0.9
    pt.payload = {"chunk_text": "x", "document_title": "D"}

    with patch("knowledge_base.services.retriever.LLMClient") as m_llm:
        inst = m_llm.return_value
        inst.is_configured.return_value = True
        inst.is_embedding_configured.return_value = True
        inst.embed_texts.return_value = [np.array([0.5, 0.6], dtype=np.float32)]
        with patch(
            "knowledge_base.services.retriever.search_vectors",
            return_value=[pt],
        ) as m_search:
            from knowledge_base.services.retriever import retrieve_for_query

            hits, _ = retrieve_for_query("q", "t-np", company_id="company-a")

    assert len(hits) == 1
    qv = m_search.call_args[0][0]
    assert len(qv) == 2
    assert abs(qv[0] - 0.5) < 1e-5 and abs(qv[1] - 0.6) < 1e-5
    assert isinstance(qv[0], float)


@pytest.mark.django_db
def test_retrieve_fallback_when_all_below_min_similarity(settings):
    """Low cosine scores must not zero out retrieval when Qdrant returned hits."""
    settings.KB_RAG_ENABLED = True
    settings.QDRANT_VECTOR_SIZE = 3
    settings.RAG_TOP_K = 5
    settings.RAG_SCORE_THRESHOLD = 0.0
    settings.RAG_MIN_SIMILARITY = 0.24

    h1 = MagicMock()
    h1.score = 0.19
    h1.payload = {"chunk_text": "carry forward up to 14 days", "document_title": "Leave"}
    h2 = MagicMock()
    h2.score = 0.08
    h2.payload = {"chunk_text": "annual leave entitlement", "document_title": "Leave"}
    h3 = MagicMock()
    h3.score = 0.12
    h3.payload = {"chunk_text": "other policy", "document_title": "Misc"}

    with patch("knowledge_base.services.retriever.LLMClient") as m_llm:
        inst = m_llm.return_value
        inst.is_configured.return_value = True
        inst.is_embedding_configured.return_value = True
        inst.embed_texts.return_value = [[0.1, 0.2, 0.3]]
        with patch(
            "knowledge_base.services.retriever.search_vectors",
            return_value=[h2, h1, h3],
        ):
            from knowledge_base.services.retriever import retrieve_for_query

            hits, _ = retrieve_for_query(
                "How many annual leaves can be carried forward?",
                "t-fallback",
                company_id="company-a",
            )

    assert len(hits) >= 3
    assert hits[0].score == 0.19
    assert hits[1].score == 0.12
