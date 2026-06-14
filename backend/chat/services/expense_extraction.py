"""
Structured multi-item expense extraction (BN / EN / Banglish).

Deterministic parsing — no LLM approval or outcome decisions here.
"""

from __future__ import annotations

import re
import unicodedata
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
    "বাসে": "Bus",
    "বাস": "Bus",
    "বাইকে": "Bike",
    "বাইক": "Bike",
    "লাঞ্ছ": "Lunch",
    "লাঞ্চ": "Lunch",
    "রিকশায়": "Rickshaw",
}

_BN_DIGIT_MAP = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")

_BN_NUMBER_WORDS: dict[str, int] = {
    "একশো": 100,
    "একশ": 100,
    "দুইশো": 200,
    "দুইশ": 200,
    "তিনশো": 300,
    "তিনশ": 300,
    "চারশো": 400,
    "চারশ": 400,
    "পাঁচশো": 500,
    "পাঁচশ": 500,
    "পঞ্চাশ": 50,
    "ষাট": 60,
    "নব্বই": 90,
    "আশি": 80,
    "চল্লিশ": 40,
    "ত্রিশ": 30,
    "বিশ": 20,
    "দশ": 10,
    "এক": 1,
    "দুই": 2,
    "তিন": 3,
    "চার": 4,
    "পাঁচ": 5,
}

# Compound Bengali amounts before single-word replacement (একশ বিশ → 120).
_BN_COMPOUND_NUMBER_RE = re.compile(
    r"(?P<hundreds>একশো|একশ|দুইশো|দুইশ|তিনশো|তিনশ|চারশো|চারশ|পাঁচশো|পাঁচশ)"
    r"\s*(?P<tens>বিশ|ত্রিশ|চল্লিশ|পঞ্চাশ|ষাট|সত্তর|আশি|নব্বই)",
    re.I,
)
_BN_HUNDREDS_MAP = {
    "একশো": 100,
    "একশ": 100,
    "দুইশো": 200,
    "দুইশ": 200,
    "তিনশো": 300,
    "তিনশ": 300,
    "চারশো": 400,
    "চারশ": 400,
    "পাঁচশো": 500,
    "পাঁচশ": 500,
}
_BN_TENS_MAP = {
    "বিশ": 20,
    "ত্রিশ": 30,
    "চল্লিশ": 40,
    "পঞ্চাশ": 50,
    "ষাট": 60,
    "সত্তর": 70,
    "আশি": 80,
    "নব্বই": 90,
}

_AMOUNT_RE = re.compile(
    r"(?<!\d)([\d০-৯]{1,6})(?:[.,](\d{1,2}))?\s*(?:টাকা|taka|tk|tks|bdt|৳)?(?!\d)",
    re.I,
)

_CATEGORY_TOKEN = (
    r"(?:lunch|lanch|luch|lunc|snacks?|bus|rickshaw|riksha|train|bike|bicycle|"
    r"cng|auto|metro(?:\s*rail)?|rail|uber|cab|taxi|food|meal|transport|travel|"
    r"other|misc|"
    r"খাওয়া|খাবার|বাস|বাসে|বাইক|বাইকে|লাঞ্ছ|লাঞ্চ|রিকশা|রিকশায়|ট্রেন|সাইকেল|সিএনজি|মেট্রো)"
)

_ROUTE_TO_CONNECTOR = r"(?:to|টু)"

# Common Dhaka place names — voice often keeps Bengali script; romanize for CRM/display.
_BN_PLACE_ROMAN: dict[str, str] = {
    "মিরপুর": "mirpur",
    "মতিঝিল": "motijheel",
    "মতিজিল": "motijheel",
    "কমলাপুর": "kamalapur",
    "বিমানবন্দর": "airport",
    "উত্তরা": "uttora",
    "আগারগাঁও": "agargaon",
    "আগারগাঁ": "agargaon",
    "বাড্ডা": "badda",
    "গুলশান": "gulshan",
    "বনানী": "banani",
    "ধানমন্ডি": "dhanmondi",
    "ফার্মগেট": "farmgate",
    "শাহবাগ": "shahbagh",
    "কারওয়ান বাজার": "karwan bazar",
    "কারওয়ানবাজার": "karwan bazar",
    "যাত্রাবাড়ী": "jatrabari",
    "যাত্রাবাড়ি": "jatrabari",
}

