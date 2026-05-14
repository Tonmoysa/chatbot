"""Orchestrator-facing RAG entry: retrieve → ground → citations."""

from __future__ import annotations

import logging
import time
from typing import Any

from django.conf import settings

# from chat.services.rules_handbook import wants_full_handbook  # disabled: RAG-only policy answers
from chat.services.llm_client import LLMClient
from chat.services.observability import log_step
from knowledge_base.services.citation_builder import build_sources
from knowledge_base.services.prompts import GROUNDED_SYSTEM, grounded_user_prompt
from knowledge_base.services.retriever import retrieve_for_query
from knowledge_base.services.sanitization import sanitize_retrieval_context

logger = logging.getLogger("hr_chatbot")

_NOT_FOUND = "I could not find this policy in the handbook."


def hr_policy_not_found_message() -> str:
    """User-visible text when RAG is enabled but no grounded answer is produced."""
    custom = getattr(settings, "KB_RAG_NOT_FOUND_MESSAGE", None)
    if isinstance(custom, str) and custom.strip():
        return custom.strip()
    return _NOT_FOUND


def _payload_from_hit(hit: Any) -> dict[str, Any]:
    pl = getattr(hit, "payload", None) or {}
    if isinstance(pl, dict):
        return pl
    md = getattr(pl, "model_dump", None)
    if callable(md):
        dumped = md()
        if isinstance(dumped, dict):
            return dumped
    return {}


def try_hr_policy_rag(
    message: str,
    trace_id: str,
    *,
    department: str | None = None,
    llm: LLMClient | None = None,
) -> dict[str, Any] | None:
    """
    Returns a dict with keys: hit (bool), text (str), sources (list), mode ('rag').
    Returns None when RAG is disabled or hard infrastructure failure before retrieval.
    """
    if not getattr(settings, "KB_RAG_ENABLED", True):
        return None

    # Static handbook "show all" bypass removed — broad queries also go through retrieve + LLM.
    # if wants_full_handbook(message or ""):
    #     log_step(trace_id, "rag_skip_full_handbook", {})
    #     return None

    msg = (message or "").strip()
    if not msg:
        return None

    t0 = time.perf_counter()
    hits, _emb_ms = retrieve_for_query(
        msg,
        trace_id,
        department=department,
        top_k=int(getattr(settings, "RAG_TOP_K", 8)),
        score_threshold=float(getattr(settings, "RAG_SCORE_THRESHOLD", 0.45)),
    )
    if not hits:
        log_step(trace_id, "rag_no_hits", {"ms": int((time.perf_counter() - t0) * 1000)})
        return None

    blocks: list[str] = []
    max_ctx = int(getattr(settings, "RAG_MAX_CONTEXT_CHARS", 10_000))
    running = 0
    for h in hits:
        payload = _payload_from_hit(h)
        if not payload:
            continue
        title = str(payload.get("section_title") or payload.get("document_title") or "Policy")
        body = sanitize_retrieval_context(str(payload.get("chunk_text") or ""), max_chars=4000)
        if not body:
            continue
        piece = f"[{title}]\n{body}"
        if running + len(piece) > max_ctx:
            break
        blocks.append(piece)
        running += len(piece)

    if not blocks:
        log_step(trace_id, "rag_empty_context", {})
        return None

    client = llm or LLMClient()
    if not client.is_configured():
        return None

    user_prompt = grounded_user_prompt(user_query=msg, evidence_blocks=blocks)
    t1 = time.perf_counter()
    parsed = client.chat_json(
        system_prompt=GROUNDED_SYSTEM,
        user_prompt=user_prompt,
        trace_id=trace_id,
    )
    gen_ms = int((time.perf_counter() - t1) * 1000)
    log_step(
        trace_id,
        "rag_generation_done",
        {"ms": gen_ms, "ok": bool(parsed)},
    )

    if not isinstance(parsed, dict):
        return None

    insufficient = bool(parsed.get("insufficient_evidence"))
    answer = str(parsed.get("answer") or "").strip()
    if insufficient or not answer:
        answer = hr_policy_not_found_message()

    sources = build_sources(hits)
    return {
        "hit": True,
        "text": answer,
        "sources": sources,
        "mode": "rag",
    }
