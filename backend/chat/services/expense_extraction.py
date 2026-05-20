"""
Structured multi-item expense extraction (BN / EN / Banglish).

Deterministic parsing — no LLM approval or outcome decisions here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Canonical categories for CRM payload
EXPENSE_CATEGORIES: tuple[str, ...] = (
    "Lunch",
    "Snack",
    "Bus",
    "Rickshaw",
    "Train",
    "Bike",
    "CNG",
    "Metro Rail",
    "Other",
)

_CATEGORY_ALIASES: dict[str, str] = {
    "lunch": "Lunch",
    "lanch": "Lunch",
    "খাওয়া": "Lunch",
    "খাবার": "Lunch",
    "snack": "Snack",
    "snacks": "Snack",
    "স্ন্যাক": "Snack",
    "bus": "Bus",
    "বাস": "Bus",
    "rickshaw": "Rickshaw",
    "riksha": "Rickshaw",
    "রিকশা": "Rickshaw",
    "train": "Train",
    "ট্রেন": "Train",
    "bike": "Bike",
    "সাইকেল": "Bike",
    "bicycle": "Bike",
    "cng": "CNG",
    "auto": "CNG",
    "সিএনজি": "CNG",
    "metro": "Metro Rail",
    "metro rail": "Metro Rail",
    "metrorail": "Metro Rail",
    "মেট্রো": "Metro Rail",
    "uber": "Other",
    "cab": "Other",
    "taxi": "Other",
    "food": "Lunch",
    "meal": "Lunch",
    "transport": "Bus",
    "travel": "Bus",
}

_AMOUNT_RE = re.compile(
    r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?\s*(?:টাকা|taka|tk|tks|bdt|৳)?(?!\d)",
    re.I,
)

_CATEGORY_TOKEN = (
    r"(?:lunch|lanch|snacks?|bus|rickshaw|riksha|train|bike|bicycle|"
    r"cng|auto|metro(?:\s*rail)?|uber|cab|taxi|food|meal|transport|travel|"
    r"খাওয়া|খাবার|বাস|রিকশা|ট্রেন|সাইকেল|সিএনজি|মেট্রো)"
)

# category ... amount OR amount ... category
_ITEM_PAIR_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})"
    rf"(?:\s*(?:e|te|এ|তে|a|er|er|for|on))?\s*"
    rf"(?P<amt>{_AMOUNT_RE.pattern})",
    re.I,
)
_ITEM_PAIR_REV_RE = re.compile(
    rf"(?P<amt>{_AMOUNT_RE.pattern})\s*"
    rf"(?:টাকা|taka|tk|bdt)?\s*"
    rf"(?P<cat>{_CATEGORY_TOKEN})",
    re.I,
)

_ROUTE_RE = re.compile(
    r"(?:from|theke|থেকে)\s+([a-zA-Z0-9\u0980-\u09FF\s]{2,40}?)\s+"
    r"(?:to|theke|যাওয়া|e|এ)\s+([a-zA-Z0-9\u0980-\u09FF\s]{2,40}?)\s+"
    rf"(?P<cat>{_CATEGORY_TOKEN})",
    re.I,
)


@dataclass
class ExpenseLineItem:
    category: str
    amount: float
    from_location: str = ""
    to_location: str = ""
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "amount": float(self.amount),
            "from_location": self.from_location or "",
            "to_location": self.to_location or "",
            "notes": self.notes or "",
        }


@dataclass
class ExtractionResult:
    items: list[ExpenseLineItem] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)


def normalize_category(raw: str) -> str:
    key = (raw or "").strip().lower()
    key = re.sub(r"\s+", " ", key)
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    titled = key.title()
    if titled in EXPENSE_CATEGORIES:
        return titled
    return "Other"


def _parse_amount_match(m: re.Match[str]) -> float | None:
    try:
        whole = m.group(1)
        frac = m.group(2) or ""
        return float(f"{whole}.{frac}" if frac else whole)
    except (TypeError, ValueError, IndexError):
        return None


def _split_clauses(message: str) -> list[str]:
    text = (message or "").strip()
    if not text:
        return []
    parts = re.split(r"[,;।\n]+|\s+এবং\s+|\s+and\s+|\s*\+\s*", text, flags=re.I)
    return [p.strip() for p in parts if p.strip()]


def _extract_from_clause(clause: str) -> list[ExpenseLineItem]:
    found: list[ExpenseLineItem] = []
    route_m = _ROUTE_RE.search(clause)
    if route_m:
        amt_m = _AMOUNT_RE.search(clause[route_m.end() :])
        if amt_m:
            val = _parse_amount_match(amt_m)
            if val and val > 0:
                found.append(
                    ExpenseLineItem(
                        category=normalize_category(route_m.group("cat")),
                        amount=val,
                        from_location=route_m.group(1).strip(),
                        to_location=route_m.group(2).strip(),
                    )
                )
                return found

    for m in _ITEM_PAIR_RE.finditer(clause):
        amt_m = _AMOUNT_RE.search(m.group(0))
        if not amt_m:
            continue
        val = _parse_amount_match(amt_m)
        if val is None or val <= 0:
            continue
        found.append(
            ExpenseLineItem(
                category=normalize_category(m.group("cat")),
                amount=val,
            )
        )

    for m in _ITEM_PAIR_REV_RE.finditer(clause):
        amt_m = _AMOUNT_RE.search(m.group("amt") or "")
        if not amt_m:
            continue
        val = _parse_amount_match(amt_m)
        if val is None or val <= 0:
            continue
        found.append(
            ExpenseLineItem(
                category=normalize_category(m.group("cat")),
                amount=val,
            )
        )

    if found:
        return found

    cat_m = re.search(rf"\b({_CATEGORY_TOKEN})\b", clause, re.I)
    amt_m = _AMOUNT_RE.search(clause)
    if cat_m and amt_m:
        val = _parse_amount_match(amt_m)
        if val and val > 0:
            found.append(
                ExpenseLineItem(
                    category=normalize_category(cat_m.group(1)),
                    amount=val,
                )
            )
            return found

    # Amount-first fallback: "100 taka", "amar expense lagbe 100 taka", "50 taka tea"
    if amt_m:
        val = _parse_amount_match(amt_m)
        if val and val > 0:
            notes = clause[:200]
            cat = "Other"
            if re.search(r"\b(tea|coffee|snack|snacks|চা)\b", clause, re.I):
                cat = "Snack"
            elif re.search(r"\b(cost|kharcha|khoroch|খরচ|expense)\b", clause, re.I):
                cat = "Other"
            found.append(
                ExpenseLineItem(category=cat, amount=val, notes=notes),
            )
    return found


def extract_expense_items(message: str) -> ExtractionResult:
    """
    Extract zero or more expense line items from one user message.
    """
    clauses = _split_clauses(message)
    if not clauses:
        clauses = [message or ""]

    items: list[ExpenseLineItem] = []
    malformed: list[str] = []
    for clause in clauses:
        chunk_items = _extract_from_clause(clause)
        if chunk_items:
            items.extend(chunk_items)
        elif _AMOUNT_RE.search(clause) or re.search(_CATEGORY_TOKEN, clause, re.I):
            malformed.append(clause[:120])

    # Deduplicate identical category+amount in same message (accidental double-parse)
    seen: set[tuple[str, float]] = set()
    unique: list[ExpenseLineItem] = []
    for it in items:
        key = (it.category, round(it.amount, 2))
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    return ExtractionResult(items=unique, malformed=malformed)


def merge_items(
    existing: list[dict[str, Any]],
    new_items: list[ExpenseLineItem],
    *,
    replace_same_category: bool = False,
) -> list[dict[str, Any]]:
    """
    Merge extracted items into draft list.
    replace_same_category: update amount if category already exists (fresh extraction turn).
    """
    out = [dict(x) for x in existing]
    for ni in new_items:
        d = ni.to_dict()
        replaced = False
        if replace_same_category:
            for i, row in enumerate(out):
                if str(row.get("category") or "").lower() == d["category"].lower():
                    out[i] = {**row, **d}
                    replaced = True
                    break
        if not replaced:
            out.append(d)
    return out