# Bengali voice fillers — use str.replace (\\b fails on Bengali script in Python regex).
_BN_VOICE_FILLER = (
    "গিয়েছি",
    "গিয়েছি",
    "giyechi",
    "পৌঁছে",
    "পৌছে",
    "pouche",
    "করেছি",
    "কিনেছি",
    "সকালে",
    "sokale",
    "আজকে",
    "ajke",
    "aajke",
    "হয়েছে",
    "হয়েছে",
    "hoyeche",
    "hocche",
)

_BIKE_HINT_RE = re.compile(
    r"(?:\b(bike|baik|baike|bicycle)\b|বাইক)",
    re.I,
)


def preprocess_expense_message(message: str) -> str:
    """
    Normalize Bengali voice/STT text before regex extraction.

    Converts Bengali digits and number words, route connector টু → to, and
    common locative category forms (বাসে → bus) so one-shot voice dumps parse.
    """
    text = unicodedata.normalize("NFKC", (message or "").strip())
    if not text:
        return text
    text = text.translate(_BN_DIGIT_MAP)
    text = re.sub(r"\s+টু\s+", " to ", text, flags=re.I)
    text = re.sub(
        r"^(?:আজকে\s+)?(?:খরচ\s+)?(?:হয়েছে|হয়েছে|hoyeche|hocche)\s+",
        "",
        text,
        flags=re.I,
    )
    for filler in _BN_VOICE_FILLER:
        text = text.replace(filler, " ")

    def _compound_bn_amount(m: re.Match[str]) -> str:
        h = _BN_HUNDREDS_MAP.get(m.group("hundreds"), 0)
        t = _BN_TENS_MAP.get(m.group("tens"), 0)
        return str(h + t) if h and t else m.group(0)

    text = _BN_COMPOUND_NUMBER_RE.sub(_compound_bn_amount, text)

    for word, val in sorted(_BN_NUMBER_WORDS.items(), key=lambda x: -len(x[0])):
        text = re.sub(
            rf"(?<![\w\u0980-\u09FF]){re.escape(word)}(?![\w\u0980-\u09FF])",
            str(val),
            text,
        )
    # Bike before bus — STT sometimes writes বাইকে; also catch Romanized voice tokens.
    text = text.replace("বাইকে", " bike ")
    text = text.replace("বাইক", " bike ")
    text = re.sub(r"\b(baik|baike|bike|bicycle)\b", " bike ", text, flags=re.I)
    text = text.replace("বাসে", " bus ")
    text = text.replace("লাঞ্ছ", " lunch ")
    text = text.replace("লাঞ্চ", " lunch ")
    text = text.replace("কফি", " snack ")
    text = text.replace("নাস্তা", " snack ")
    text = re.sub(r"\bnasta\b", " snack ", text, flags=re.I)
    text = re.sub(r"চা\s+snack", " snack ", text, flags=re.I)
    # \\b does not work on Bengali words — replace longest metro compounds first.
    text = text.replace("মেট্রোরেলে", " metro rail ")
    text = text.replace("মেট্রোরেল", " metro rail ")
    text = re.sub(r"মেট্রো(?!র)", " metro ", text)
    text = text.replace("ট্রেনে", " train ")
    text = re.sub(r"metro\s+রেলে", " metro rail ", text, flags=re.I)
    for bn, en in sorted(_BN_PLACE_ROMAN.items(), key=lambda x: -len(x[0])):
        text = text.replace(bn, f" {en} ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _romanize_known_place(label: str) -> str:
    s = strip_location_punctuation((label or "").strip())
    if not s:
        return s
    for bn, en in sorted(_BN_PLACE_ROMAN.items(), key=lambda x: -len(x[0])):
        if bn in s:
            s = s.replace(bn, en)
    return strip_location_punctuation(s)


def _location_key(label: str) -> str:
    return _romanize_known_place(label).lower()


def _routes_are_reverse(a: ExpenseLineItem, b: ExpenseLineItem) -> bool:
    if not (
        a.from_location
        and a.to_location
        and b.from_location
        and b.to_location
    ):
        return False
    af, at = _location_key(a.from_location), _location_key(a.to_location)
    bf, bt = _location_key(b.from_location), _location_key(b.to_location)
    return bool(af and at and bf and bt and af == bt and at == bf)


def _message_has_bike_hint(message: str) -> bool:
    return bool(_BIKE_HINT_RE.search(message or ""))


def _disambiguate_return_leg_categories(
    items: list[ExpenseLineItem],
    raw_message: str,
) -> list[ExpenseLineItem]:
    """
    Voice/STT often maps বাইকে → বাসে. When two Bus lines share a reverse commute
    (mirpur↔motijheel), relabel the return leg as Bike unless both are explicit bus.
    """
    if not items:
        return items
    bus_rows = [
        it
        for it in items
        if it.category == "Bus"
        and is_travel_category(it.category)
        and it.from_location
        and it.to_location
    ]
    if len(bus_rows) < 2:
        return items

    bike_hint = _message_has_bike_hint(raw_message)
    out = list(items)
    for i, first in enumerate(bus_rows):
        for second in bus_rows[i + 1 :]:
            if not _routes_are_reverse(first, second):
                continue
            if not bike_hint and first.amount == second.amount:
                continue
            # Prefer fixing the later leg (return trip) in original item order.
            try:
                idx = out.index(second)
            except ValueError:
                continue
            out[idx] = ExpenseLineItem(
                category="Bike",
                amount=second.amount,
                from_location=second.from_location,
                to_location=second.to_location,
                notes=second.notes,
            )
            return out
    return out


def _romanize_travel_locations(items: list[ExpenseLineItem]) -> list[ExpenseLineItem]:
    fixed: list[ExpenseLineItem] = []
    for it in items:
        if not is_travel_category(it.category):
            fixed.append(it)
            continue
        fixed.append(
            ExpenseLineItem(
                category=it.category,
                amount=it.amount,
                from_location=_romanize_known_place(it.from_location),
                to_location=_romanize_known_place(it.to_location),
                notes=it.notes,
            )
        )
    return fixed

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
# Tail must allow Bengali vowel signs (ি, ু, …) — \\w alone misses those codepoints.
_LOC_LABEL = r"[a-zA-Z0-9\u0980-\u09FF][\w\u0980-\u09FF\s\-]{0,59}"
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
    r"(?:from|theke|থেকে)\s*(?P<frm>[a-zA-Z0-9\u0980-\u09FF][\w\u0980-\u09FF\s\-]{0,59})\s+"
    rf"(?:{_ROUTE_TO_CONNECTOR}|theke|যাওয়া|e|এ|পর্যন্ত|porjonto)\s*"
    r"(?P<to>[a-zA-Z0-9\u0980-\u09FF][\w\u0980-\u09FF\s\-]{0,59})"
    r"|"
    rf"(?P<frm2>[a-zA-Z][\w\u0980-\u09FF\s\-]{{0,59}})\s+{_ROUTE_TO_CONNECTOR}\s+"
    r"(?P<to2>[a-zA-Z][\w\u0980-\u09FF\s\-]{0,59})"
    r")",
    re.I,
)

