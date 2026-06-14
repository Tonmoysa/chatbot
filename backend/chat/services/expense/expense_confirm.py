"""
Expense review / confirmation gate and inline line-item corrections.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense_extraction import (
    ExpenseLineItem,
    _CATEGORY_TOKEN,
    _looks_like_route_answer,
    extract_expense_items,
    is_travel_category,
    normalize_category,
)

_CONFIRM_RE = re.compile(
    r"^(?:"
    r"yes|yep|yeah|ok|okay|confirm|submit|done|correct|right|ha|"
    r"হ্যাঁ|হ্যা|ঠিক\s*আছে|ঠিক|জমা\s*দাও|জমা\s*দিন|"
    r"thik\s*ache|thik|hmm?\s*yes|submit\s*koro"
    r")\s*\.?$",
    re.I,
)

_DENY_RE = re.compile(
    r"^(?:"
    r"no|nope|wrong|incorrect|not\s+right|cancel|"
    r"না|ভুল|ঠিক\s*নয়|ভুল\s*আছে"
    r")\s*\.?$",
    re.I,
)

_UPDATE_AMOUNT_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN}|bos|bas)\s+"
    r"(?:(?P<old>\d+(?:[.,]\d{{1,2}})?)\s*(?:টাকা|taka|tk)?\s*)?"
    r"(?:না|na|no|not)\s*[,]?\s+"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk|হবে|hobe)?",
    re.I,
)

_SET_AMOUNT_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN}|bos|bas)\s+"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?\s*(?:হবে|hobe|হয়|hoy)?",
    re.I,
)

# "bos er expense 50 taka hobe" / "bus er khoroch 70 hobe"
_CAT_ER_EXPENSE_AMOUNT_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN}|bos|bas)\s+er\s+"
    r"(?:expense|khoroch|kharcha|cost)\s+"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?\s*(?:হবে|hobe|হয়|hoy)?",
    re.I,
)

# "lunch er amount ta 200 koro" / "bus er taka 150 kore dao"
_CAT_ER_AMOUNT_KORO_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN}|bos|bas)\s+er\s+"
    r"(?:amount|টাকা|taka)\s+"
    r"(?:ta|টা)\s+"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?\s*"
    r"(?:koro|kor|dao|daw|de|din|hobe|habe|hoy|kore\s*(?:dao|daw|de))?",
    re.I | re.UNICODE,
)

_BARE_DELETE_RE = re.compile(
    r"^(?:"
    r"(?:delete|remove|মুছ|বাদ)\s*(?:koro|kor|dao|daw|de|din|kore\s*(?:dao|daw|de))?|"
    r"(?:koro|kor|dao|daw)\s*(?:delete|remove|মুছ|বাদ)?"
    r")\s*\.?$",
    re.I | re.UNICODE,
)

_ORDINAL_IN_MESSAGE_RE = re.compile(
    r"(?:প্রথম|দ্বিতীয়|তৃতীয়|শেষ|শেষটা|first|second|third|last|prothom|ditio)\b",
    re.I | re.UNICODE,
)

_KEY_ORDINAL_AMOUNT_CONFIRM = "ordinal_amount_confirm_pending"

_CORRECTION_TYPO_RE = re.compile(r"\b(bos|bas)\b", re.I)

_REMOVE_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+"
    r"(?:remove|delete|বাদ|বাদ\s*দাও|বাদ\s*দিন|remove\s*koro|bad\s*daw)",
    re.I,
)

_REMOVE_VERB_CAT_RE = re.compile(
    rf"\b(?:remove|delete)\s+(?P<cat>{_CATEGORY_TOKEN}|rtain|rtrain|tran|trin)\b",
    re.I,
)

_REMOVE_VERB_CAT_AMT_RE = re.compile(
    rf"\b(?:remove|delete)\s+(?P<cat>{_CATEGORY_TOKEN}|rtain|rtrain|tran|trin)\s+"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?",
    re.I,
)

_CAT_HOBE_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN}|rtain|rtrain|tran|trin)\s+"
    r"(?:hobe|habe|hoy|হবে|হয়)\b",
    re.I,
)

# "Lunch · 120 Tk eta 200 hobe" — user echoes a disambiguation line
_LINE_HINT_AMOUNT_HOBE_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})"
    r"\s*[·•]\s*"
    r"(?:—|–|-)?\s*"
    r"(?P<hint>\d+(?:[.,]\d{1,2})?)\s*(?:Tk|টাকা|taka|tk)?"
    r".{0,28}?"
    r"(?:eta|eita|এটা|seta|se|that)\s*"
    r"(?P<new>\d+(?:[.,]\d{1,2})?)\s*(?:Tk|টাকা|taka|tk)?\s*"
    r"(?:hobe|habe|hoy|kor\w*)",
    re.I | re.UNICODE,
)

# "ekhon je lunch ta ache eta 155 hobe" / "je lunch ache seta 150 hobe"
_CONTEXTUAL_CAT_AMOUNT_HOBE_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN}|bos|bas)"
    r"(?:\s+ta)?(?:\s+(?:ache|ase|aache|ach[e]?))?"
    r".{0,48}?"
    rf"(?P<amt>\d+(?:[.,]\d{{1,2}})?)\s*(?:টাকা|taka|tk)?\s*"
    r"(?:hobe|habe|hoy|হবে|হয়)\b",
    re.I | re.UNICODE,
)

_CAT_AMT_BAAD_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN}|rtain|rtrain|tran|trin)[-\s]+"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?\s*"
    r"(?:baad|bad|বাদ|remove|delete)",
    re.I,
)

_REMOVE_ONE_RE = re.compile(
    r"(?:ekta|একটা|one|ek)\s+"
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+"
    r"(?:baad|বাদ|bad)\s*(?:jabe|daw|debo|kor|koro|হবে|হবে)?",
    re.I,
)

_REMOVE_LOOSE_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+.*?"
    r"(?:\bbaad\b|\bbad\b|বাদ)\s*(?:jabe|daw|debo|kor|koro|হবে)?",
    re.I,
)

# Move amount from one category to another (e.g. bus theke 50 bike e add koro).
_TRANSFER_RE = re.compile(
    rf"(?P<from_cat>{_CATEGORY_TOKEN})"
    r".{0,55}?"
    rf"(?P<amt>\d+(?:[.,]\d{{1,2}})?)\s*(?:টাকা|taka|tk)?"
    r".{0,45}?"
    r"(?:baad|komao|komiye|bad|বাদ|কম)"
    r".{0,55}?"
    rf"(?P<to_cat>{_CATEGORY_TOKEN})"
    r".{0,35}?"
    r"(?:add|jog|যোগ|daw|debo|koro|de|dey|diye)",
    re.I | re.UNICODE,
)

# Subtract a fixed amount from one category without removing the line.
_PARTIAL_DEDUCT_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})"
    r".{0,50}?"
    rf"(?P<amt>\d+(?:[.,]\d{{1,2}})?)\s*(?:টাকা|taka|tk)?"
    r".{0,25}?"
    r"(?:baad|komao|komiye|bad|বাদ|কম)"
    r"(?!.{{0,60}}?(?:add|jog|যোগ|daw|debo|koro))",
    re.I | re.UNICODE,
)

_ADD_RE = re.compile(
    r"(?:"
    r"(?:আরও|add|plus|new|extra)\s+"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?\s*"
    rf"(?P<cat>{_CATEGORY_TOKEN})"
    r"|"
    rf"(?P<cat2>{_CATEGORY_TOKEN})\s+"
    r"(?:add|যোগ|jog)\s+"
    r"(?P<amt2>\d+(?:[.,]\d{1,2})?)"
    r"|"
    rf"(?P<cat3>{_CATEGORY_TOKEN})\s+e\s+"
    r"(?P<amt3>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?\s*"
    r"(?:add|যোগ|jog)"
    r")",
    re.I,
)

_REMOVE_TRAVEL_GROUP_RE = re.compile(
    r"(?:"
    r"travel\s*(?:cost|costs|expense|expenses|kharcha|khoroch|charge|fee|line|lines)?"
    r"|transport\s*(?:cost|costs|expense|expenses|charge)?"
    r"|communication\s*(?:cost|allowance)?"
    r"|যাতায়াত(?:\s*খরচ)?"
    r")"
    r".{0,30}?"
    r"(?:remove|delete|baad|bad|বাদ|dur|drop|koro|kor|daw|debo|diye|coro|ছাড়|"
    r"remove\s*koro|bad\s*daw|baad\s*d(iy|i)ao)",
    re.I | re.UNICODE,
)

_REMOVE_TRAVEL_GROUP_ALT_RE = re.compile(
    r"(?:travel|transport|যাতায়াত).{0,25}?"
    r"(?:remove|baad|bad|বাদ|dur|drop|koro|kor|daw|debo|diye)",
    re.I | re.UNICODE,
)

_REPLACE_RE = re.compile(
    rf"(?P<from_cat>{_CATEGORY_TOKEN})"
    r"(?:\s+er|\s+ar|\s+the|\s+theke|\s+from|\s+ke)?"
    r"\s*"
    r"(?:poriborte|poribortte|instead|replace|change\s*kore|er\s*jaygay|er\s*jagay|"
    r"er\s*jaigai|er\s*jaigay|jaigai|jaigay|jaiga|take|"
    r"substitute|পরিবর্তে|বদলে|badle|bodle|poriborto)"
    r".{0,30}?"
    rf"(?:(?:tumi|you|ami|me)\s*)?(?P<to_cat>{_CATEGORY_TOKEN})"
    r"(?:\s+(?:add|koro|kor|daw|debo|diye|lagbe|den|din|coro|হবে|দাও|দিন|kore\s*daw|kore\s*de|kore\s*dao))?",
    re.I | re.UNICODE,
)

# "lunch ke snack kore daw" / "lunch snack kore daw"
_REPLACE_KORE_DAW_RE = re.compile(
    rf"(?P<from_cat>{_CATEGORY_TOKEN})(?:\s+ke)?\s+"
    rf"(?P<to_cat>{_CATEGORY_TOKEN})\s+"
    r"kore\s+(?:daw|de|dao)",
    re.I | re.UNICODE,
)

# "bus ta bike hobe" / "lunch ta snack" — common Banglish category swap
_REPLACE_TA_CAT_RE = re.compile(
    rf"(?P<from_cat>{_CATEGORY_TOKEN})\s+ta\s+"
    rf"(?P<to_cat>{_CATEGORY_TOKEN})"
    r"(?:\s+(?:hobe|habe|hoy|হবে|হয়))?\b",
    re.I | re.UNICODE,
)

# "bus ke bike koro" / "lunch ke snack kore daw"
_REPLACE_KE_KORO_RE = re.compile(
    rf"(?P<from_cat>{_CATEGORY_TOKEN})\s+ke\s+"
    rf"(?P<to_cat>{_CATEGORY_TOKEN})"
    r"(?:\s+(?:koro|kor|banay|baniye|banao|kore\s*(?:daw|de|dao)|hobe|habe|hoy))?\b",
    re.I | re.UNICODE,
)

# "bus ta ache setake bike koro" / "je lunch ta ache setake snack hobe"
_REPLACE_SETAKE_RE = re.compile(
    rf"(?P<from_cat>{_CATEGORY_TOKEN})\s+ta\s+"
    r"(?:ache|ase|ach[e]?|ay|aase)\s+"
    rf"setake\s+(?P<to_cat>{_CATEGORY_TOKEN})"
    r"(?:\s+(?:koro|kor|kore\s*(?:daw|de|dao)|hobe|habe|hoy))?\b",
    re.I | re.UNICODE,
)

# "bus hobe na bike hobe" / "bus hobe nah bki hobe" — category swap with negation
_REPLACE_HOBE_NA_RE = re.compile(
    rf"(?P<from_cat>{_CATEGORY_TOKEN}|bos|bas)\s+"
    r"(?:hobe|habe|hoy|হবে|হয়)\s+"
    r"(?:na|nah|না|নাহ)\s+"
    rf"(?P<to_cat>{_CATEGORY_TOKEN}|bos|bas|bki|baik|baike|bike)\b",
    re.I | re.UNICODE,
)

# "use 400 instead of 4000" / "400 instead of 4000" — amount-only swap on draft lines.
_AMOUNT_INSTEAD_RE = re.compile(
    r"(?:"
    r"(?:use|make|set)\s+(?P<new_amt>\d+(?:[.,]\d{1,2})?)\s+instead\s+of\s+(?P<old_amt>\d+(?:[.,]\d{1,2})?)"
    r"|"
    r"(?P<new_amt2>\d+(?:[.,]\d{1,2})?)\s+instead\s+of\s+(?P<old_amt2>\d+(?:[.,]\d{1,2})?)"
    r")",
    re.I,
)

_CATEGORY_REPLACE_PATTERNS = (
    _REPLACE_RE,
    _REPLACE_KORE_DAW_RE,
    _REPLACE_TA_CAT_RE,
    _REPLACE_KE_KORO_RE,
    _REPLACE_SETAKE_RE,
    _REPLACE_HOBE_NA_RE,
)


def _has_category_replace_pattern(text: str) -> bool:
    low = text or ""
    return any(p.search(low) for p in _CATEGORY_REPLACE_PATTERNS)


def _normalize_correction_message(message: str) -> str:
    """Fix common STT/typo tokens before correction regexes run."""
    from chat.services.bn_normalize import normalize_bn_digits

    text = normalize_bn_digits(message or "")
    text = re.sub(r"বাস\s*ভাড়া", "bus", text, flags=re.I | re.UNICODE)
    text = re.sub(r"(?<!\w)বাস(?!\w)", "bus", text, flags=re.UNICODE)
    text = re.sub(r"মেট্রো\s*রেল", "metro rail", text, flags=re.I | re.UNICODE)
    text = re.sub(r"নাস্তা", "snack", text, flags=re.UNICODE)
    text = re.sub(r"\bbki\b", "bike", text, flags=re.I)
    text = re.sub(r"\bbaik\b", "bike", text, flags=re.I)
    text = re.sub(r"\bbaike\b", "bike", text, flags=re.I)
    return _CORRECTION_TYPO_RE.sub("bus", text)


_EXPLICIT_CORRECTION_MARKER_RE = re.compile(
    r"(?:"
    r"\bhobe\b|\bhabe\b|\bhoy\b|হবে|হয়|"
    r"kore\s*d(?:aw|e|ao|in)|করে\s*দ|"
    r"\bbaad\b|\bbad\b|বাদ|remove|delete|"
    r"\bna\b|না|instead|replace|poriborte|poriborto|"
    r"amount\s+ta|টাকা\s*টা|"
    r"প্রথম|দ্বিতীয়|তৃতীয়|শেষ|শেষটা|first|second|third|last|"
    r"\bupdate\b|\bchange\b"
    r")",
    re.I | re.UNICODE,
)

_BARE_AMOUNT_KORE_DAO_RE = re.compile(
    r"(?:"
    r"(?:amount|টাকা|taka)\s+(?:ta|টা)\s+"
    r"|(?:eta|এটা|seta|সেটা)\s+"
    r")?"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?\s*"
    r"kore\s+(?:dao|daw|de|din)",
    re.I | re.UNICODE,
)


def _has_explicit_correction_marker(message: str) -> bool:
    low = _normalize_correction_message(message)
    return bool(_EXPLICIT_CORRECTION_MARKER_RE.search(low))


def _is_bare_fresh_category_amount_claim(message: str) -> bool:
    """``lunch 150`` style ingest — not a review correction."""
    text = (message or "").strip()
    if not text or _has_explicit_correction_marker(text):
        return False
    ext = extract_expense_items(text)
    if len(ext.items) != 1 or ext.malformed:
        return False
    item = ext.items[0]
    if not str(item.category or "").strip():
        return False
    try:
        return float(item.amount or 0) > 0
    except (TypeError, ValueError):
        return False


def parse_bare_amount_correction(message: str) -> float | None:
    """``amount ta 200 kore dao`` — category unspecified amount update."""
    low = _normalize_correction_message(message)
    if _ORDINAL_IN_MESSAGE_RE.search(low):
        return None
    m = _BARE_AMOUNT_KORE_DAO_RE.search(low)
    if not m:
        return None
    try:
        return float(str(m.group("amt")).replace(",", "."))
    except (TypeError, ValueError):
        return None


def looks_like_new_expense_during_pending_slot(
    message: str,
    pending: dict[str, Any],
    items: list[dict[str, Any]],
    block: dict[str, Any] | None = None,
    *,
    pending_step: str = "",
) -> bool:
    """
    Fresh categorized line while a category/from_to slot is open (queue-and-continue).

    Example: pending Bus 100 (route open) + ``lunch 150`` → new line, not correction.
    """
    step = (pending_step or "").strip().lower()
    if step not in ("category", "from_to"):
        return False
    text = (message or "").strip()
    if not text:
        return False
    if step == "from_to" and _looks_like_route_answer(text):
        return False
    if _is_fresh_multi_category_expense_claim(text):
        return True
    if looks_like_expense_correction(text):
        return False
    from chat.services.expense.command_parser import parse_correction_plan

    plan = parse_correction_plan(text, item_count=len(items or []))
    if plan.add_amounts or plan.update_amounts or plan.amount_replacements:
        return False
    if plan.set_amounts and not _is_bare_fresh_category_amount_claim(text):
        return False
    if re.search(
        r"\b(again|abar|arobar|another|extra|new|add|যোগ|jog|etao|e\s*tao)\b",
        text,
        re.I | re.UNICODE,
    ):
        ext = extract_expense_items(text)
        return bool(ext.items)
    ext = extract_expense_items(text)
    if len(ext.items) != 1 or ext.malformed:
        return False
    item = ext.items[0]
    if not str(item.category or "").strip():
        return False
    try:
        new_amt = round(float(item.amount or 0), 2)
    except (TypeError, ValueError):
        return False
    if new_amt <= 0:
        return False
    if step == "category":
        try:
            pending_amt = round(float(pending.get("amount") or 0), 2)
        except (TypeError, ValueError):
            return False
        return abs(pending_amt - new_amt) >= 0.01
    pending_cat = str(pending.get("category") or "").strip().lower()
    new_cat = str(item.category or "").strip().lower()
    if not new_cat:
        return False
    if re.search(r"\b(again|abar|arobar|another|extra|new|add|যোগ|jog)\b", text, re.I):
        return True
    if new_cat != pending_cat:
        return True
    # Same category while route slot is open — fresh ``bus 100`` adds another line.
    return _is_bare_fresh_category_amount_claim(text)


def parse_category_slot_answer(message: str) -> str | None:
    """Single-category assignment without amount, e.g. ``metro rail hobe``."""
    text = (message or "").strip()
    if not text:
        return None
    m = _CAT_HOBE_RE.search(text)
    if m:
        raw = m.group("cat")
        if raw in ("rtain", "rtrain", "tran", "trin"):
            return normalize_category("train")
        return normalize_category(raw)
    if re.search(r"\d", text):
        return None
    from chat.services.expense_extraction import parse_category_token

    return parse_category_token(text)


def _is_fresh_multi_category_expense_claim(message: str) -> bool:
    """
    Fresh multi-line ingest such as ``lunch 100, bus 200, rail 400`` or
    ``lunch 150 taka.,nasta 50 taka`` (nasta → Snack via parser).

    Must not be classified as a review correction (bare cat+amount matches _SET_AMOUNT_RE).
    """
    low = (message or "").lower().strip()
    if not low:
        return False
    try:
        ext = extract_expense_items(message)
        parsed_count = len(ext.items)
    except Exception:
        parsed_count = 0
    if parsed_count >= 2:
        if (
            _REMOVE_TRAVEL_GROUP_RE.search(low)
            or _REMOVE_TRAVEL_GROUP_ALT_RE.search(low)
            or _REPLACE_RE.search(low)
            or _REPLACE_KORE_DAW_RE.search(low)
            or _REPLACE_TA_CAT_RE.search(low)
            or _REPLACE_KE_KORO_RE.search(low)
            or _REPLACE_SETAKE_RE.search(low)
            or _TRANSFER_RE.search(low)
            or _PARTIAL_DEDUCT_RE.search(low)
            or _UPDATE_AMOUNT_RE.search(low)
        ):
            return False
        if re.search(
            r"\b(remove|delete|baad|bad|বাদ|poriborte|replace|transfer)\b", low, re.I
        ):
            return False
        if re.search(r"\b(hobe|hoy|হবে|হয়)\b", low, re.I) and re.search(
            r"\b(and|&,|na|না|update|change)\b", low, re.I
        ):
            return False
        return True
    cat_tokens = re.findall(rf"\b({_CATEGORY_TOKEN}|rail)\b", low, re.I)
    unique_cats = {normalize_category(c) for c in cat_tokens if c}
    amounts = re.findall(r"\d+(?:[.,]\d{1,2})?", low)
    if len(unique_cats) < 2 or len(amounts) < 2:
        return False
    if (
        _REMOVE_TRAVEL_GROUP_RE.search(low)
        or _REMOVE_TRAVEL_GROUP_ALT_RE.search(low)
        or _REPLACE_RE.search(low)
        or _REPLACE_KORE_DAW_RE.search(low)
        or _REPLACE_TA_CAT_RE.search(low)
        or _REPLACE_KE_KORO_RE.search(low)
        or _REPLACE_SETAKE_RE.search(low)
        or _TRANSFER_RE.search(low)
        or _PARTIAL_DEDUCT_RE.search(low)
        or _UPDATE_AMOUNT_RE.search(low)
    ):
        return False
    if re.search(
        r"\b(remove|delete|baad|bad|বাদ|poriborte|replace|transfer)\b", low, re.I
    ):
        return False
    if _ADD_RE.search(low):
        return False
    if re.search(r"\b(hobe|hoy|হবে|হয়)\b", low, re.I):
        if re.search(r"\b(and|&,|na|না|update|change)\b", low, re.I):
            return False
        if len(re.findall(r"\b(hobe|hoy|হবে|হয়)\b", low, re.I)) >= 2:
            return False
        return False
    return True


def wants_expense_draft_edit_intent(message: str) -> bool:
    """
  User wants to edit the expense draft (not leave slots).

  Examples: ``bus update korte chacchi``, ``lunch change korte chai``.
  """
    raw = (message or "").strip()
    if not raw:
        return False
    low = _normalize_correction_message(raw)
    has_edit_verb = bool(
        re.search(
            r"\b(edit|change|update|modify|correct|fix|badl|poriborto|আপডেট|সংশোধন|ঠিক\s*কর)\b",
            low,
            re.I | re.UNICODE,
        )
        or re.search(
            r"\b(korte\s+chacchi|korte\s+chai|korbo|korben|chacci|lagbe)\b",
            low,
            re.I | re.UNICODE,
        )
    )
    if not has_edit_verb:
        return False
    if re.search(
        rf"\b({_CATEGORY_TOKEN}|bos|bas|expense|খরচ|kharcha|khoroch|line|entry)\b",
        low,
        re.I | re.UNICODE,
    ):
        return True
    return bool(
        re.search(r"\b(expense|খরচ|kharcha|khoroch)\b", low, re.I | re.UNICODE)
    )


def looks_like_expense_correction(message: str) -> bool:
    """Inline review edits (amount change, remove line, add line)."""
    low = _normalize_correction_message(message)
    if not low.strip():
        return False
    if wants_expense_draft_edit_intent(message):
        return True
    if _is_bare_fresh_category_amount_claim(message):
        return False
    if parse_bare_amount_correction(message) is not None:
        return True
    if _is_fresh_multi_category_expense_claim(message):
        return False
    if _REMOVE_TRAVEL_GROUP_RE.search(low) or _REMOVE_TRAVEL_GROUP_ALT_RE.search(low):
        return True
    if (
        _REPLACE_RE.search(low)
        or _REPLACE_KORE_DAW_RE.search(low)
        or _REPLACE_TA_CAT_RE.search(low)
        or _REPLACE_KE_KORO_RE.search(low)
        or _REPLACE_SETAKE_RE.search(low)
        or _REPLACE_HOBE_NA_RE.search(low)
        or _AMOUNT_INSTEAD_RE.search(low)
    ):
        return True
    if re.search(
        r"\b(remove|delete|বাদ|bad\s*d(iy|i)ao|remove\s*কর)\b",
        low,
        re.I,
    ):
        # Affirmative delete confirm — not a correction ("হ্যাঁ delete করো")
        if _has_bn_affirmative_token(message) or re.search(
            r"^(yes|y|ok|okay|confirm|হ্যাঁ|হ্যা)\b", low, re.I | re.UNICODE
        ):
            return False
        return True
    if (
        _UPDATE_AMOUNT_RE.search(low)
        or _SET_AMOUNT_RE.search(low)
        or _CAT_ER_EXPENSE_AMOUNT_RE.search(low)
        or _CAT_ER_AMOUNT_KORO_RE.search(low)
        or _CONTEXTUAL_CAT_AMOUNT_HOBE_RE.search(low)
    ):
        return True
    if re.search(
        r"(?:প্রথম|দ্বিতীয়|তৃতীয়|শেষ|শেষটা|first|second|third|last).{0,30}"
        r"(?:entry|line|expense|খরচ).{0,20}(?:delete|remove|বাদ|মুছ)",
        low,
        re.I | re.UNICODE,
    ):
        return True
    if _TRANSFER_RE.search(low) or _PARTIAL_DEDUCT_RE.search(low):
        return True
    if (
        _REMOVE_RE.search(low)
        or _REMOVE_VERB_CAT_RE.search(low)
        or _REMOVE_VERB_CAT_AMT_RE.search(low)
        or _CAT_AMT_BAAD_RE.search(low)
        or _REMOVE_ONE_RE.search(low)
        or _REMOVE_LOOSE_RE.search(low)
    ):
        return True
    if _CAT_HOBE_RE.search(low):
        return True
    if _ADD_RE.search(low):
        return True
    _na_swap = re.compile(r"(?:^|[\s,])(?:na|না)(?:[\s,]|$)", re.I | re.UNICODE)
    if re.search(r"\b(bus|lunch|train|snack|dinner|breakfast|bike|cab|metro|rail|rickshaw|cng)\b", low, re.I):
        if _na_swap.search(low) and re.search(r"\d", low):
            return True
        if re.search(r"(?:hobe|হবে|update|change)\b", low, re.I) and re.search(r"\d", low):
            return True
    if re.search(
        r"(?:প্রথম|দ্বিতীয়|তৃতীয়|শেষ|শেষটা|first|second|third|last).{0,30}"
        r"(?:entry|line|expense|খরচ|টা).{0,30}\d+.{0,25}(?:না|na).{0,25}\d+",
        low,
        re.I | re.UNICODE,
    ):
        return True
    if re.search(
        r"(?:প্রথম|দ্বিতীয়|তৃতীয়|শেষ|শেষটা|first|second|third|last).{0,40}"
        r"(?:expense|entry|line|খরচ|টা).{0,40}\d+.{0,30}(?:ছিল|chilo|was)",
        low,
        re.I | re.UNICODE,
    ) and re.search(r"(?:করে\s*দাও|kore\s*daw|হবে|hobe|habe)", low, re.I):
        return True
    return False


_KEY_DELETE_VERIFY = "expense_delete_verify_pending"
_KEY_DELETE_INDEX = "expense_delete_verify_index"


def looks_like_bare_delete_request(message: str) -> bool:
    """``delete koro`` without naming a category/line."""
    raw = (message or "").strip()
    if not raw:
        return False
    low = _normalize_correction_message(raw)
    if not _BARE_DELETE_RE.search(low):
        return False
    from chat.services.expense_extraction import extract_expense_items

    ext = extract_expense_items(raw)
    return not ext.items


def has_ordinal_amount_confirm_pending(block: dict[str, Any] | None) -> bool:
    row = (block or {}).get(_KEY_ORDINAL_AMOUNT_CONFIRM)
    return isinstance(row, dict) and row.get("index") is not None


def read_ordinal_amount_confirm(block: dict[str, Any] | None) -> tuple[int, float] | None:
    row = (block or {}).get(_KEY_ORDINAL_AMOUNT_CONFIRM)
    if not isinstance(row, dict):
        return None
    try:
        return int(row.get("index")), float(row.get("amount") or 0)
    except (TypeError, ValueError):
        return None


def mark_ordinal_amount_confirm(
    block: dict[str, Any], *, index: int, amount: float
) -> dict[str, Any]:
    block[_KEY_ORDINAL_AMOUNT_CONFIRM] = {
        "index": int(index),
        "amount": float(amount),
    }
    return block


def clear_ordinal_amount_confirm(block: dict[str, Any]) -> dict[str, Any]:
    block.pop(_KEY_ORDINAL_AMOUNT_CONFIRM, None)
    return block


def build_ordinal_amount_confirm_prompt(
    items: list[dict[str, Any]],
    index: int,
    new_amount: float,
    *,
    lang: str | None = None,
) -> str:
    from chat.services.expense_copy import format_expense_line_bullet, normalize_reply_lang
    from chat.services.expense_workflow import format_expense_summary

    reply_lang = normalize_reply_lang(lang)
    if not (0 <= index < len(items)):
        return correction_unclear_notice(lang)
    row = items[index]
    line = format_expense_line_bullet(row, reply_lang)
    if reply_lang == "en":
        head = (
            f"Update line {index + 1} to **{new_amount:g} Tk**?\n\n"
            f"Current: {line}\n\n"
            f"Reply **yes** to confirm or **no** to keep unchanged."
        )
    elif reply_lang == "banglish":
        head = (
            f"Line {index + 1} **{new_amount:g} Tk** korbo?\n\n"
            f"Ekhon: {line}\n\n"
            f"**Yes** dile update hobe, **no** dile same thakbe."
        )
    else:
        head = (
            f"Line {index + 1} **{new_amount:g} Tk** করব?\n\n"
            f"এখন: {line}\n\n"
            f"**হ্যাঁ** দিলে update হবে, **না** দিলে আগের মতো থাকবে।"
        )
    return head


def parse_ordinal_delete_index(message: str) -> int | None:
    from chat.services.expense.command_parser import _parse_ordinal_delete_index

    return _parse_ordinal_delete_index(message)


def is_expense_delete_verify_pending(block: dict[str, Any] | None) -> bool:
    return bool((block or {}).get(_KEY_DELETE_VERIFY))


def read_expense_delete_verify_index(block: dict[str, Any] | None) -> int:
    """Pending delete line index; ``0`` is valid — never use ``or -1`` on this field."""
    raw = (block or {}).get(_KEY_DELETE_INDEX)
    if raw is None:
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def mark_expense_delete_verify(block: dict[str, Any], index: int) -> dict[str, Any]:
    block[_KEY_DELETE_VERIFY] = True
    block[_KEY_DELETE_INDEX] = int(index)
    return block


def clear_expense_delete_verify(block: dict[str, Any]) -> dict[str, Any]:
    block.pop(_KEY_DELETE_VERIFY, None)
    block.pop(_KEY_DELETE_INDEX, None)
    return block


def build_delete_confirm_prompt(
    items: list[dict[str, Any]],
    index: int,
    *,
    lang: str | None = None,
) -> str:
    if 0 <= index < len(items):
        row = items[index]
        amt = float(row.get("amount") or 0)
        cat = str(row.get("category") or "line").strip()
        return (
            f"**{index + 1} নম্বর** expense (**{cat}**, **{amt:g} Tk**) "
            f"**মুছে ফেলব**? (**হ্যাঁ** / **না**)"
        )
    return "এই expense line **delete** করব? (**হ্যাঁ** / **না**)"


def build_confirmation_question() -> str:
    return "সব তথ্য কি ঠিক আছে? (হ্যাঁ / না)"


def _has_bn_affirmative_token(text: str) -> bool:
    """Bengali affirmatives — \\b is unreliable on Indic scripts."""
    return bool(
        re.search(
            r"(?:^|[\s,;])(হ্যাঁ|হ্যা|ঠিক\s*আছে|সব\s*ঠিক|জি)(?:[\s,;।.!?]|$)",
            text or "",
            re.I | re.UNICODE,
        )
        or re.search(r"^(হ্যাঁ|হ্যা|ঠিক\s*আছে|সব\s*ঠিক|জি)(?:[\s,;।.!?]|$)", text or "", re.I)
    )


def is_confirmation_yes(message: str) -> bool:
    from chat.services.leave_confirm import wants_defer_expense_for_leave_submit

    t = (message or "").strip()
    if wants_defer_expense_for_leave_submit(t):
        return False
    try:
        from chat.services.expense.wizard_commands import wants_expense_submit_command

        if wants_expense_submit_command(t):
            return False
    except Exception:
        pass
    try:
        from chat.services.expense_workflow import wants_expense_summary

        if wants_expense_summary(t):
            return False
    except Exception:
        pass
    if _CONFIRM_RE.match(t):
        return True
    if _has_bn_affirmative_token(t):
        return True
    return bool(re.search(r"\b(confirm|yes|ok|okay)\b", t, re.I))


def is_submit_confirm_yes(message: str) -> bool:
    """Final CRM gate: yes / ha / submit / joma daw / UI yes chip."""
    from chat.services.leave_confirm import wants_defer_expense_for_leave_submit

    t = (message or "").strip()
    if not t or is_confirmation_no(t):
        return False
    if wants_defer_expense_for_leave_submit(t):
        return False
    try:
        from chat.services.expense.wizard_commands import wants_expense_submit_command

        if wants_expense_submit_command(t):
            # Combined affirmative + submit at CRM gate counts as yes.
            if re.search(
                r"(?:ঠিক\s*আছে|সব\s*ঠিক|হ্যাঁ|yes|confirm)",
                t,
                re.I | re.UNICODE,
            ):
                return True
            if re.search(
                r"(?:expense|খরচ|application|report).{0,50}(?:submit|জমা)",
                t,
                re.I | re.UNICODE,
            ):
                return False
            return True
    except Exception:
        pass
    if _CONFIRM_RE.match(t):
        return True
    if _has_bn_affirmative_token(t):
        return True
    return bool(re.search(r"\b(confirm|yes|ok|okay)\b", t, re.I))


def is_confirmation_no(message: str) -> bool:
    t = (message or "").strip()
    if _DENY_RE.match(t):
        return True
    # Explicit cancel: "না delete করো না", "no don't delete"
    if re.search(r"^(?:no|nope|না)\b", t, re.I) and re.search(
        r"(?:delete|remove|মুছ|বাতিল|kor[o]?\s*na|koro\s*na)\b",
        t,
        re.I | re.UNICODE,
    ):
        return True
    if re.search(
        r"^(?:no|nope|না)\b.*(?:delete|remove|মুছ).*(?:না|na|no)\s*$",
        t,
        re.I | re.UNICODE,
    ):
        return True
    # Standalone denial only — not "bus 50 na 70 hobe" style corrections.
    if len(re.findall(r"\S+", t)) <= 2:
        return bool(re.search(r"^(no|nope|wrong|incorrect|না|ভুল)\b", t, re.I))
    return False


def expense_line_fingerprint(row: dict[str, Any]) -> tuple[Any, ...]:
    """Identity for one draft line — allows multiple same category/amount with different routes."""
    try:
        amt = round(float(row.get("amount") or 0), 2)
    except (TypeError, ValueError):
        amt = 0.0
    return (
        str(row.get("category") or "").strip().lower(),
        amt,
        str(row.get("from_location") or "").strip().lower(),
        str(row.get("to_location") or "").strip().lower(),
        str(row.get("notes") or "").strip().lower(),
    )


def dedupe_expense_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shallow copy only — identical lines are allowed (unlimited entries per category)."""
    return [dict(row) for row in items]


