"""Reconcile draft lines when user assigns or corrects categories."""

from __future__ import annotations

from typing import Any

from chat.services.expense_extraction import is_travel_category


def _loc_key(label: str) -> str:
    return (label or "").strip().lower()


def _same_commute(
    a_frm: str, a_to: str, b_frm: str, b_to: str, *, reverse_ok: bool = True
) -> bool:
    af, at = _loc_key(a_frm), _loc_key(a_to)
    bf, bt = _loc_key(b_frm), _loc_key(b_to)
    if not af or not at or not bf or not bt:
        return False
    if af == bf and at == bt:
        return True
    if reverse_ok and af == bt and at == bf:
        return True
    return False


def drop_conflicting_travel_lines(
    items: list[dict[str, Any]],
    pending: dict[str, Any],
    assigned_category: str,
) -> list[dict[str, Any]]:
    """
    When user assigns a category to a pending route line, remove wrong-category
    duplicates for the same amount + commute (e.g. Bus → Metro Rail correction).
    """
    cat = (assigned_category or "").strip()
    if not cat or not is_travel_category(cat):
        return list(items)

    try:
        amt = round(float(pending.get("amount") or 0), 2)
    except (TypeError, ValueError):
        return list(items)
    if amt <= 0:
        return list(items)

    frm = str(pending.get("from_location") or "")
    to = str(pending.get("to_location") or "")
    if not frm and not to:
        return list(items)

    out: list[dict[str, Any]] = []
    for row in items:
        row_cat = str(row.get("category") or "").strip()
        try:
            row_amt = round(float(row.get("amount") or 0), 2)
        except (TypeError, ValueError):
            row_amt = 0.0
        if (
            row_cat
            and row_cat.lower() != cat.lower()
            and is_travel_category(row_cat)
            and row_amt == amt
            and _same_commute(
                frm,
                to,
                str(row.get("from_location") or ""),
                str(row.get("to_location") or ""),
            )
        ):
            continue
        out.append(dict(row))
    return out