_HYPHEN_ROUTE_RE = re.compile(
    rf"(?P<frm>{_LOC_LABEL})\s*-\s*(?P<to>{_LOC_LABEL})",
    re.I,
)

# Tight "place to place" scan — enumerate around each ``to`` token (see _iter_simple_to_candidates).
_LOC_WORD = r"[a-zA-Z0-9\u0980-\u09FF][\w\-]*"
_SIMPLE_TO_SEP_RE = re.compile(rf"\s+{_ROUTE_TO_CONNECTOR}\s+", re.I)

# Voice/BN: "bus mirpur to motijheel 100" or "mirpur to motijheel bike 200"
_TRAVEL_CAT_ROUTE_AMT_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+"
    rf"(?P<frm>{_LOC_LABEL})\s+{_ROUTE_TO_CONNECTOR}\s+(?P<to>{_LOC_LABEL})\s+"
    rf"(?P<amt>{_AMOUNT_RE.pattern})",
    re.I,
)
_TRAVEL_ROUTE_CAT_AMT_RE = re.compile(
    rf"(?P<frm>{_LOC_LABEL})\s+{_ROUTE_TO_CONNECTOR}\s+(?P<to>{_LOC_LABEL})\s+"
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+"
    rf"(?P<amt>{_AMOUNT_RE.pattern})",
    re.I,
)

