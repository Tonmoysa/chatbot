"""
Structured multi-item expense extraction (BN / EN / Banglish).

Deterministic parsing — no LLM approval or outcome decisions here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from chat.services.expense_locations import strip_location_punctuation

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
    "luch": "Lunch",
    "lunc": "Lunch",
    "খাওয়া": "Lunch",
    "খাবার": "Lunch",
    "snack": "Snack",
    "snacks": "Snack",
    "স্ন্যাক": "Snack",
    "bus": "Bus",
    "bos": "Bus",
    "bas": "Bus",
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
    "metroral": "Metro Rail",
    "metorail": "Metro Rail",
    "rail": "Metro Rail",
    "মেট্রো": "Metro Rail",
    "uber": "Other",
    "cab": "Other",
    "taxi": "Other",
    "food": "Lunch",
    "meal": "Lunch",
    "transport": "Bus",
    "travel": "Bus",
    "other": "Other",
    "misc": "Other",
}

_AMOUNT_RE = re.compile(
    r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?\s*(?:টাকা|taka|tk|tks|bdt|৳)?(?!\d)",
    re.I,
)

_CATEGORY_TOKEN = (
    r"(?:lunch|lanch|luch|lunc|snacks?|bus|rickshaw|riksha|train|bike|bicycle|"
    r"cng|auto|metro(?:\s*rail)?|rail|uber|cab|taxi|food|meal|transport|travel|"
    r"other|misc|"
    r"খাওয়া|খাবার|বাস|রিকশা|ট্রেন|সাইকেল|সিএনজি|মেট্রো)"
)

# Words between category and amount in natural BN/EN (e.g. "bus vara 30 taka").
_CAT_TO_AMT_GAP = (
    r"(?:vara|vhara|bhara|fare|ভাড়া|ভাড়ায়|খরচ|করেছি|করেছ|হয়েছে|"
    r"expense|expenses|exp|"
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

# Tight "place to place" scan — enumerate around each ``to`` token (see _iter_simple_to_candidates).
_LOC_WORD = r"[a-zA-Z0-9\u0980-\u09FF][\w\-]*"
_SIMPLE_TO_SEP_RE = re.compile(r"\s+to\s+", re.I)

# Preamble / expense words that should not appear inside a location label.
_ROUTE_JUNK_WORDS_RE = re.compile(
    r"\b(?:ajke|aajke|amar|ami|my|expense|expenses|hoyeche|hocche|cost|kharch|"
    r"খরচ|taka|টাকা|tk|then|abar|first|note|kor|korechi|kore|lagche|laglo)\b",
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
    # Banglish: "bus e", "lunch e", "metroral e"
    m = re.search(rf"\b({_CATEGORY_TOKEN})\s*e\b", text, re.I)
    if m:
        return normalize_category(m.group(1))
    m = re.search(rf"\b({_CATEGORY_TOKEN})\b", text, re.I)
    if not m:
        for word in re.findall(r"\b[\w\u0980-\u09FF]+\b", text):
            key = word.lower()
            if key in _CATEGORY_ALIASES:
                return _CATEGORY_ALIASES[key]
        return None
    return normalize_category(m.group(1))


# Near-miss tokens that look like a category but fail strict regex (typos / Banglish).
_CATEGORY_TYPO_HINTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bmetroral\b", re.I), "Metro Rail"),
    (re.compile(r"\brail\b", re.I), "Metro Rail"),
    (re.compile(r"\bmetorail\b", re.I), "Metro Rail"),
    (re.compile(r"\bmetrorail\b", re.I), "Metro Rail"),
    (re.compile(r"\bmetrorel\b", re.I), "Metro Rail"),
    (re.compile(r"\bluch\b", re.I), "Lunch"),
    (re.compile(r"\blanch\b", re.I), "Lunch"),
    (re.compile(r"\briksha\b", re.I), "Rickshaw"),
    (re.compile(r"\brikshaw\b", re.I), "Rickshaw"),
    (re.compile(r"\bsnaks\b", re.I), "Snack"),
)


def detect_likely_category_typo(text: str) -> tuple[str, str] | None:
    """
    Return (original_token, suggested_category) when text contains a likely
    misspelled category (e.g. metroral → Metro Rail).
    """
    raw = (text or "").strip()
    if not raw:
        return None
    for pattern, category in _CATEGORY_TYPO_HINTS:
        m = pattern.search(raw)
        if m:
            return m.group(0), category
    for word in re.findall(r"\b[\w\u0980-\u09FF]+\b", raw):
        low = word.lower()
        if low in _CATEGORY_ALIASES:
            continue
        if parse_category_token(word):
            continue
        if low.startswith("metro") and len(low) >= 5:
            return word, "Metro Rail"
    return None


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


def _looks_like_location_label(label: str) -> bool:
    """True when text looks like a place name, not expense preamble or category."""
    s = (label or "").strip()
    if len(s) < 2 or len(s) > 45:
        return False
    if _ROUTE_JUNK_WORDS_RE.search(s):
        return False
    if re.search(rf"\b({_CATEGORY_TOKEN})\b", s, re.I):
        return False
    if _AMOUNT_RE.search(s):
        return False
    if len(s.split()) > 4:
        return False
    return True


def _nearest_category_distance(text: str, pos: int) -> int:
    best = 10_000
    for m in re.finditer(rf"\b({_CATEGORY_TOKEN})\b", text, re.I):
        mid = (m.start() + m.end()) // 2
        best = min(best, abs(pos - mid))
    return best


def _score_route_pair(
    frm: str, to: str, start: int, end: int, text: str
) -> int:
    """
    Rank route candidates in a long clause: prefer real place names near a category token.
    """
    score = 0
    if _looks_like_location_label(frm):
        score += 50
    else:
        score -= 60
    if _looks_like_location_label(to):
        score += 50
    else:
        score -= 60
    score -= len(frm.split()) + len(to.split())
    score -= _nearest_category_distance(text, (start + end) // 2) // 3
    score += start // 20
    return score


def _pick_best_route_pair(
    text: str,
    pattern: re.Pattern[str],
    *,
    frm_group: str = "frm",
    to_group: str = "to",
    frm_alt: str | None = None,
    to_alt: str | None = None,
) -> tuple[str, str] | None:
    """Among all regex matches, return the highest-scoring valid From/To pair."""
    best: tuple[str, str] | None = None
    best_score: int | None = None
    for m in pattern.finditer(text):
        frm_raw = m.group(frm_group) or (m.group(frm_alt) if frm_alt else "") or ""
        to_raw = m.group(to_group) or (m.group(to_alt) if to_alt else "") or ""
        pair = _valid_location_pair(frm_raw, to_raw)
        if not pair:
            continue
        score = _score_route_pair(pair[0], pair[1], m.start(), m.end(), text)
        if best_score is None or score > best_score:
            best_score = score
            best = pair
    return best


def _extend_trailing_sector(text: str, end: int, label: str) -> tuple[str, int]:
    """Attach a trailing sector number: ``road`` + ``7`` → ``road 7``."""
    m = re.match(r"^\s+(\d{1,2})\b", text[end:])
    if m:
        return f"{label} {m.group(1)}", end + m.end()
    return label, end


def _iter_simple_to_candidates(
    text: str,
) -> list[tuple[str, str, int, int]]:
    """
    Enumerate 'X to Y' spans around each ``to`` token (overlapping allowed).
    Picks real routes like ``mirpur to badda`` inside long preambles.
    """
    out: list[tuple[str, str, int, int]] = []
    for sep in _SIMPLE_TO_SEP_RE.finditer(text):
        before = text[: sep.start()]
        after = text[sep.end() :]
        for n_frm in range(1, 5):
            m_frm = re.search(
                rf"(\S+(?:\s+\S+){{{n_frm - 1}}})\s*$",
                before,
            )
            if not m_frm:
                continue
            for n_to in range(1, 5):
                m_to = re.match(
                    rf"^(\S+(?:\s+\S+){{{n_to - 1}}})(?:\s|$|[,.])",
                    after,
                )
                if not m_to:
                    continue
                frm_raw = m_frm.group(1)
                to_raw = m_to.group(1)
                start = m_frm.start(1)
                end = sep.end() + m_to.end(1)
                to_raw, end = _extend_trailing_sector(text, end, to_raw)
                out.append((frm_raw, to_raw, start, end))
    return out


def _pick_best_simple_to_pair(text: str) -> tuple[str, str] | None:
    """Best 'X to Y' pair in text — ignores preamble before the real route."""
    candidates: list[tuple[tuple[str, str], int]] = []

    for frm_raw, to_raw, start, end in _iter_simple_to_candidates(text):
        pair = _valid_location_pair(frm_raw, to_raw)
        if not pair:
            continue
        score = _score_route_pair(pair[0], pair[1], start, end, text)
        candidates.append((pair, score))

    for m in _FROM_TO_SIMPLE_RE.finditer(text):
        if m.group("frm") or m.group("to"):
            frm_raw = (m.group("frm") or "").strip()
            to_raw = (m.group("to") or "").strip()
        else:
            frm_raw = (m.group("frm2") or "").strip()
            to_raw = (m.group("to2") or "").strip()
        pair = _valid_location_pair(frm_raw, to_raw)
        if not pair:
            continue
        score = _score_route_pair(pair[0], pair[1], m.start(), m.end(), text)
        candidates.append((pair, score))

    if not candidates:
        return None
    return max(candidates, key=lambda x: x[1])[0]


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
    pair = _pick_best_route_pair(text, _BANGLISH_FROM_TO_RE)
    if pair:
        return pair
    pair = _pick_best_simple_to_pair(text)
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
        r"|\s+then\s+|\s+tarpor\s+|\s+abar\s+|\s+again\s+|\s+pore\s+"
        # Banglish: "...and lunch", "..and bus", or "cost hoyeche...50 ta lunch"
        r"|\s*\.{2,}\s*and\s+|\s*\.{2,}\s+(?=\d)"
        # "cost hoyeche.luch 20" — period before word, no space (not decimals like 3.50)
        r"|(?<!\d)\.(?=[A-Za-z\u0980-\u09FF])",
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
    return strip_location_punctuation(s)


def _route_from_clause_suffix(
    clause: str, category: str, amount: float
) -> tuple[str, str] | None:
    """
    Parse From/To after category+amount, e.g. "bus 50 office to badda"
    or reverse order "100 taka bus mirpur to badda".
    """
    target_cat = normalize_category(category)
    target_amt = round(float(amount), 2)

    def _pair_after_match(m: re.Match[str], val: float | None) -> tuple[str, str] | None:
        if val is None or round(val, 2) != target_amt:
            return None
        suffix = clause[m.end() :].strip()
        if not suffix:
            return None
        pair = parse_from_to_locations(suffix)
        if not pair:
            return None
        frm, to = _clean_location_label(pair[0]), _clean_location_label(pair[1])
        if frm and to and len(frm) >= 2 and len(to) >= 2:
            return frm, to
        return None

    for m in _ITEM_PAIR_RE.finditer(clause):
        if normalize_category(m.group("cat")) != target_cat:
            continue
        amt_m = _AMOUNT_RE.search(m.group(0))
        if not amt_m:
            continue
        pair = _pair_after_match(m, _parse_amount_match(amt_m))
        if pair:
            return pair

    for m in _ITEM_PAIR_REV_RE.finditer(clause):
        if normalize_category(m.group("cat")) != target_cat:
            continue
        amt_m = _AMOUNT_RE.search(m.group("amt") or "")
        if not amt_m:
            continue
        pair = _pair_after_match(m, _parse_amount_match(amt_m))
        if pair:
            return pair

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


def _uncategorized_amounts_in_clause(
    clause: str, covered: list[tuple[int, int]]
) -> list[ExpenseLineItem]:
    """Amounts in the clause not already paired with a category (e.g. '100 cost hoyeche, lunch 20')."""
    extra: list[ExpenseLineItem] = []
    for m in _AMOUNT_RE.finditer(clause):
        if _span_overlaps(covered, m.start(), m.end()):
            continue
        val = _parse_amount_match(m)
        if val is None or val <= 0:
            continue
        extra.append(ExpenseLineItem(category="", amount=val))
    return extra


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


def _extract_alias_category_line(
    clause: str, covered: list[tuple[int, int]]
) -> ExpenseLineItem | None:
    """Match alias typos (metroral, luch) that fail strict _CATEGORY_TOKEN regex."""
    for m in re.finditer(r"\b[\w\u0980-\u09FF]+\b", clause):
        key = m.group(0).lower()
        if key not in _CATEGORY_ALIASES:
            continue
        if _span_overlaps(covered, m.start(), m.end()):
            continue
        cat = _CATEGORY_ALIASES[key]
        val = _amount_nearest_category(clause, m.start())
        if val is None or val <= 0:
            continue
        item = ExpenseLineItem(category=cat, amount=val)
        if is_travel_category(cat):
            pair = _route_from_clause_prefix(clause, cat) or _route_from_clause_suffix(
                clause, cat, val
            )
            if pair:
                item.from_location, item.to_location = pair
        return item
    return None


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
        alias_item = _extract_alias_category_line(clause, covered)
        if alias_item:
            return [alias_item]
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
        found.extend(_uncategorized_amounts_in_clause(clause, covered))
        return found

    alias_item = _extract_alias_category_line(clause, covered)
    if alias_item:
        covered.append((0, len(clause)))
        found.append(alias_item)
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
            for it in chunk_items:
                if not it.category and not (it.notes or "").strip():
                    it.notes = clause
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

    items = _collapse_metro_train_duplicates(items, message)

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


def _collapse_metro_train_duplicates(
    items: list[ExpenseLineItem], message: str
) -> list[ExpenseLineItem]:
    """
    Drop spurious Train lines when Metro Rail was parsed from the same message.
    Common when users say metroral/metro rail (STT) without mentioning train.
    """
    if not items:
        return items
    raw = message or ""
    low = raw.lower()
    has_metro_item = any(it.category == "Metro Rail" for it in items)
    if not has_metro_item:
        return items
    has_metro_mention = bool(
        re.search(
            r"metro(?:\s*rail|rail|ral|rel)?|metroral|metrorail|\brail\b|মেট্রো",
            low,
            re.I,
        )
    )
    if not has_metro_mention:
        return items
    explicit_train = bool(
        re.search(r"(?<![a-z])train(?![a-z])|ট্রেন", low, re.I)
        and not re.search(r"metrorail|metroral|metro\s*rail", low, re.I)
    )
    if explicit_train:
        return items
    return [it for it in items if it.category != "Train"]


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
