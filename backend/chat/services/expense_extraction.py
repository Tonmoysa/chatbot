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

# CRM "Add Daily Expense" — travel rows need From / To (screenshot form).
TRAVEL_CATEGORIES: frozenset[str] = frozenset(
    {"Bus", "Rickshaw", "Train", "Bike", "CNG", "Metro Rail"}
)
NON_TRAVEL_CATEGORIES: frozenset[str] = frozenset({"Lunch", "Snack"})

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

# Words between category and amount in natural BN/EN (e.g. "bus vara 30 taka").
_CAT_TO_AMT_GAP = (
    r"(?:vara|vhara|bhara|fare|ভাড়া|ভাড়ায়|খরচ|করেছি|করেছ|হয়েছে|"
    r"e|te|এ|তে|a|er|for|on|এর|এ)"
)

# category ... amount OR amount ... category
_ITEM_PAIR_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})"
    rf"(?:\s*(?:{_CAT_TO_AMT_GAP}))*\s*"
    rf"(?P<amt>{_AMOUNT_RE.pattern})",
    re.I,
)
_ITEM_PAIR_REV_RE = re.compile(
    rf"(?P<amt>{_AMOUNT_RE.pattern})\s*"
    rf"(?:টাকা|taka|tk|bdt)?\s*"
    rf"(?P<cat>{_CATEGORY_TOKEN})",
    re.I,
)

# Banglish: "50 ta lunch", "50টা lunch"
_TA_AMOUNT_CAT_RE = re.compile(
    rf"(?P<amt>{_AMOUNT_RE.pattern})\s*ta\s+(?P<cat>{_CATEGORY_TOKEN})\b",
    re.I,
)

_DECLARED_TOTAL_RE = re.compile(
    r"(?:"
    rf"(?P<a>{_AMOUNT_RE.pattern})\s*(?:টাকা|taka|tk)?\s*(?:cost|খরচ|kharcha|khoroch)\s*hoy"
    r"|"
    rf"(?:mot|total|মোট)\s*(?:খরচ|cost|kharcha)?\s*(?P<a2>{_AMOUNT_RE.pattern})"
    r")",
    re.I,
)

# Banglish route: "uttora theke mirpur bus" (location before থেকে, not after).
_LOC_LABEL = r"[a-zA-Z0-9\u0980-\u09FF][\w\s\-]{1,60}"
_BANGLISH_ROUTE_CONNECTOR = r"(?:theke|thke|থেকে|from)"

_ROUTE_BANGLISH_RE = re.compile(
    rf"(?P<frm>{_LOC_LABEL})\s+{_BANGLISH_ROUTE_CONNECTOR}\s+"
    rf"(?P<to>{_LOC_LABEL})\s+"
    rf"(?P<cat>{_CATEGORY_TOKEN})",
    re.I,
)

# English-first route: "from office to mirpur bus"
_ROUTE_RE = re.compile(
    r"(?:from|theke|থেকে)\s+([a-zA-Z0-9\u0980-\u09FF\s]{2,40}?)\s+"
    r"(?:to|theke|যাওয়া|e|এ)\s+([a-zA-Z0-9\u0980-\u09FF\s]{2,40}?)\s+"
    rf"(?P<cat>{_CATEGORY_TOKEN})",
    re.I,
)

_BANGLISH_FROM_TO_RE = re.compile(
    rf"(?P<frm>{_LOC_LABEL})\s+{_BANGLISH_ROUTE_CONNECTOR}\s+"
    rf"(?P<to>{_LOC_LABEL})",
    re.I,
)

# Route + amount without category: "uttora theke mirpur 60 taka"
_ROUTE_AMOUNT_ONLY_RE = re.compile(
    rf"(?P<frm>{_LOC_LABEL})\s+{_BANGLISH_ROUTE_CONNECTOR}\s+"
    rf"(?P<to>{_LOC_LABEL})\s+"
    rf"(?P<amt>{_AMOUNT_RE.pattern})",
    re.I,
)

_FROM_TO_SIMPLE_RE = re.compile(
    r"(?:"
    r"(?:from|theke|থেকে)\s*(?P<frm>[a-zA-Z0-9\u0980-\u09FF][\w\s\-]{0,60})\s+"
    r"(?:to|theke|যাওয়া|e|এ|পর্যন্ত|porjonto)\s*(?P<to>[a-zA-Z0-9\u0980-\u09FF][\w\s\-]{0,60})"
    r"|"
    r"(?P<frm2>[a-zA-Z][\w\s\-]{1,60})\s+to\s+(?P<to2>[a-zA-Z][\w\s\-]{1,60})"
    r")",
    re.I,
)

