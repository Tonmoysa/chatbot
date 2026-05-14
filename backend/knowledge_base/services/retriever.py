"""Semantic retrieval against Qdrant with embedding."""

from __future__ import annotations

import logging
import time
from typing import Any

from django.conf import settings

from chat.services.llm_client import LLMClient
from chat.services.observability import log_step
from knowledge_base.services.qdrant_service import search_vectors
from knowledge_base.services.sanitization import preprocess_query

logger = logging.getLogger("hr_chatbot")


def _department_filter(department: str | None):
    if not department or not str(department).strip():
        return None
    try:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        return Filter(
            must=[
                FieldCondition(
                    key="department",
                    match=MatchValue(value=str(department).strip()),
                )
            ]
        )
    except Exception:
        return None


def _hit_score(hit: Any) -> float:
    return float(getattr(hit, "score", 0.0) or 0.0)


def _coerce_embedding_vector(vec: Any, *, trace_id: str) -> list[float] | None:
    """Match ingest path: plain ``list[float]`` for Qdrant ``query_points``."""
    if vec is None:
        return None
    if isinstance(vec, (list, tuple)):
        try:
            return [float(x) for x in vec]
        except (TypeError, ValueError):
            return None
    try:
        import numpy as np

        if isinstance(vec, np.ndarray):
            flat = np.asarray(vec, dtype=np.float64).reshape(-1)
            return [float(x) for x in flat.tolist()]
    except Exception:
        pass
    if hasattr(vec, "tolist"):
        try:
            raw = vec.tolist()
            if isinstance(raw, (int, float)):
                return [float(raw)]
            if isinstance(raw, list):
                return [float(x) for x in raw]
        except (TypeError, ValueError):
            return None
    logger.warning(
        "rag_embed_vector_coerce_unsupported trace_id=%s type=%s",
        trace_id,
        type(vec).__name__,
    )
    return None


def retrieve_for_query(
    query: str,
    trace_id: str,
    *,
    department: str | None = None,
    top_k: int | None = None,
    score_threshold: float | None = None,
) -> tuple[list[Any], int]:
    """
    Returns (scored_points, embedding_latency_ms for the query embedding call).

    Uses the same ``LLMClient.embed_texts`` path as ingestion. If the strict
    Qdrant ``score_threshold`` returns no points, retries without a server-side
    threshold and drops results below ``RAG_MIN_SIMILARITY`` (default 0.3).
    """
    if not getattr(settings, "KB_RAG_ENABLED", True):
        return [], 0

    q = preprocess_query(query)
    if not q:
        return [], 0

    llm = LLMClient()
    if not llm.is_configured():
        return [], 0

    t0 = time.perf_counter()
    vectors = llm.embed_texts([q], trace_id)
    emb_ms = int((time.perf_counter() - t0) * 1000)
    if not vectors:
        log_step(trace_id, "rag_embed_query_failed", {"ms": emb_ms})
        return [], emb_ms
    raw_vec = vectors[0]
    if raw_vec is None:
        log_step(trace_id, "rag_embed_query_failed", {"ms": emb_ms, "reason": "null_vector"})
        return [], emb_ms
    if isinstance(raw_vec, (list, tuple)) and len(raw_vec) == 0:
        log_step(trace_id, "rag_embed_query_failed", {"ms": emb_ms, "reason": "empty_vector"})
        return [], emb_ms

    qv = _coerce_embedding_vector(raw_vec, trace_id=trace_id)
    if not qv:
        log_step(trace_id, "rag_embed_vector_coerce_failed", {"ms": emb_ms})
        return [], emb_ms

    expected = int(getattr(settings, "QDRANT_VECTOR_SIZE", 0) or 0)
    if expected and len(qv) != expected:
        log_step(
            trace_id,
            "rag_query_vector_dim_mismatch",
            {"expected": expected, "got": len(qv), "ms": emb_ms},
        )
        logger.warning(
            "rag_query_vector_dim_mismatch trace_id=%s expected=%s got=%s",
            trace_id,
            expected,
            len(qv),
        )
        return [], emb_ms

    log_step(
        trace_id,
        "rag_query_vector_ready",
        {
            "dim": len(qv),
            "sample_head": [round(x, 5) for x in qv[:3]],
            "embedding_ms": emb_ms,
        },
    )

    k = int(top_k or getattr(settings, "RAG_TOP_K", 8))
    thr = score_threshold
    if thr is None:
        thr = float(getattr(settings, "RAG_SCORE_THRESHOLD", 0.45))
    min_sim = float(getattr(settings, "RAG_MIN_SIMILARITY", 0.3))

    flt = _department_filter(department)
    t1 = time.perf_counter()
    try:
        hits = search_vectors(
            qv,
            limit=k,
            score_threshold=thr,
            payload_filter=flt,
            trace_id=trace_id,
        )
        used_relaxed = False
        if not hits:
            log_step(
                trace_id,
                "rag_retrieval_relaxed_retry",
                {
                    "strict_threshold": thr,
                    "reason": "no_server_hits_with_threshold",
                },
            )
            hits = search_vectors(
                qv,
                limit=k,
                score_threshold=None,
                payload_filter=flt,
                trace_id=trace_id,
            )
            used_relaxed = True

        before_filter = len(hits)
        pre_scores = [round(_hit_score(h), 4) for h in hits[:8]]
        hits = [h for h in hits if _hit_score(h) >= min_sim]
        if before_filter and len(hits) < before_filter:
            log_step(
                trace_id,
                "rag_retrieval_min_similarity_filter",
                {
                    "min_similarity": min_sim,
                    "before": before_filter,
                    "after": len(hits),
                    "relaxed_pass": used_relaxed,
                    "scores_before_filter": pre_scores,
                },
            )
    except Exception as exc:
        log_step(
            trace_id,
            "rag_qdrant_search_failed",
            {
                "error": type(exc).__name__,
                "detail": (str(exc) or "")[:500],
            },
        )
        logger.warning(
            "rag_qdrant_search_failed trace_id=%s err=%s detail=%s",
            trace_id,
            type(exc).__name__,
            str(exc)[:300],
        )
        return [], emb_ms

    ret_ms = int((time.perf_counter() - t1) * 1000)
    log_step(
        trace_id,
        "rag_retrieval_done",
        {
            "embedding_ms": emb_ms,
            "retrieval_ms": ret_ms,
            "hits": len(hits),
            "scores": [round(_hit_score(h), 4) for h in hits[:8]],
            "collection": getattr(settings, "QDRANT_COLLECTION", ""),
        },
    )

    return hits, emb_ms