def duplicate_reentry_notice(lang: str | None = None) -> str:
    """Brief ack when user repeats the same expense message (draft unchanged)."""
    if lang == "en":
        return (
            "You sent the same expense again — draft **unchanged** (no duplicate lines added). "
            "Say **yes** to continue, or correct a line (e.g. **bus 70 hobe**)."
        )
    if lang == "banglish":
        return (
            "Same message abar pathiyechilen — draft **unchanged** (duplicate add kori nai). "
            "**yes** din, ba change korte bolun (e.g. bus 70 hobe)."
        )
    return (
        "একই তথ্য আবার পাঠিয়েছেন — draft **অপরিবর্তিত** (duplicate যোগ করিনি)। "
        "**yes** দিন, বা বদলাতে বলুন (যেমন: bus 70 hobe)।"
    )


def looks_like_compound_expense_claim(message: str) -> bool:
    """Multi-line / multi-category expense utterance (re-ingest risk)."""
    if looks_like_expense_correction(message):
        return False
    try:
        from chat.services.expense_extraction import extract_expense_items

        ext = extract_expense_items(message or "")
        if len(ext.items) >= 2:
            return True
    except Exception:
        pass
    low = (message or "").lower()
    # "bus 50 hobe and bike 150 hobe" — correction, not a fresh compound claim.
    if re.search(r"\b(hobe|hoy|update|change)\b", low, re.I) and not re.search(
        r"\b(hoyeche|hoyeche|cost\s+hoy)\b", low, re.I
    ):
        if re.search(r"\d", message or ""):
            return False
    cats = len(
        re.findall(
            r"\b(bus|bike|lunch|snack|train|metro|rail|cng|rickshaw|travel)\b", low
        )
    )
    amounts = len(re.findall(r"\d+", message or ""))
    if cats >= 2 and amounts >= 2:
        return True
    if re.search(r"\bthen\b", low) and cats >= 1 and amounts >= 2:
        return True
    if re.search(r"[,;]\s*\w", low) and cats >= 2:
        return True
    try:
        from chat.services.intent_detector import _strong_expense_claim

        if _strong_expense_claim(message) and cats >= 2:
            return True
    except Exception:
        pass
    return False