# Preamble / expense words that should not appear inside a location label.
_ROUTE_JUNK_WORDS_RE = re.compile(
    r"\b(?:ajke|aajke|amar|ami|my|expense|expenses|hoyeche|hocche|hoyese|cost|kharch|"
    r"খরচ|হয়েছে|হয়েছে|hoyeche|taka|টাকা|tk|then|abar|tarpor|তারপর|first|note|"
    r"kor|korechi|kore|lagche|laglo|khoroch|sokale|সকালে|giyechi|গিয়েছি|গিয়েছি|"
    r"pouche|পৌঁছে|পৌছে)\b",
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


def strip_ungrounded_travel_routes(
    items: list[ExpenseLineItem],
    message: str,
) -> list[ExpenseLineItem]:
    """Clear From/To on travel lines when the user did not state that route."""
    out: list[ExpenseLineItem] = []
    for item in items:
        cat = (item.category or "").strip()
        frm = (item.from_location or "").strip()
        to = (item.to_location or "").strip()
        if (
            cat
            and is_travel_category(cat)
            and frm
            and to
            and not route_explicit_in_user_message(message, frm, to)
        ):
            item = ExpenseLineItem(
                category=item.category,
                amount=item.amount,
                from_location="",
                to_location="",
                notes=item.notes,
            )
        out.append(item)
    return out


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
    r"\s+(?<![\d০-৯])([\d০-৯]{1,6})(?:[.,](\d{1,2}))?\s+"
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


def _strip_route_endpoint(raw: str) -> str:
    """Drop STT/preamble junk before the real place name (e.g. 'হয়েছে motijheel')."""
    s = _clean_location_label(_trim_route_location_tail(raw))
    for _ in range(5):
        if not s:
            break
        if _looks_like_location_label(s) or not _ROUTE_JUNK_WORDS_RE.search(s):
            return _romanize_known_place(s)
        parts = s.split(None, 1)
        if len(parts) < 2:
            break
        s = parts[1].strip()
    return _romanize_known_place(s)


def _valid_location_pair(frm: str, to: str) -> tuple[str, str] | None:
    frm = _strip_route_endpoint(frm)
    to = _strip_route_endpoint(to)
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


def _grounding_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def location_grounded_in_message(location: str, message: str) -> bool:
    """True when a location label is explicitly present in the user message."""
    loc = _grounding_text(_location_key(location))
    msg = _grounding_text(preprocess_expense_message(message))
    if not loc or not msg:
        return False
    if loc in msg:
        return True
    tokens = [
        t
        for t in re.findall(r"[a-zA-Z\u0980-\u09FF]+", loc)
        if len(t) >= 3
    ]
    if not tokens:
        return loc in msg
    return all(t in msg for t in tokens)


def route_explicit_in_user_message(
    message: str,
    from_loc: str,
    to_loc: str,
) -> bool:
    """Both endpoints must appear in the user message — never from prompt examples."""
    frm = (from_loc or "").strip()
    to = (to_loc or "").strip()
    if not frm or not to:
        return False
    if not location_grounded_in_message(frm, message):
        return False
    if not location_grounded_in_message(to, message):
        return False
    norm_msg = preprocess_expense_message(message)
    return _looks_like_route_answer(norm_msg) or bool(parse_from_to_locations(norm_msg))


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
        whole = str(m.group(1) or "").translate(_BN_DIGIT_MAP)
        frac = str(m.group(2) or "").translate(_BN_DIGIT_MAP)
        return float(f"{whole}.{frac}" if frac else whole)
    except (TypeError, ValueError, IndexError):
        return None


def _split_clauses(message: str) -> list[str]:
    text = preprocess_expense_message(message)
    if not text:
        return []
    parts = re.split(
        r"[,;।\n]+|\s+এবং\s+|\s+and\s+|\s*\+\s*"
        r"|\s+then\s+|\s+tarpor\s+|\s+তারপরে\s+|\s+তারপর\s+"
        r"|\s+abar\s+|\s+again\s+|\s+pore\s+"
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
        r"^(?:first\s+cost|cost|খরচ|khoroch|hoyeche|hocche|হয়েছে|হয়েছে|amar|ami|my|"
        r"ajke|aajke|আজকে|সকালে|sokale|giyechi|গিয়েছি|গিয়েছি)\s+",
        "",
        s,
        flags=re.I,
    ).strip()
    s = re.sub(
        r"^(?:খরচ|হয়েছে|হয়েছে)\s+",
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

    # Fallback: category + nearest amount (no item-pair regex), e.g.
    # "train e cost hoyeche 80 taka uttora to motejhil"
    for m in _AMOUNT_RE.finditer(clause):
        val = _parse_amount_match(m)
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


def _extract_travel_route_patterns(
    clause: str, covered: list[tuple[int, int]]
) -> list[ExpenseLineItem]:
    """Bangla/Banglish voice: category+route+amount or route+category+amount."""
    found: list[ExpenseLineItem] = []
    for pattern in (_TRAVEL_CAT_ROUTE_AMT_RE, _TRAVEL_ROUTE_CAT_AMT_RE):
        for m in pattern.finditer(clause):
            if _span_overlaps(covered, m.start(), m.end()):
                continue
            amt_m = _AMOUNT_RE.search(m.group("amt") or "")
            val = _parse_amount_match(amt_m) if amt_m else None
            if val is None or val <= 0:
                continue
            pair = _valid_location_pair(m.group("frm") or "", m.group("to") or "")
            if not pair:
                continue
            covered.append((m.start(), m.end()))
            found.append(
                ExpenseLineItem(
                    category=normalize_category(m.group("cat")),
                    amount=val,
                    from_location=pair[0],
                    to_location=pair[1],
                )
            )
    return found


def _extract_from_clause(clause: str) -> list[ExpenseLineItem]:
    clause = preprocess_expense_message(clause)
    found: list[ExpenseLineItem] = []
    covered: list[tuple[int, int]] = []

    route_hits = _extract_travel_route_patterns(clause, covered)
    if route_hits:
        found.extend(route_hits)
        _attach_trailing_route(clause, found)
        found.extend(_uncategorized_amounts_in_clause(clause, covered))
        return found

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
            _attach_trailing_route(clause, found)
            return found

    if not found and not re.search(rf"\b{_CATEGORY_TOKEN}\b", clause, re.I):
        alias_item = _extract_alias_category_line(clause, covered)
        if alias_item:
            return [alias_item]
        pair = _pick_best_simple_to_pair(clause)
        if pair:
            frm_raw, to_raw = pair
            tail = clause[clause.lower().rfind(to_raw.lower()) + len(to_raw) :]
            amt_m = _AMOUNT_RE.search(tail) or _AMOUNT_RE.search(clause)
            val = _parse_amount_match(amt_m) if amt_m else None
            if val and val > 0:
                if not re.search(
                    r"(?:taka|tk|টাকা|cost|kharch|hoyeche|hoyese|hocche)",
                    clause,
                    re.I,
                ):
                    return found
                return [
                    ExpenseLineItem(
                        category="",
                        amount=val,
                        from_location=pair[0],
                        to_location=pair[1],
                        notes=clause,
                    )
                ]

    # Amount without category — do not invent "Other"; workflow will ask category.
    return found


def message_contains_expense_claim_lines(message: str) -> bool:
    """
    True when the user lists new expense lines (BN / Banglish / EN).

    Uses preprocess + rules extraction — catches Bengali number words (একশ, ষাট)
    that digit-only regex would miss.
    """
    try:
        return bool(extract_expense_items(message or "").items)
    except Exception:
        return False


def extract_expense_items(message: str) -> ExtractionResult:
    """
    Extract zero or more expense line items from one user message.
    """
    raw_message = message or ""
    message = preprocess_expense_message(raw_message)
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
                if is_travel_category(it.category) and not (
                    it.from_location and it.to_location
                ):
                    pair = parse_from_to_locations(clause)
                    if pair:
                        it.from_location, it.to_location = pair
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
    items = _romanize_travel_locations(items)

    # Deduplicate identical lines in same message (accidental double-parse).
    # Route-aware for travel: same fare on different routes (e.g. mirpur→motijheel
    # vs motijheel→mirpur) are distinct trips and must NOT be collapsed.
    seen: set[tuple[Any, ...]] = set()
    unique: list[ExpenseLineItem] = []
    for it in items:
        if is_travel_category(it.category):
            key = (
                it.category,
                round(it.amount, 2),
                str(it.from_location or "").strip().lower(),
                str(it.to_location or "").strip().lower(),
            )
        else:
            key = (it.category, round(it.amount, 2))
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    return ExtractionResult(items=unique, malformed=malformed)


def _is_spurious_train_line(item: ExpenseLineItem, message: str) -> bool:
    """True when a Train line is likely metro/STT noise rather than a real train fare."""
    blob = f"{item.notes} {message}"
    return not re.search(r"\btrain\b|ট্রেন", blob, re.I)


def _collapse_metro_train_duplicates(
    items: list[ExpenseLineItem], message: str
) -> list[ExpenseLineItem]:
    """
    Drop spurious Train lines when Metro Rail was parsed from the same message.
    Common when users say metroral/metro rail (STT) without mentioning train.
    Keeps explicit train fares and kamalapur-station legs.
    """
    if not items:
        return items
    if not any(it.category == "Metro Rail" for it in items):
        return items
    if not re.search(r"metro|মেট্রো", message or "", re.I):
        return items
    out: list[ExpenseLineItem] = []
    for it in items:
        if it.category == "Train" and _is_spurious_train_line(it, message):
            continue
        out.append(it)
    return out


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
