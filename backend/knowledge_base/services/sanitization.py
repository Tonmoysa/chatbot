"""Sanitize extracted document text and retrieved snippets before LLM context."""

from __future__ import annotations

import re
from typing import Final

# Patterns that often appear in prompt-injection attempts in documents / OCR.
_INJECTION_HINTS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions?", re.I),
    re.compile(r"disregard\s+(?:the\s+)?system\s+prompt", re.I),
    re.compile(r"you\s+are\s+now\s+(?:a|an)\s+", re.I),
    re.compile(r"<\s*/?\s*system\s*>", re.I),
    re.compile(r"\[\s*INST\s*\]", re.I),
)


def normalize_whitespace(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\x00", " ")
    t = re.sub(r"\r\n?", "\n", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def sanitize_for_indexing(text: str, *, max_chars: int | None = None) -> str:
    """Normalize + strip control chars; safe for storage and embedding."""
    t = normalize_whitespace(text)
    t = "".join(ch for ch in t if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    if max_chars is not None and len(t) > max_chars:
        t = t[:max_chars].rstrip()
    return t


def sanitize_retrieval_context(text: str, *, max_chars: int = 12_000) -> str:
    """
    Harden retrieved chunks before they are placed in an LLM prompt.
    Does not remove factual content; neutralizes obvious injection scaffolding.
    """
    t = sanitize_for_indexing(text, max_chars=max_chars)
    for pat in _INJECTION_HINTS:
        t = pat.sub("[redacted]", t)
    t = t.replace("```", "'''")
    return t


def preprocess_query(text: str) -> str:
    """Lightweight multilingual-safe query normalization for retrieval."""
    return normalize_whitespace(text)[:4000]
