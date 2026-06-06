"""
Canonical synonym normalization for expense line items.

Maps informal Bangla/Banglish/English phrases to stable CRM categories.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense_locations import (
    category_explicitly_other,
    strip_location_punctuation,
)
from chat.services.expense_extraction import (
    EXPENSE_CATEGORIES,
    is_travel_category,
    normalize_category,
)

# Extra aliases beyond expense_extraction._CATEGORY_ALIASES (merged at runtime).
_EXTRA_CATEGORY_ALIASES: dict[str, str] = {
    "খাওয়া": "Lunch",
    "খাবার": "Lunch",
    "খাওয়া": "Lunch",
    "খাওয়ার": "Lunch",
    "ভাত": "Lunch",
    "দুপুরের খাবার": "Lunch",
    "নাস্তা": "Snack",
    "নাশতা": "Snack",
    "রিক্সা": "Rickshaw",
    "রিকসা": "Rickshaw",
    "ভাড়া": "Bus",
    "ভাড়া": "Bus",
    "যাতায়াত": "Bus",
    "যানবাহন": "Bus",
    "সিএনজি": "CNG",
    "অটো": "CNG",
    "অটোরিকশা": "CNG",
    "মেট্রো রেল": "Metro Rail",
    "মেট্রোরেল": "Metro Rail",
    "অন্যান্য": "Other",
    "অন্য": "Other",
}

_AMOUNT_RE = re.compile(
    r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?\s*(?:টাকা|taka|tk|tks|bdt|৳)?(?!\d)",
    re.I,
)


def _low(text: str) -> str:
    return (text or "").strip().lower()


def normalize_category_label(raw: Any) -> str:
    """Normalize a category token to a canonical CRM label."""
    key = _low(str(raw or ""))
    key = re.sub(r"\s+", " ", key)
    if not key:
        return "Other"
    if key in _EXTRA_CATEGORY_ALIASES:
        return _EXTRA_CATEGORY_ALIASES[key]
    return normalize_category(key)


def normalize_amount(value: Any) -> float | None:
    """Parse a numeric amount from a scalar or short phrase."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        amt = float(value)
        return amt if amt > 0 else None
    text = str(value).strip()
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    try:
        whole = m.group(1)
        frac = m.group(2) or ""
        amt = float(f"{whole}.{frac}" if frac else whole)
        return amt if amt > 0 else None
    except (TypeError, ValueError, IndexError):
        return None


def normalize_location(value: Any) -> str:
    return strip_location_punctuation(str(value or ""))


def resolve_llm_expense_category(
    raw: Any,
    *,
    message: str = "",
    notes: str = "",
) -> str:
    """LLM category — never default unknown lines to Other."""
    if raw is None or not str(raw).strip():
        return ""
    cat = normalize_category_label(raw)
    if cat == "Other" and not category_explicitly_other(
        message, notes, str(raw or "")
    ):
        return ""
    return cat


def expense_category_needs_clarification(
    category: str,
    *,
    message: str = "",
) -> bool:
    """True when review must not proceed without a real category."""
    cat = (category or "").strip()
    if not cat:
        return True
    if cat == "Other" and not category_explicitly_other(message):
        return True
    return False


def normalize_expense_line(row: dict[str, Any]) -> dict[str, Any]:
    """Return a cleaned expense line dict ready for validation/CRM."""
    out = dict(row)
    raw_cat = out.get("category")
    if raw_cat is None or not str(raw_cat).strip():
        out["category"] = ""
    else:
        out["category"] = normalize_category_label(raw_cat)
    amt = normalize_amount(out.get("amount"))
    if amt is not None:
        out["amount"] = amt
    out["from_location"] = normalize_location(out.get("from_location"))
    out["to_location"] = normalize_location(out.get("to_location"))
    if out.get("notes") is not None:
        out["notes"] = str(out.get("notes") or "").strip()
    return out


def normalize_expense_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize every collected line in place and return the list."""
    normalized: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        cleaned = normalize_expense_line(row)
        cat = str(cleaned.get("category") or "").strip()
        if cat and cat not in EXPENSE_CATEGORIES:
            cleaned["category"] = "Other"
        normalized.append(cleaned)
    return normalized


def normalize_pending_line(pending: dict[str, Any]) -> dict[str, Any]:
    """Normalize an in-progress pending_line before finalize."""
    out = dict(pending)
    if out.get("category"):
        out["category"] = normalize_category_label(out["category"])
    amt = normalize_amount(out.get("amount"))
    if amt is not None:
        out["amount"] = amt
    out["from_location"] = normalize_location(out.get("from_location"))
    out["to_location"] = normalize_location(out.get("to_location"))
    return out


def pending_line_ready(pending: dict[str, Any]) -> bool:
    """True when pending line has enough data to finalize into items[]."""
    cat = str(pending.get("category") or "").strip()
    try:
        amt = float(pending.get("amount") or 0)
    except (TypeError, ValueError):
        return False
    if not cat or amt <= 0:
        return False
    if is_travel_category(cat):
        frm = str(pending.get("from_location") or "").strip()
        to = str(pending.get("to_location") or "").strip()
        return bool(frm and to)
    return True