_HYPHEN_ROUTE_RE = re.compile(
    rf"(?P<frm>{_LOC_LABEL})\s*-\s*(?P<to>{_LOC_LABEL})",
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


def is_travel_category(category: str) -> bool:
    return normalize_category(category) in TRAVEL_CATEGORIES


def parse_category_token(message: str) -> str | None:
    text = message or ""
    if re.search(r"\bother\b", text, re.I):
        return "Other"
    # Banglish: "bus e", "lunch e"
    m = re.search(rf"\b({_CATEGORY_TOKEN})\s*e\b", text, re.I)
    if m:
        return normalize_category(m.group(1))
    m = re.search(rf"\b({_CATEGORY_TOKEN})\b", text, re.I)
    if not m:
        return None
    return normalize_category(m.group(1))


def parse_declared_day_total(message: str) -> float | None:
    """User-stated day total, e.g. '100 taka cost hoyeche' (not per-line amounts)."""
    text = (message or "").strip()
    if not text:
        return None
    m = _DECLARED_TOTAL_RE.search(text)
    if not m:
        return None
    raw = m.group("a") or m.group("a2")
    if not raw:
        return None
    try:
        val = float(raw.replace(",", "."))
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def parse_amount_only(message: str) -> float | None:
    """Single amount, no category — e.g. 'ajke 40 taka cost hoyeche'."""
    text = (message or "").strip()
    if not text or parse_category_token(text):
        return None
    if len(_split_clauses(text)) > 1:
        return None
    hits = list(_AMOUNT_RE.finditer(text))
    if len(hits) != 1:
        return None
    val = _parse_amount_match(hits[0])
    return val if val and val > 0 else None


# Only strip a trailing number from a location when it is clearly an expense amount
# (e.g. "mirpur 60 taka"), not part of the place name (e.g. "road 7", "mirpur 10").
_AMOUNT_WITH_CURRENCY_TAIL_RE = re.compile(
    r"\s+(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?\s+"
    r"(?:টাকা|taka|tk|tks|bdt|৳|cost|খরচ|hoyeche)\b.*$",
    re.I,
)


def _trim_route_location_tail(loc: str) -> str:
    """Drop trailing category/fare/amount tokens accidentally captured in a location label."""
    s = (loc or "").strip()
    if not s:
        return s
    s = _AMOUNT_WITH_CURRENCY_TAIL_RE.sub("", s)
    s = re.sub(
        rf"\s+(?:{_CATEGORY_TOKEN}|vara|vhara|bhara|fare|ভাড়া)\b.*$",
        "",
        s,
        flags=re.I,
    )
    return s.strip()


def _valid_location_pair(frm: str, to: str) -> tuple[str, str] | None:
    frm = _clean_location_label(_trim_route_location_tail(frm))
    to = _clean_location_label(_trim_route_location_tail(to))
    if frm and to and len(frm) >= 2 and len(to) >= 2:
        return frm, to
    return None


def _looks_like_route_answer(message: str) -> bool:
    """True when the user is likely answering a From/To prompt (not adding new lines)."""
    text = (message or "").strip()
    if not text:
        return False
    if re.search(rf"\b{_CATEGORY_TOKEN}\b", text, re.I):
        return False
    if _HYPHEN_ROUTE_RE.search(text):
        return True
    if _BANGLISH_FROM_TO_RE.search(text):
        return True
    if _FROM_TO_SIMPLE_RE.search(text):
        return True
    return bool(re.search(r"\b(to|theke|থেকে)\b", text, re.I))


def parse_from_to_locations(message: str) -> tuple[str, str] | None:
    text = (message or "").strip()
    if not text:
        return None
    m = _HYPHEN_ROUTE_RE.search(text)
    if m:
        pair = _valid_location_pair(m.group("frm") or "", m.group("to") or "")
        if pair:
            return pair
    m = _BANGLISH_FROM_TO_RE.search(text)
    if m:
        pair = _valid_location_pair(m.group("frm") or "", m.group("to") or "")
        if pair:
            return pair
    m = _FROM_TO_SIMPLE_RE.search(text)
    if m:
        pair = _valid_location_pair(
            (m.group("frm") or m.group("frm2") or ""),
            (m.group("to") or m.group("to2") or ""),
        )
        if pair:
            return pair
    route_m = _ROUTE_BANGLISH_RE.search(text)
    if route_m:
        pair = _valid_location_pair(
            route_m.group("frm") or "", route_m.group("to") or ""
        )
        if pair:
            return pair
    route_m = _ROUTE_RE.search(text)
    if route_m:
        pair = _valid_location_pair(route_m.group(1) or "", route_m.group(2) or "")
        if pair:
            return pair
    return None


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
    parts = re.split(
        r"[,;।\n]+|\s+এবং\s+|\s+and\s+|\s*\+\s*"
        r"|\s+then\s+|\s+tarpor\s+|\s+abar\s+|\s+again\s+|\s+pore\s+",
        text,
        flags=re.I,
    )
    return [p.strip() for p in parts if p.strip()]


def _span_overlaps(covered: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(not (end <= a or start >= b) for a, b in covered)


def _clean_location_label(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(
        r"^(?:first\s+cost|cost|খরচ|amar|ami|my)\s+",
        "",
        s,
        flags=re.I,
    ).strip()
    return s


def _route_from_clause_suffix(
    clause: str, category: str, amount: float
) -> tuple[str, str] | None:
    """
    Parse From/To after category+amount, e.g. "bus 50 office to badda".
    """
    target_cat = normalize_category(category)
    target_amt = round(float(amount), 2)
    for m in _ITEM_PAIR_RE.finditer(clause):
        if normalize_category(m.group("cat")) != target_cat:
            continue
        amt_m = _AMOUNT_RE.search(m.group(0))
        if not amt_m:
            continue
        val = _parse_amount_match(amt_m)
        if val is None or round(val, 2) != target_amt:
            continue
        suffix = clause[m.end() :].strip()
        if not suffix:
            continue
        pair = parse_from_to_locations(suffix)
        if not pair:
            continue
        frm, to = _clean_location_label(pair[0]), _clean_location_label(pair[1])
        if frm and to and len(frm) >= 2 and len(to) >= 2:
            return frm, to
    return None


def _route_from_clause_prefix(clause: str, category: str) -> tuple[str, str] | None:
    """
    Parse From/To only from text *before* the category token (not the full clause).
    e.g. "office to mirpur bus e 40" → office / mirpur
    """
    cat_key = (category or "").split()[0].lower()
    if not cat_key:
        return None
    m = re.search(
        rf"\b({cat_key}|bus|lunch|rickshaw|train|bike|cng|metro)\s*e?\b",
        clause,
        re.I,
    )
    if not m:
        return None
    prefix = clause[: m.start()].strip()
    if not prefix:
        return None
    pair = parse_from_to_locations(prefix)
    if not pair:
        return None
    frm, to = _clean_location_label(pair[0]), _clean_location_label(pair[1])
    if frm and to and len(frm) >= 2 and len(to) >= 2:
        return frm, to
    return None


def _attach_trailing_route(clause: str, items: list[ExpenseLineItem]) -> None:
    """Apply route hints to travel lines in this clause."""
    for item in items:
        if not is_travel_category(item.category):
            continue
        if item.from_location and item.to_location:
            continue
        pair = _route_from_clause_prefix(clause, item.category)
        if not pair:
            pair = _route_from_clause_suffix(clause, item.category, item.amount)
        if pair:
            item.from_location, item.to_location = pair


def _amount_nearest_category(clause: str, cat_pos: int) -> float | None:
    best: float | None = None
    best_dist: int | None = None
    for m in _AMOUNT_RE.finditer(clause):
        val = _parse_amount_match(m)
        if val is None or val <= 0:
            continue
        dist = abs(((m.start() + m.end()) // 2) - cat_pos)
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = val
    return best


def _extract_from_clause(clause: str) -> list[ExpenseLineItem]:
    found: list[ExpenseLineItem] = []
    covered: list[tuple[int, int]] = []

    for m in _TA_AMOUNT_CAT_RE.finditer(clause):
        amt_m = _AMOUNT_RE.search(m.group("amt") or "")
        if not amt_m:
            continue
        val = _parse_amount_match(amt_m)
        if val is None or val <= 0:
            continue
        covered.append((m.start(), m.end()))
        found.append(
            ExpenseLineItem(
                category=normalize_category(m.group("cat")),
                amount=val,
            )
        )

    route_amt = _ROUTE_AMOUNT_ONLY_RE.search(clause)
    if route_amt and not re.search(rf"\b{_CATEGORY_TOKEN}\b", clause, re.I):
        amt_match = _AMOUNT_RE.search(route_amt.group("amt") or "")
        val = _parse_amount_match(amt_match) if amt_match else None
        pair = _valid_location_pair(
            route_amt.group("frm") or "", route_amt.group("to") or ""
        )
        if val and val > 0 and pair:
            return [
                ExpenseLineItem(
                    category="",
                    amount=val,
                    from_location=pair[0],
                    to_location=pair[1],
                )
            ]

    route_m = _ROUTE_BANGLISH_RE.search(clause) or _ROUTE_RE.search(clause)
    if route_m:
        amt_m = _AMOUNT_RE.search(clause[route_m.end() :])
        if amt_m:
            val = _parse_amount_match(amt_m)
            if val and val > 0:
                abs_start = route_m.start()
                abs_end = route_m.end() + amt_m.end()
                covered.append((abs_start, abs_end))
                if route_m.groupdict().get("frm"):
                    frm_raw, to_raw = route_m.group("frm"), route_m.group("to")
                else:
                    frm_raw, to_raw = route_m.group(1), route_m.group(2)
                pair = _valid_location_pair(frm_raw or "", to_raw or "")
                found.append(
                    ExpenseLineItem(
                        category=normalize_category(route_m.group("cat")),
                        amount=val,
                        from_location=pair[0] if pair else (frm_raw or "").strip(),
                        to_location=pair[1] if pair else (to_raw or "").strip(),
                    )
                )
                return found

    # Forward: "bus vara 30", "lunch 100" — highest priority.
    for m in _ITEM_PAIR_RE.finditer(clause):
        amt_m = _AMOUNT_RE.search(m.group(0))
        if not amt_m:
            continue
        val = _parse_amount_match(amt_m)
        if val is None or val <= 0:
            continue
        covered.append((m.start(), m.end()))
        found.append(
            ExpenseLineItem(
                category=normalize_category(m.group("cat")),
                amount=val,
            )
        )

    # Reverse: "100 taka lunch" — only if this span was not already paired forward.
    for m in _ITEM_PAIR_REV_RE.finditer(clause):
        if _span_overlaps(covered, m.start(), m.end()):
            continue
        amt_m = _AMOUNT_RE.search(m.group("amt") or "")
        if not amt_m:
            continue
        abs_amt = (m.start("amt"), m.end("amt")) if m.group("amt") else amt_m.span()
        if _span_overlaps(covered, abs_amt[0], abs_amt[1]):
            continue
        val = _parse_amount_match(amt_m)
        if val is None or val <= 0:
            continue
        covered.append((m.start(), m.end()))
        found.append(
            ExpenseLineItem(
                category=normalize_category(m.group("cat")),
                amount=val,
            )
        )

    if found:
        _attach_trailing_route(clause, found)
        return found

    cat_m = re.search(rf"\b({_CATEGORY_TOKEN})\b", clause, re.I)
    if cat_m and not _span_overlaps(covered, cat_m.start(), cat_m.end()):
        val = _amount_nearest_category(clause, cat_m.start())
        if val and val > 0:
            covered.append((cat_m.start(), cat_m.end()))
            found.append(
                ExpenseLineItem(
                    category=normalize_category(cat_m.group(1)),
                    amount=val,
                )
            )
            return found

    # Amount without category — do not invent "Other"; workflow will ask category.
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
        elif _AMOUNT_RE.search(clause):
            malformed.append(clause[:120])
        elif re.search(_CATEGORY_TOKEN, clause, re.I):
            malformed.append(clause[:120])

    # Route-only sibling clause: "rickshaw 10 taka, office to road 7"
    for it in items:
        if not is_travel_category(it.category):
            continue
        if it.from_location and it.to_location:
            continue
        for clause in list(malformed):
            if re.search(rf"\b{_CATEGORY_TOKEN}\b", clause, re.I):
                continue
            pair = parse_from_to_locations(clause)
            if pair:
                it.from_location, it.to_location = pair
                malformed.remove(clause)
                break

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
