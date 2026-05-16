"""Document ingestion: extract → sanitize → chunk → embed → Qdrant + ORM."""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from django.conf import settings
from django.utils import timezone
from qdrant_client.models import PointStruct

from chat.services.document_reader import DocumentExtractResult, extract_text_from_upload
from chat.services.llm_client import LLMClient
from chat.services.observability import log_step
from chat.services.translator import detect_user_language
from knowledge_base.models import DocumentStatus, DocumentType, KnowledgeChunk, KnowledgeDocument
from knowledge_base.services.chunker import chunk_policy_text, count_tokens
from knowledge_base.services.qdrant_service import delete_by_document_id, upsert_points
from knowledge_base.services.sanitization import sanitize_for_indexing

logger = logging.getLogger("hr_chatbot")


def _indexed_chunk_surface(doc_title: str, section_title: str, chunk_body: str) -> str:
    """Prefix document + section context for embedding and citation (retrieval alignment)."""
    d = (doc_title or "").strip()
    s = (section_title or "").strip()
    b = (chunk_body or "").strip()
    bits = [f"Policy title: {d}"] if d else []
    if s:
        bits.append(f"Section: {s}")
    if bits:
        return ("; ".join(bits) + "\n\n" + b).strip()
    return b


def read_policy_file(
    *,
    data: bytes,
    filename: str | None,
    content_type: str | None,
    max_chars: int | None = None,
) -> DocumentExtractResult:
    """TXT/Markdown inline decode; PDF/images via ``document_reader``."""
    cap = max_chars or int(getattr(settings, "KB_MAX_EXTRACT_CHARS", 200_000))
    name = (filename or "").lower()
    if name.endswith((".md", ".markdown", ".txt")):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        text = sanitize_for_indexing(text, max_chars=cap)
        return DocumentExtractResult(text=text, warnings=[], source="text_file")
    return extract_text_from_upload(
        filename=filename,
        content_type=content_type,
        data=data,
        max_chars=cap,
    )


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ingest_bytes(
    *,
    data: bytes,
    title: str,
    filename: str | None,
    content_type: str | None,
    document_type: str = DocumentType.POLICY,
    source_path: str = "",
    uploaded_by_id: int | None,
    trace_id: str,
    metadata: dict[str, Any] | None = None,
    reindex: bool = False,
) -> dict[str, Any]:
    """
    Persist document + chunks, upsert vectors. Returns status dict for API/command.
    """
    t0 = time.perf_counter()
    meta = dict(metadata or {})
    max_upload = int(getattr(settings, "KB_MAX_UPLOAD_BYTES", 26_214_400))  # 25 MiB
    if len(data) > max_upload:
        raise ValueError("upload_too_large")

    chk = _checksum(data)
    existing = KnowledgeDocument.objects.filter(checksum=chk).first()
    if existing and not reindex and existing.status == DocumentStatus.INDEXED:
        log_step(trace_id, "kb_ingest_deduped", {"document_id": existing.pk})
        return {
            "document_id": str(existing.pk),
            "chunks_created": existing.total_chunks,
            "status": "deduped",
            "checksum": chk,
        }

    extracted = read_policy_file(
        data=data, filename=filename, content_type=content_type
    )
    text = sanitize_for_indexing(extracted.text or "")
    if not text.strip():
        doc = KnowledgeDocument.objects.create(
            title=title,
            checksum=chk,
            document_type=document_type,
            source_path=source_path,
            uploaded_by_id=uploaded_by_id,
            status=DocumentStatus.FAILED,
            metadata={**meta, "warnings": extracted.warnings},
        )
        return {
            "document_id": str(doc.pk),
            "chunks_created": 0,
            "status": "failed",
            "error": "empty_extract",
        }

    if existing and reindex:
        delete_by_document_id(existing.pk, trace_id=trace_id)
        KnowledgeChunk.objects.filter(document=existing).delete()
        doc = existing
        doc.title = title
        doc.status = DocumentStatus.PROCESSING
        doc.metadata = {**meta, "warnings": extracted.warnings, "source": extracted.source}
        doc.total_chunks = 0
        doc.save()
    else:
        doc = KnowledgeDocument.objects.create(
            title=title,
            checksum=chk,
            document_type=document_type,
            source_path=source_path,
            uploaded_by_id=uploaded_by_id,
            status=DocumentStatus.PROCESSING,
            metadata={**meta, "warnings": extracted.warnings, "source": extracted.source},
        )

    chunks = chunk_policy_text(
        text,
        target_tokens=int(getattr(settings, "KB_CHUNK_TARGET_TOKENS", 500)),
        overlap_tokens=int(getattr(settings, "KB_CHUNK_OVERLAP_TOKENS", 100)),
    )
    if not chunks:
        doc.status = DocumentStatus.FAILED
        doc.save(update_fields=["status"])
        return {"document_id": str(doc.pk), "chunks_created": 0, "status": "failed", "error": "no_chunks"}

    surface_texts = [
        _indexed_chunk_surface(doc.title, ch.section_title, ch.text) for ch in chunks
    ]
    texts = surface_texts

    embed_batches: list[list[float]] = []

    llm = LLMClient()
    batch = int(getattr(settings, "EMBED_BATCH_SIZE", 64))
    for i in range(0, len(texts), batch):
        part = texts[i : i + batch]
        t_emb = time.perf_counter()
        vecs = llm.embed_texts(part, trace_id) if llm.is_configured() else None
        emb_ms = int((time.perf_counter() - t_emb) * 1000)
        log_step(
            trace_id,
            "kb_ingest_embed_batch",
            {"offset": i, "size": len(part), "ms": emb_ms, "ok": bool(vecs)},
        )
        if not vecs or len(vecs) != len(part):
            doc.status = DocumentStatus.FAILED
            doc.save(update_fields=["status"])
            return {
                "document_id": str(doc.pk),
                "chunks_created": 0,
                "status": "failed",
                "error": "embedding_failed",
            }
        embed_batches.extend(vecs)

    KnowledgeChunk.objects.filter(document=doc).delete()
    delete_by_document_id(doc.pk, trace_id=trace_id)

    upload_ts = timezone.now().isoformat()
    points: list[PointStruct] = []
    lang = detect_user_language(text[:500])
    dept = str(meta.get("department") or "")
    policy_type = str(meta.get("policy_type") or doc.document_type)

    for i, ch in enumerate(chunks):
        pid = str(uuid4())
        surface = surface_texts[i]
        payload = {
            "chunk_text": surface[:8000],
            "document_title": doc.title,
            "source_document": doc.title,
            "section_title": ch.section_title or "",
            "chunk_index": i,
            "policy_type": policy_type,
            "department": dept,
            "language": lang,
            "upload_timestamp": upload_ts,
            "created_at": upload_ts,
            "document_db_id": doc.pk,
        }
        points.append(PointStruct(id=pid, vector=embed_batches[i], payload=payload))

    KnowledgeChunk.objects.bulk_create(
        [
            KnowledgeChunk(
                document=doc,
                chunk_index=i,
                chunk_text=surface_texts[i],
                token_count=count_tokens(surface_texts[i]),
                qdrant_point_id=str(points[i].id),
                language=lang,
                metadata={
                    "section_title": ch.section_title,
                    "policy_type": policy_type,
                },
            )
            for i, ch in enumerate(chunks)
        ]
    )

    try:
        upsert_points(points, trace_id=trace_id)
    except Exception:
        logger.exception("kb_ingest_qdrant_failed trace_id=%s doc_id=%s", trace_id, doc.pk)
        KnowledgeChunk.objects.filter(document=doc).delete()
        doc.total_chunks = 0
        doc.status = DocumentStatus.FAILED
        doc.save(update_fields=["status", "total_chunks"])
        return {
            "document_id": str(doc.pk),
            "chunks_created": 0,
            "status": "failed",
            "error": "qdrant_upsert_failed",
        }

    doc.total_chunks = len(chunks)
    doc.status = DocumentStatus.INDEXED
    doc.save(update_fields=["total_chunks", "status"])

    total_ms = int((time.perf_counter() - t0) * 1000)
    log_step(
        trace_id,
        "kb_ingest_done",
        {"document_id": doc.pk, "chunks": len(chunks), "ms": total_ms},
    )
    return {
        "document_id": str(doc.pk),
        "chunks_created": len(chunks),
        "status": "indexed",
        "ingestion_ms": total_ms,
    }


def ingest_path(
    path: Path,
    *,
    trace_id: str,
    reindex: bool,
    metadata: dict[str, Any] | None,
    uploaded_by_id: int | None = None,
) -> dict[str, Any]:
    data = path.read_bytes()
    return ingest_bytes(
        data=data,
        title=path.stem,
        filename=path.name,
        content_type="application/octet-stream",
        source_path=str(path.resolve()),
        trace_id=trace_id,
        reindex=reindex,
        metadata={**(metadata or {}), "path": str(path)},
        uploaded_by_id=uploaded_by_id,
    )
