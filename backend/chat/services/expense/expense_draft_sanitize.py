"""Remove phantom / corrupted expense draft rows after failed parses."""

from __future__ import annotations

import re
from typing import Any

_PHANTOM_LOCATION_FRAGMENTS = frozenset(
    {
        "baad",
        "bad",
        "diye",
        "add",
        "koro",
        "kor",
        "theke",
        "jog",
        "debo",
        "daw",
        "dao",
        "kore",
        "de",
        "dey",
        "komao",
        "komiye",
        "por",
        "pore",
        "taka",
    }
)

_PHANTOM_ROUTE_RE = re.compile(
    r"\b(baad\s+diye|add\s+koro|baad\s+diye|kore\s+daw|kore\s+de)\b",
    re.I | re.UNICODE,
)


def _location_is_phantom(label: str) -> bool:
    s = (label or "").strip().lower()
    if not s:
        return False
    if _PHANTOM_ROUTE_RE.search(s):
        return True
    words = re.findall(r"[a-zA-Z\u0980-\u09FF]+", s)
    if not words:
        return False
    lower_words = [w.lower() for w in words]
    if len(lower_words) <= 4 and all(w in _PHANTOM_LOCATION_FRAGMENTS for w in lower_words):
        return True
    return False


def is_phantom_expense_row(row: dict[str, Any]) -> bool:
    """Garbage lines from misparsed transfer/edit phrases (not real travel rows)."""
    cat = str(row.get("category") or "").strip().lower()
    frm = str(row.get("from_location") or "").strip()
    to = str(row.get("to_location") or "").strip()
    if _location_is_phantom(frm) or _location_is_phantom(to):
        return True
    if cat and (frm.lower() == cat or to.lower() == cat):
        return True
    if re.search(r"\d+\s*taka", frm, re.I) or re.search(r"\d+\s*taka", to, re.I):
        return True
    return False


def _row_fingerprint(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("category") or "").strip().lower(),
        round(float(row.get("amount") or 0), 2),
        str(row.get("from_location") or "").strip().lower(),
        str(row.get("to_location") or "").strip().lower(),
    )


def sanitize_expense_draft_block(
    block: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop phantom/duplicate rows; clear stale ack. Returns (items, block)."""
    out_block = dict(block)
    seen: set[tuple[Any, ...]] = set()
    clean_items: list[dict[str, Any]] = []
    for raw in list(out_block.get("items") or []):
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        if is_phantom_expense_row(row):
            continue
        cat = str(row.get("category") or "").strip()
        if not cat:
            continue
        key = _row_fingerprint(row)
        if key in seen:
            continue
        seen.add(key)
        clean_items.append(row)

    clean_queue: list[dict[str, Any]] = []
    for raw in list(out_block.get("pending_queue") or []):
        if not isinstance(raw, dict) or not raw.get("amount"):
            continue
        row = dict(raw)
        if is_phantom_expense_row(row):
            continue
        if not str(row.get("category") or "").strip():
            continue
        clean_queue.append(row)

    pending = out_block.get("pending_line")
    if isinstance(pending, dict) and pending.get("amount"):
        if is_phantom_expense_row(dict(pending)):
            out_block.pop("pending_line", None)
            out_block.pop("pending_step", None)

    out_block["items"] = clean_items
    out_block["pending_queue"] = clean_queue
    from chat.services.expense.pending_slot import rebalance_expense_draft_state

    return rebalance_expense_draft_state(out_block, clean_items)