def review_denial_hints(lang: str | None = None) -> str:
    """Hints after user says no at review — not an update acknowledgement."""
    if lang == "en":
        return (
            "Which line should we fix? Examples:\n"
            "- **bus 70 hobe** (change amount)\n"
            "- **lunch baad daw** (remove a line)\n"
            "- **bus theke 50 bike e add koro** (move amount between lines)"
        )
    if lang == "banglish":
        return (
            "Kon line thik korben? Example:\n"
            "- **bus 70 hobe**\n"
            "- **lunch baad daw**\n"
            "- **bus theke 50 bike e add koro**"
        )
    return (
        "কোন line ঠিক করবেন? উদাহরণ:\n"
        "- **bus 70 hobe** (amount বদল)\n"
        "- **lunch baad daw** (line বাদ)\n"
        "- **bus theke 50 bike e add koro** (এক line theke অন্যটিতে shift)"
    )


def correction_unclear_notice(lang: str | None = None) -> str:
    if lang == "en":
        return (
            "I kept your current expense review unchanged. "
            "Please say the correction more specifically (see examples below)."
        )
    if lang == "banglish":
        return (
            "Apnar expense review same rekhechi. Correction ta aro specific bolen (niche example)."
        )
    return (
        "আপনার expense review **আগের মতোই** রেখেছি। "
        "correction টা আরও স্পষ্ট করে বলুন (নিচে example)।"
    )


