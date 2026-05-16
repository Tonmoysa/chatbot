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


# Lightweight HR-topic hints concatenated ONLY for the retrieval query embedding —
# user-facing wording in RAG prompts stays untouched (same original question).
_EMBEDDING_TOPIC_HINTS: Final[tuple[tuple[re.Pattern[str], tuple[str, ...]], ...]] = (
    (
        re.compile(
            r"(carry\s*-?\s*forward|carried\s+forward|carrying\s+forward|\bcarry\b\s*-?\s*over\b|carryover|rollover|opening\s*balance\b|unused\s+leave|"
            r"annual\s+(?:credit|leaves?|leave|vacation|days)\b|\bpto\b|\bavl\b|\bleave\s+accrual\b|"
            r"vacation\b.*(?:accru|balance)\b|"
            r"ছুটি.*(?:ফরওয়ার্ড|ফরয়াওয়ার্ড|ক্যারি|বয়ে\s*যাবে|স্থানান্তর|জমা)|বছর\b.*ছুটি|বছরান্ত\b.*ছুটি|"
            r"কত\b.*ছুটি|ছুটি.*(?:কত|ফর|S|মেয়াদ|মেয়াদ)|kotodin\b|koydin\b|kondo?in\b|\bbaki\b.*ছুটি|ছুটি.*\bbaki\b)",
            re.I | re.UNICODE,
        ),
        (
            "annual leave entitlement",
            "vacation PTO accrued leave balance",
            "leave carry forward carryover rollover forfeiture expiry",
        ),
    ),
    (
        re.compile(
            r"\b(?:sick|casual|bereavement|maternity|paternity|marriage|study)\s+leave\b|\blwop\b|"
            r"উ\/এল\b|উ\s*\/?\s*এল\b|বিশেষ ছুটি|ক্যাশুয়াল\b",
            re.I | re.UNICODE,
        ),
        ("sick casual special leave entitlement", "LWOP unpaid leave policy"),
    ),
    (
        re.compile(r"\b(?:remote\s+work|telework\b|wfh\b|ওএফএইচ)\b", re.I | re.UNICODE),
        ("work from home remote work policy",),
    ),
    (
        re.compile(
            r"\bacceptable\s+use\b|\bpersonal\b.*illegal.*assets\b|"
            r"\bcompany\s+(?:devices|equipment|computers|laptops)\b|\bউপস্থিতি\b|\battendance\b|"
            r"\bflex\s*time\b|\bbio\b.*time\b|\bflexible\b.*working\b|\bfingerprint\b",
            re.I | re.UNICODE,
        ),
        ("acceptable use company assets", "attendance tardiness policy"),
    ),
    (
        re.compile(
            r"\b(?:policy|rules?\b.*regulations?|handbook|guideline\b.*HR)\b|"
            r"ছুটি\s*(?:শর্ত|নিয়ম|পলিসি)|হ্যান্ডবুক|নিয়ম\b",
            re.I | re.UNICODE,
        ),
        ("human resources employee handbook excerpt",),
    ),
)


def _unique_join_phrases(phrases: tuple[str, ...], *, hint_cap: int) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for phrase in phrases:
        p = " ".join(phrase.split())
        if not p or p.lower() in seen:
            continue
        seen.add(p.lower())
        out.append(p)
        if sum(len(x) + 1 for x in out) >= hint_cap:
            break
    return ". ".join(out)


def hr_retrieval_hint_line(query_normalized: str, *, phrase_cap_chars: int = 650) -> str:
    phrases: list[str] = []
    for pat, tup in _EMBEDDING_TOPIC_HINTS:
        if pat.search(query_normalized):
            phrases.append(_unique_join_phrases(tup, hint_cap=max(phrase_cap_chars // 2, 120)))
        if phrases and sum(len(p) + 2 for p in phrases) >= phrase_cap_chars:
            break
    return ". ".join(p for p in phrases if p.strip()).strip()


def build_retrieval_embedding_text(query: str, *, max_chars: int = 3800) -> str:
    """
    Text passed to embedding for vector search — may include multilingual-safe HR
    paraphrases to improve recall versus English-heavy policy corpuses.

    Caller must ensure the conversational RAG pathway still forwards the user's
    original question to grounded_user_prompt unchanged.
    """
    base = normalize_whitespace(query)[:max_chars]
    if not base:
        return ""
    hint = hr_retrieval_hint_line(base[:2000])
    if not hint:
        return base
    suffix = (
        "[HR handbook retrieval context]\n"
        + hint[: min(len(hint), 900)]
    ).strip()
    sep = "\n\n---\n\n"
    room = max(0, max_chars - len(sep) - len(suffix))
    head = base[:room] if room < len(base) else base
    return (head + sep + suffix).strip()[:max_chars]
