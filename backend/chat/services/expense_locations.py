"""
Dhaka-area location cleanup and typo suggestions for expense travel rows.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

# Common CRM / user route labels (Banglish spellings included).
KNOWN_EXPENSE_LOCATIONS: frozenset[str] = frozenset(
    {
        "mirpur",
        "uttora",
        "uttara",
        "badda",
        "motijheel",
        "motejheel",
        "motijeel",
        "gulshan",
        "banani",
        "baridhara",
        "dhanmondi",
        "farmgate",
        "shahbagh",
        "office",
        "home",
        "basha",
        "road 7",
        "mirpur 1",
        "mirpur 10",
    }
)

_LOCATION_TRAILING_PUNCT_RE = re.compile(r"[.,;!?]+$")
_EXPLICIT_OTHER_RE = re.compile(
    r"\b(other|misc|অন্যান্য|অন্য)\b",
    re.I,
)


def strip_location_punctuation(label: str) -> str:
    return _LOCATION_TRAILING_PUNCT_RE.sub("", (label or "").strip()).strip()


def category_explicitly_other(*texts: str) -> bool:
    for text in texts:
        if text and _EXPLICIT_OTHER_RE.search(text):
            return True
    return False


def location_context_from_rows(rows: list[dict[str, Any]]) -> frozenset[str]:
    ctx: set[str] = set(KNOWN_EXPENSE_LOCATIONS)
    for row in rows:
        for key in ("from_location", "to_location"):
            val = strip_location_punctuation(str(row.get(key) or ""))
            if len(val) >= 3 and val.lower() in KNOWN_EXPENSE_LOCATIONS:
                ctx.add(val.lower())
    return frozenset(ctx)


def suggest_location_correction(
    label: str,
    *,
    context: frozenset[str] | None = None,
) -> str | None:
    """
    Return a corrected place name for likely typos (e.g. irpur → mirpur).
    """
    cleaned = strip_location_punctuation(label)
    if not cleaned or len(cleaned) < 3:
        return None

    key = cleaned.lower()
    pool = frozenset((context or frozenset()) | KNOWN_EXPENSE_LOCATIONS)
    if key in {c.lower() for c in KNOWN_EXPENSE_LOCATIONS}:
        return None

    best_match: str | None = None
    best_ratio = 0.0
    for candidate in pool:
        ratio = difflib.SequenceMatcher(None, key, candidate.lower()).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = candidate

    if not best_match or best_ratio < 0.72:
        return None
    if abs(len(key) - len(best_match.lower())) > 2:
        return None
    if key == best_match.lower():
        return None
    return best_match


def detect_travel_location_typos(
    row: dict[str, Any],
    *,
    context: frozenset[str] | None = None,
) -> list[dict[str, str]]:
    """Return typo candidates without mutating the row."""
    typos: list[dict[str, str]] = []
    ctx = context or location_context_from_rows([row])

    for field in ("from_location", "to_location"):
        raw = str(row.get(field) or "").strip()
        if not raw:
            continue
        cleaned = strip_location_punctuation(raw)
        suggestion = suggest_location_correction(cleaned, context=ctx)
        if suggestion and suggestion.lower() != cleaned.lower():
            typos.append(
                {
                    "field": field,
                    "original": cleaned,
                    "suggestion": suggestion,
                }
            )
    return typos


def normalize_travel_locations_on_row(
    row: dict[str, Any],
    *,
    context: frozenset[str] | None = None,
) -> list[str]:
    """
    Strip punctuation and apply high-confidence typo fixes on a travel row.
    Returns human-readable warning strings (empty when nothing changed).
    """
    warnings: list[str] = []
    ctx = context or location_context_from_rows([row])
    cat = str(row.get("category") or "")

    for field, role in (("from_location", "From"), ("to_location", "To")):
        raw = str(row.get(field) or "")
        if not raw:
            continue
        cleaned = strip_location_punctuation(raw)
        if cleaned != raw:
            row[field] = cleaned
            raw = cleaned

        suggestion = suggest_location_correction(cleaned, context=ctx)
        if suggestion and suggestion.lower() != cleaned.lower():
            warnings.append(
                f"**{cat}** · {role}: **{cleaned}** — আপনি কি **{suggestion}** বোঝাচ্ছেন?"
            )
            row[field] = suggestion

    return warnings