def looks_like_duplicate_expense_reentry(
    message: str, items: list[dict[str, Any]]
) -> bool:
    """True when user re-sends a compound expense claim overlapping the current draft."""
    if not items:
        return False
    if looks_like_expense_correction(message):
        return False
    if not looks_like_compound_expense_claim(message):
        return False
    from chat.services.expense_extraction import extract_expense_items

    ext = extract_expense_items(message)
    if not ext.items:
        return False
    existing_cats = {
        str(r.get("category") or "").lower() for r in items if r.get("category")
    }
    parsed_cats = {ni.category.lower() for ni in ext.items if ni.category}
    if not parsed_cats:
        return False
    overlap = len(existing_cats & parsed_cats)
    if len(items) >= 2:
        return overlap >= 2
    return overlap >= 1 and len(parsed_cats) >= 2


def _adjust_category_amount(
    out: list[dict[str, Any]], cat: str, delta: float
) -> bool:
    cat_l = cat.lower()
    for row in out:
        if str(row.get("category") or "").lower() == cat_l:
            row["amount"] = max(0.0, round(float(row.get("amount") or 0) + delta, 2))
            return True
    return False


def _prune_zero_lines(out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in out if float(r.get("amount") or 0) > 0.009]


def _set_category_amount(
    out: list[dict[str, Any]], cat: str, new_amt: float
) -> bool:
    """Update the first matching category row; keep other lines (e.g. two bus routes)."""
    cat_l = cat.lower()
    for row in out:
        if str(row.get("category") or "").lower() == cat_l:
            row["amount"] = new_amt
            return True
    return False


