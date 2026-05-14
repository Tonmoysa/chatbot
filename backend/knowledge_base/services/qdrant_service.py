"""Qdrant client lifecycle: lazy init, collection ensure, vector upsert/search."""

from __future__ import annotations

import logging
import threading
from typing import Any

from django.conf import settings

logger = logging.getLogger("hr_chatbot")

_client_lock = threading.Lock()


def _coerce_query_vector(vec: Any, *, trace_id: str = "") -> list[float]:
    """Ensure Qdrant receives a plain ``list[float]`` (not numpy/tensor)."""
    if vec is None:
        return []
    if isinstance(vec, (list, tuple)):
        try:
            return [float(x) for x in vec]
        except (TypeError, ValueError):
            return []
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
            return []
    logger.warning(
        "qdrant_vector_coerce_unsupported trace_id=%s type=%s",
        trace_id,
        type(vec).__name__,
    )
_client: Any = None


def _build_client():
    from qdrant_client import QdrantClient

    url = getattr(settings, "QDRANT_URL", "http://localhost:6333")
    timeout = float(getattr(settings, "QDRANT_TIMEOUT_SECONDS", 180.0))
    candidates: tuple[dict[str, Any], ...] = (
        {"url": url, "timeout": timeout, "prefer_grpc": False, "check_compatibility": False},
        {"url": url, "timeout": timeout, "prefer_grpc": False},
        {"url": url, "timeout": timeout},
    )
    last_err: TypeError | None = None
    for kwargs in candidates:
        try:
            return QdrantClient(**kwargs)
        except TypeError as exc:
            last_err = exc
            continue
    if last_err:
        raise last_err
    return QdrantClient(url=url, timeout=timeout)


def get_qdrant_client():
    """Lazy singleton Qdrant client (thread-safe)."""
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        _c = _build_client()
        globals()["_client"] = _c
        return _c


def vector_size() -> int:
    return int(getattr(settings, "QDRANT_VECTOR_SIZE", 1536))


def collection_name() -> str:
    return getattr(settings, "QDRANT_COLLECTION", "hr_policies_local")


def ensure_collection(*, trace_id: str = "") -> None:
    from qdrant_client.models import Distance, VectorParams

    client = get_qdrant_client()
    name = collection_name()
    vs = vector_size()
    try:
        if hasattr(client, "collection_exists"):
            exists = client.collection_exists(name)
        else:
            cols = client.get_collections().collections
            exists = any(c.name == name for c in cols)
    except Exception as exc:
        logger.warning(
            "qdrant_collection_check_failed trace_id=%s err=%s",
            trace_id,
            type(exc).__name__,
        )
        raise
    if exists:
        return
    try:
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=vs, distance=Distance.COSINE),
        )
        logger.info("qdrant_collection_created trace_id=%s name=%s", trace_id, name)
    except Exception as exc:
        logger.warning(
            "qdrant_collection_create_failed trace_id=%s err=%s",
            trace_id,
            type(exc).__name__,
        )
        raise


def upsert_points(points: list, *, trace_id: str = "") -> None:
    ensure_collection(trace_id=trace_id)
    client = get_qdrant_client()
    name = collection_name()
    batch = max(1, int(getattr(settings, "QDRANT_UPSERT_BATCH_SIZE", 128)))
    wait = bool(getattr(settings, "QDRANT_UPSERT_WAIT", False))
    total = len(points)
    for i in range(0, total, batch):
        chunk = points[i : i + batch]
        client.upsert(collection_name=name, points=chunk, wait=wait)
        logger.info(
            "qdrant_upsert trace_id=%s batch=%s size=%s total=%s wait=%s",
            trace_id,
            i // batch + 1,
            len(chunk),
            total,
            wait,
        )


def delete_by_document_id(document_db_id: int, *, trace_id: str = "") -> None:
    from qdrant_client.models import FieldCondition, Filter, MatchValue

    client = get_qdrant_client()
    name = collection_name()
    try:
        if hasattr(client, "collection_exists") and not client.collection_exists(name):
            return
    except Exception:
        pass
    try:
        client.delete(
            collection_name=name,
            points_selector=Filter(
                must=[
                    FieldCondition(
                        key="document_db_id",
                        match=MatchValue(value=document_db_id),
                    )
                ]
            ),
        )
    except Exception as exc:
        logger.warning(
            "qdrant_delete_failed trace_id=%s err=%s",
            trace_id,
            type(exc).__name__,
        )


def search_vectors(
    query_vector: list[float] | Any,
    *,
    limit: int = 8,
    score_threshold: float | None = None,
    payload_filter: Any | None = None,
    trace_id: str = "",
) -> list[Any]:
    qv = _coerce_query_vector(query_vector, trace_id=trace_id)
    if not qv:
        logger.warning("qdrant_search_skipped_empty_vector trace_id=%s", trace_id)
        return []

    ensure_collection(trace_id=trace_id)
    client = get_qdrant_client()
    name = collection_name()
    # qdrant-client >= 1.14 removed client.search(); use query_points instead.
    if hasattr(client, "query_points"):
        qp_kwargs: dict[str, Any] = {
            "collection_name": name,
            "query": qv,
            "limit": limit,
            "with_payload": True,
        }
        if score_threshold is not None:
            qp_kwargs["score_threshold"] = score_threshold
        if payload_filter is not None:
            qp_kwargs["query_filter"] = payload_filter
        resp = client.query_points(**qp_kwargs)
        points = list(getattr(resp, "points", []) or [])
    else:
        kwargs: dict[str, Any] = {
            "collection_name": name,
            "query_vector": qv,
            "limit": limit,
            "with_payload": True,
        }
        if score_threshold is not None:
            kwargs["score_threshold"] = score_threshold
        if payload_filter is not None:
            kwargs["query_filter"] = payload_filter
        points = list(client.search(**kwargs) or [])

    scores = [round(float(getattr(p, "score", 0.0) or 0.0), 4) for p in points[:8]]
    logger.info(
        "qdrant_vector_search trace_id=%s collection=%s dim=%s sample_head=%s hits=%s scores=%s",
        trace_id,
        name,
        len(qv),
        [round(x, 4) for x in qv[:3]],
        len(points),
        scores[:8],
    )
    if getattr(settings, "RAG_QUERY_DEBUG", False) and points:
        p0 = points[0]
        pid = getattr(p0, "id", None)
        pl = getattr(p0, "payload", None)
        pl_keys = list(pl.keys()) if isinstance(pl, dict) else type(pl).__name__
        logger.debug(
            "qdrant_query_debug trace_id=%s first_id=%s payload_keys=%s",
            trace_id,
            pid,
            pl_keys,
        )

    return points