def apply_category_hobe_correction(
    items: list[dict[str, Any]],
    category: str,
    *,
    pending: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Assign category from ``metro rail hobe`` style replies; drop wrong travel dupes."""
    cat = (category or "").strip()
    if not cat:
        return list(items), False

    out = [dict(row) for row in items]
    changed = False

    for idx, row in enumerate(out):
        if str(row.get("category") or "").strip():
            continue
        try:
            amt = float(row.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if amt <= 0:
            continue
        row["category"] = cat
        changed = True
        pending_ctx = pending or row
        out = drop_conflicting_travel_lines(out, pending_ctx, cat)
        return out, True

    if pending:
        try:
            pending_amt = round(float(pending.get("amount") or 0), 2)
        except (TypeError, ValueError):
            pending_amt = 0.0
        pending_frm = str(pending.get("from_location") or "")
        pending_to = str(pending.get("to_location") or "")
        if pending_amt > 0 and pending_frm and pending_to:
            for row in out:
                row_cat = str(row.get("category") or "").strip()
                if row_cat.lower() == cat_l:
                    continue
                if not is_travel_category(row_cat):
                    continue
                try:
                    row_amt = round(float(row.get("amount") or 0), 2)
                except (TypeError, ValueError):
                    continue
                if row_amt != pending_amt:
                    continue
                if not _same_commute(
                    pending_frm,
                    pending_to,
                    str(row.get("from_location") or ""),
                    str(row.get("to_location") or ""),
                ):
                    continue
                row["category"] = cat
                out = drop_conflicting_travel_lines(out, row, cat)
                return out, True
        out = drop_conflicting_travel_lines(out, pending, cat)
        if out != items:
            changed = True

    cat_l = cat.lower()
    travel_rows = [
        row
        for row in out
        if is_travel_category(str(row.get("category") or ""))
        and str(row.get("category") or "").lower() != cat_l
    ]
    if len(travel_rows) == 1:
        travel_rows[0]["category"] = cat
        changed = True
        out = drop_conflicting_travel_lines(out, travel_rows[0], cat)

    return out, changed


def _open_pending_travel_keys(block: dict[str, Any]) -> set[tuple[str, float]]:
    from chat.services.expense.normalization import normalize_category_label

    keys: set[tuple[str, float]] = set()
    pl = block.get("pending_line") or {}
    cat = normalize_category_label(str(pl.get("category") or ""))
    try:
        amt = round(float(pl.get("amount") or 0), 2)
    except (TypeError, ValueError):
        amt = 0.0
    if cat and is_travel_category(cat) and amt > 0:
        keys.add((cat, amt))
    for row in block.get("pending_queue") or []:
        if not isinstance(row, dict):
            continue
        cat = normalize_category_label(str(row.get("category") or ""))
        try:
            amt = round(float(row.get("amount") or 0), 2)
        except (TypeError, ValueError):
            continue
        if cat and is_travel_category(cat) and amt > 0:
            keys.add((cat, amt))
    return keys


def drop_pending_duplicate_travel_ingest(
    items: list[Any],
    *,
    message: str,
    block: dict[str, Any],
) -> list[Any]:
    """Drop LLM phantom travel lines that repeat an already-open pending route slot."""
    from chat.services.expense.entity_merge import explicit_category_mentions
    from chat.services.expense.normalization import normalize_category_label

    pending_keys = _open_pending_travel_keys(block)
    if not pending_keys:
        return items
    explicit = explicit_category_mentions(message)
    out: list[Any] = []
    for it in items:
        cat = normalize_category_label(str(getattr(it, "category", "") or ""))
        if not cat or not is_travel_category(cat):
            out.append(it)
            continue
        try:
            amt = round(float(getattr(it, "amount", 0) or 0), 2)
        except (TypeError, ValueError):
            out.append(it)
            continue
        if (cat, amt) in pending_keys and cat not in explicit:
            continue
        out.append(it)
    return out


def filter_llm_phantom_lines(
    items: list[Any],
    *,
    message: str = "",
) -> list[Any]:
    """
    Drop LLM-invented rows that fight parser truth.

    - Lunch at the same amount as an existing Snack (voice: নাস্তা ৫৫).
    - Lunch/Snack rows with travel routes (meals are not commutes).
    """
    from chat.services.expense.normalization import normalize_category_label

    del message
    snack_amounts: set[float] = set()
    for it in items:
        cat = normalize_category_label(str(getattr(it, "category", "") or ""))
        if cat != "Snack":
            continue
        try:
            amt = round(float(getattr(it, "amount", 0) or 0), 2)
        except (TypeError, ValueError):
            continue
        if amt > 0:
            snack_amounts.add(amt)

    out: list[Any] = []
    for it in items:
        cat = normalize_category_label(str(getattr(it, "category", "") or ""))
        try:
            amt = round(float(getattr(it, "amount", 0) or 0), 2)
        except (TypeError, ValueError):
            out.append(it)
            continue
        frm = str(getattr(it, "from_location", "") or "").strip()
        to = str(getattr(it, "to_location", "") or "").strip()
        if cat == "Lunch" and amt in snack_amounts:
            continue
        if cat in ("Lunch", "Snack") and frm and to:
            continue
        out.append(it)
    return out


def filter_llm_invented_travel(
    items: list[Any],
    pending_uncategorized: list[dict[str, Any]],
    message: str,
) -> list[Any]:
    """Drop LLM-added travel lines that duplicate uncategorized parser pending."""
    from chat.services.expense.entity_merge import explicit_category_mentions

    if not pending_uncategorized:
        return items

    explicit = explicit_category_mentions(message)
    out = list(items)
    for pending in pending_uncategorized:
        try:
            amt = round(float(pending.get("amount") or 0), 2)
        except (TypeError, ValueError):
            continue
        frm = str(pending.get("from_location") or "")
        to = str(pending.get("to_location") or "")
        if not frm or not to:
            continue
        filtered: list[Any] = []
        for it in out:
            cat = str(getattr(it, "category", "") or "").strip()
            if not cat or not is_travel_category(cat):
                filtered.append(it)
                continue
            if cat not in explicit and round(float(getattr(it, "amount", 0) or 0), 2) == amt:
                if _same_commute(
                    frm,
                    to,
                    str(getattr(it, "from_location", "") or ""),
                    str(getattr(it, "to_location", "") or ""),
                ):
                    continue
            filtered.append(it)
        out = filtered
    return out