def _replace_category(
    out: list[dict[str, Any]], from_cat: str, to_cat: str
) -> bool:
    from_l = from_cat.lower()
    to_l = to_cat.lower()
    if from_l == to_l:
        return False
    changed = False
    for row in out:
        if str(row.get("category") or "").lower() == from_l:
            row["category"] = to_cat
            changed = True
    return changed


def _remove_travel_lines(out: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    before = len(out)
    kept = [r for r in out if not is_travel_category(str(r.get("category") or ""))]
    return kept, before - len(kept)


def _travel_lines_present(items: list[dict[str, Any]]) -> bool:
    return any(is_travel_category(str(r.get("category") or "")) for r in items)


def _category_present(items: list[dict[str, Any]], cat: str) -> bool:
    cat_l = cat.lower()
    return any(str(r.get("category") or "").lower() == cat_l for r in items)


def wants_travel_group_remove(message: str) -> bool:
    low = message or ""
    return bool(
        _REMOVE_TRAVEL_GROUP_RE.search(low) or _REMOVE_TRAVEL_GROUP_ALT_RE.search(low)
    )


def wants_category_replace(message: str) -> bool:
    text = message or ""
    return _has_category_replace_pattern(text)


def travel_group_not_found_notice(
    items: list[dict[str, Any]], *, lang: str | None = None
) -> str:
    if lang == "en":
        return (
            "No **travel-related** line (Bus/Bike/Train/CNG/…) found in your expense draft. "
            "Nothing was removed."
        )
    if lang == "banglish":
        return (
            "Draft-e **travel-related** line (Bus/Bike/Train/…) nai — kichu remove hoyni."
        )
    return (
        "আপনার expense draft-এ **travel-related** line (Bus/Bike/Train/CNG/…) "
        "খুঁজে পাইনি — কিছু বাদ দেওয়া হয়নি।"
    )


def replace_not_found_notice(
    from_cat: str, *, lang: str | None = None
) -> str:
    if lang == "en":
        return (
            f"Could not find **{from_cat}** in your draft to replace. "
            f"Check the category name or say e.g. **bike er poriborte train**."
        )
    if lang == "banglish":
        return (
            f"Draft-e **{from_cat}** pai ni replace korar jonno. "
            f"Example: **bike er poriborte train**."
        )
    return (
        f"Draft-এ **{from_cat}** খুঁজে পাইনি — replace করা যায়নি। "
        f"উদাহরণ: **bike er poriborte train add koro**।"
    )


def build_correction_failure_notice(
    message: str,
    items: list[dict[str, Any]],
    *,
    lang: str | None = None,
    block: dict[str, Any] | None = None,
) -> str | None:
    """Explicit feedback when a correction was attempted but nothing changed."""
    if not looks_like_expense_correction(message):
        return None
    low = message or ""
    if looks_like_bare_delete_request(message):
        from chat.services.expense.delete_flow import (
            build_numbered_delete_prompt,
            start_numbered_delete,
        )
        from chat.services.expense.draft_view import ExpenseDraftView

        if block is not None:
            start_numbered_delete(block)
        view = ExpenseDraftView(items, block)
        return build_numbered_delete_prompt(view, lang=lang)

    bare_amt = parse_bare_amount_correction(message)
    if bare_amt is not None:
        from chat.services.expense.confusion_handler import (
            build_amount_correction_disambiguation_prompt,
            list_amount_correction_targets,
        )

        targets = list_amount_correction_targets(items, block)
        if len(targets) > 1:
            return build_amount_correction_disambiguation_prompt(
                targets, bare_amt, lang=lang
            )
    if wants_travel_group_remove(low) and not _travel_lines_present(items):
        return travel_group_not_found_notice(items, lang=lang)
    from chat.services.expense.command_parser import parse_correction_plan

    plan = parse_correction_plan(message, item_count=len(items))
    for cat, rm_amt in plan.remove_by_amount:
        from chat.services.expense.confusion_handler import list_amount_correction_targets

        target_amt = round(float(rm_amt), 2)
        matches = [
            t
            for t in list_amount_correction_targets(items, block)
            if str(t.get("category") or "").strip().lower() == cat.lower()
            and abs(round(float(t.get("amount") or 0), 2) - target_amt) < 0.01
        ]
        if len(matches) > 1:
            from chat.services.expense.confusion_handler import (
                build_remove_disambiguation_prompt,
            )
            from chat.services.expense.delete_disambiguation_pending import (
                mark_delete_disambiguation_pending,
            )

            rows = [
                {
                    "category": str(t.get("category") or ""),
                    "amount": float(t.get("amount") or 0),
                    "from_location": str(t.get("from_location") or ""),
                    "to_location": str(t.get("to_location") or ""),
                }
                for t in matches
            ]
            if block is not None:
                mark_delete_disambiguation_pending(block)
            return build_remove_disambiguation_prompt(cat, rows, lang=lang)
    for cat in plan.remove_verb_first + plan.remove_loose:
        matches = [
            row
            for row in items
            if str(row.get("category") or "").lower() == cat.lower()
        ]
        if len(matches) > 1:
            from chat.services.expense.confusion_handler import (
                build_remove_disambiguation_prompt,
            )
            from chat.services.expense.delete_disambiguation_pending import (
                mark_delete_disambiguation_pending,
            )

            if block is not None:
                mark_delete_disambiguation_pending(block)
            return build_remove_disambiguation_prompt(cat, matches, lang=lang)
    from chat.services.expense.confusion_handler import list_amount_correction_targets

    for pattern in _CATEGORY_REPLACE_PATTERNS:
        m = pattern.search(low)
        if not m or "from_cat" not in m.groupdict():
            continue
        from_cat = normalize_category(m.group("from_cat"))
        to_cat = normalize_category(m.group("to_cat")) if m.groupdict().get("to_cat") else ""
        targets = [
            t
            for t in list_amount_correction_targets(items, block)
            if str(t.get("category") or "").strip().lower() == from_cat.lower()
        ]
        if len(targets) > 1 and to_cat:
            from chat.services.expense.confusion_handler import (
                build_category_replace_disambiguation_prompt,
            )

            return build_category_replace_disambiguation_prompt(
                targets,
                from_category=from_cat,
                to_category=to_cat,
                lang=lang,
            )
        if not targets and not _category_present(items, from_cat):
            return replace_not_found_notice(from_cat, lang=lang)
    if wants_expense_draft_edit_intent(message):
        from chat.services.expense.confusion_handler import list_amount_correction_targets

        targets = list_amount_correction_targets(items, block)
        if targets:
            lines = [
                f"- **{t.get('category')}** · **{float(t.get('amount') or 0):g} Tk**"
                for t in targets
            ]
            body = "\n".join(lines)
            if lang == "en":
                return (
                    "Which expense line should I update?\n\n"
                    f"{body}\n\n"
                    "Examples: **`bus 100 bike hobe`**, **`lunch ta snack hobe`**, "
                    "**`remove snack`**."
                )
            if lang == "banglish":
                return (
                    "Kon expense line update korbo?\n\n"
                    f"{body}\n\n"
                    "Example: **`bus 100 bike hobe`**, **`lunch ta snack hobe`**, "
                    "**`snack baad daw`**."
                )
            return (
                "কোন expense line আপডেট করব?\n\n"
                f"{body}\n\n"
                "উদাহরণ: **`bus 100 bike hobe`**, **`lunch ta snack hobe`**, "
                "**`snack baad daw`**।"
            )
    return correction_unclear_notice(lang)


def apply_corrections(
    items: list[dict[str, Any]],
    message: str,
    *,
    extract_lines=None,
) -> tuple[list[dict[str, Any]], bool]:
    """Return updated items and whether any correction was applied."""
    from chat.services.expense.command_executor import apply_message_corrections

    result = apply_message_corrections(items, message, extract_lines=extract_lines)
    return result.items, result.changed
