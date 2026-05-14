"""Token-aware, markdown-friendly chunking for policy documents."""

from __future__ import annotations

import re
from dataclasses import dataclass

from knowledge_base.services.sanitization import normalize_whitespace

try:
    import tiktoken  # type: ignore

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENC = None


def count_tokens(text: str) -> int:
    if not text:
        return 0
    if _ENC is not None:
        return len(_ENC.encode(text))
    return max(1, len(text) // 4)


def _split_sentences(block: str) -> list[str]:
    if not block.strip():
        return []
    parts = re.split(r"(?<=[।.!?])\s+|\n+", block)
    return [p.strip() for p in parts if p.strip()]


@dataclass(frozen=True)
class TextChunk:
    text: str
    section_title: str
    chunk_index: int
    token_count: int


_HEADER_RE = re.compile(r"^(#{1,6}\s+.+)$", re.MULTILINE)


def split_by_markdown_sections(text: str) -> list[tuple[str, str]]:
    """Return list of (section_title, body) preserving order."""
    t = normalize_whitespace(text)
    if not t:
        return [("", "")]

    matches = list(_HEADER_RE.finditer(t))
    if not matches:
        return [("", t)]

    sections: list[tuple[str, str]] = []
    preamble = t[: matches[0].start()].strip()
    if preamble:
        sections.append(("", preamble))
    for i, m in enumerate(matches):
        title = m.group(1).strip().lstrip("#").strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(t)
        body = t[start:end].strip()
        sections.append((title, body))
    return sections


def _tail_by_tokens(text: str, max_tokens: int) -> str:
    if max_tokens <= 0 or not text:
        return ""
    low, high = 0, len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        cand = text[mid:].strip()
        if count_tokens(cand) <= max_tokens:
            best = cand
            high = mid - 1
        else:
            low = mid + 1
    return best


def _merged(carry: str, buf: list[str]) -> str:
    parts = ([carry] if carry else []) + buf
    return "\n".join(p for p in parts if p).strip()


def chunk_policy_text(
    text: str,
    *,
    target_tokens: int = 500,
    overlap_tokens: int = 100,
) -> list[TextChunk]:
    """
    Semantic-ish chunking: respect markdown headers, pack sentences until
    ~target_tokens, overlap tail of previous chunk for continuity.
    """
    sections = split_by_markdown_sections(text)
    raw_chunks: list[tuple[str, str]] = []
    tgt = max(128, target_tokens)
    ovl = max(0, overlap_tokens)

    for section_title, body in sections:
        sentences = _split_sentences(body) if body else []
        if not sentences and body:
            sentences = [body]
        carry = ""
        buf: list[str] = []

        def flush() -> None:
            nonlocal carry, buf
            merged = _merged(carry, buf)
            if merged:
                raw_chunks.append((section_title, merged))
            carry = _tail_by_tokens(merged, ovl) if ovl else ""
            buf = []

        for sent in sentences:
            trial = _merged(carry, buf + [sent])
            if count_tokens(trial) > tgt and (_merged(carry, buf)):
                flush()
            buf.append(sent)
        flush()

    out: list[TextChunk] = []
    for i, (sec, txt) in enumerate(raw_chunks):
        if not txt:
            continue
        out.append(
            TextChunk(
                text=txt,
                section_title=sec,
                chunk_index=len(out),
                token_count=count_tokens(txt),
            )
        )
    return out
