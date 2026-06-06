"""
Expense review / confirmation gate and inline line-item corrections.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense_extraction import (
    ExpenseLineItem,
    _CATEGORY_TOKEN,
    normalize_category,
)

_CONFIRM_RE = re.compile(
    r"^(?:"
    r"yes|yep|yeah|ok|okay|confirm|submit|done|correct|right|"
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
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+"
    r"(?:(?:\d+)\s*(?:টাকা|taka|tk)?\s*)?"
    r"(?:না|na|no|not)\s+"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk|হবে|hobe)?",
    re.I,
)

_SET_AMOUNT_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?\s*(?:হবে|hobe|হয়|hoy)?",
    re.I,
)

_REMOVE_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+"
    r"(?:remove|delete|বাদ|বাদ\s*দাও|বাদ\s*দিন|remove\s*koro|bad\s*daw)",
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
    r"(?:baad|বাদ|bad)\s*(?:jabe|daw|debo|kor|koro|হবে)?",
    re.I,
)

_ADD_RE = re.compile(
    r"(?:"
    r"(?:আরও|add|plus|new|extra)\s+)?"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?\s*"
    rf"(?P<cat>{_CATEGORY_TOKEN})"
    r"|"
    rf"(?P<cat2>{_CATEGORY_TOKEN})\s+"
    r"(?:add|যোগ|jog)\s+"
    r"(?P<amt2>\d+(?:[.,]\d{1,2})?)",
    re.I,
)


def looks_like_expense_correction(message: str) -> bool:
    """Inline review edits (amount change, remove line, add line)."""
    low = message or ""
    if not low.strip():
        return False
    if re.search(
        r"\b(remove|delete|বাদ|bad\s*d(iy|i)ao|remove\s*কর)\b",
        low,
        re.I,
    ):
        return True
    if _UPDATE_AMOUNT_RE.search(low) or _SET_AMOUNT_RE.search(low):
        return True
    if _REMOVE_RE.search(low) or _REMOVE_ONE_RE.search(low) or _REMOVE_LOOSE_RE.search(low):
        return True
    if _ADD_RE.search(low):
        return True
    if re.search(r"\b(bus|lunch|train|snack|dinner|breakfast|bike|cab)\b", low, re.I):
        if re.search(r"(?:na|না)", low, re.I) and re.search(r"\d", low):
            return True
        if re.search(r"(?:hobe|হবে|update|change)", low, re.I) and re.search(r"\d", low):
            return True
    return False


def build_confirmation_question() -> str:
    return "সব তথ্য কি ঠিক আছে? (হ্যাঁ / না)"


def is_confirmation_yes(message: str) -> bool:
    from chat.services.leave_confirm import wants_defer_expense_for_leave_submit

    t = (message or "").strip()
    if wants_defer_expense_for_leave_submit(t):
        return False
    if _CONFIRM_RE.match(t):
        return True
    return bool(re.search(r"\b(confirm|submit|ঠিক\s*আছে|হ্যাঁ)\b", t, re.I))


def is_confirmation_no(message: str) -> bool:
    t = (message or "").strip()
    if _DENY_RE.match(t):
        return True
    return bool(re.search(r"\b(না|ভুল|wrong|not\s+right)\b", t, re.I))


def dedupe_expense_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop accidental duplicate lines (same category + amount)."""
    seen: set[tuple[str, float]] = set()
    out: list[dict[str, Any]] = []
    for row in items:
        cat = str(row.get("category") or "").lower()
        try:
            amt = round(float(row.get("amount") or 0), 2)
        except (TypeError, ValueError):
            out.append(dict(row))
            continue
        key = (cat, amt)
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    return out


def _set_category_amount(
    out: list[dict[str, Any]], cat: str, new_amt: float
) -> bool:
    """Update amount; collapse multiple rows of the same category to one."""
    cat_l = cat.lower()
    idxs = [
        i
        for i, row in enumerate(out)
        if str(row.get("category") or "").lower() == cat_l
    ]
    if not idxs:
        return False
    out[idxs[0]]["amount"] = new_amt
    for i in reversed(idxs[1:]):
        del out[i]
    return True


def apply_corrections(
    items: list[dict[str, Any]],
    message: str,
    *,
    extract_lines=None,
) -> tuple[list[dict[str, Any]], bool]:
    """Return updated items and whether any correction was applied."""
    changed = False
    out = [dict(x) for x in items]
    low = message or ""

    for m in _REMOVE_ONE_RE.finditer(low):
        cat = normalize_category(m.group("cat"))
        for i, row in enumerate(out):
            if str(row.get("category") or "").lower() == cat.lower():
                del out[i]
                changed = True
                break

    for m in _REMOVE_LOOSE_RE.finditer(low):
        if _REMOVE_ONE_RE.search(low):
            continue
        cat = normalize_category(m.group("cat"))
        for i, row in enumerate(out):
            if str(row.get("category") or "").lower() == cat.lower():
                del out[i]
                changed = True
                break

    for m in _REMOVE_RE.finditer(low):
        cat = normalize_category(m.group("cat"))
        before = len(out)
        out = [r for r in out if str(r.get("category") or "").lower() != cat.lower()]
        if len(out) < before:
            changed = True

    for m in _UPDATE_AMOUNT_RE.finditer(low):
        cat = normalize_category(m.group("cat"))
        raw_amt = m.group("amt").replace(",", ".")
        try:
            new_amt = float(raw_amt)
        except ValueError:
            continue
        if _set_category_amount(out, cat, new_amt):
            changed = True

    for m in _SET_AMOUNT_RE.finditer(low):
        if _UPDATE_AMOUNT_RE.search(low):
            continue
        cat = normalize_category(m.group("cat"))
        try:
            new_amt = float(m.group("amt").replace(",", "."))
        except ValueError:
            continue
        if _set_category_amount(out, cat, new_amt):
            changed = True

    for m in _ADD_RE.finditer(low):
        cat_g = m.group("cat") or m.group("cat2")
        amt_g = m.group("amt") or m.group("amt2")
        if not cat_g or not amt_g:
            continue
        try:
            new_amt = float(amt_g.replace(",", "."))
        except ValueError:
            continue
        cat = normalize_category(cat_g)
        found = False
        for row in out:
            if str(row.get("category") or "").lower() == cat.lower():
                row["amount"] = float(row.get("amount") or 0) + new_amt
                found = True
                changed = True
                break
        if not found:
            out.append(ExpenseLineItem(category=cat, amount=new_amt).to_dict())
            changed = True

    if not changed and extract_lines is not None:
        ext = extract_lines(message)
        for ni in ext.items:
            cat = ni.category
            if _set_category_amount(out, cat, float(ni.amount)):
                row = next(
                    r
                    for r in out
                    if str(r.get("category") or "").lower() == cat.lower()
                )
                if ni.from_location:
                    row["from_location"] = ni.from_location
                if ni.to_location:
                    row["to_location"] = ni.to_location
                changed = True
            else:
                out.append(ni.to_dict())
                changed = True

    if changed:
        out = dedupe_expense_items(out)
    return out, changed
