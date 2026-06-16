"""
Resolve user replies against the open pending travel line (amount + route in one turn).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from chat.services.expense.draft_view import ExpenseDraftView, ensure_line_ids
from chat.services.expense_extraction import (
    extract_expense_items,
    is_travel_category,
    parse_category_token,
    parse_from_to_locations,
    route_explicit_in_user_message,
)
from chat.services.expense.pending_routes import (
    build_pending_routes_prompt,
    consolidate_incomplete_travel_duplicates,
    try_apply_pending_routes,
)


@dataclass
class PendingSlotResult:
    handled: bool
    items: list[dict[str, Any]]
    block: dict[str, Any]
    question: str = ""


def message_completes_open_pending_travel(
    message: str,
    pending: dict[str, Any],
    *,
    pending_step: str = "",
) -> bool:
    """True when the message should update/finish the open pending line — not add a new one."""
    step = (pending_step or "").strip().lower()
    if step != "from_to":
        return False
    text = (message or "").strip()
    if not text:
        return False
    pending_cat = str(pending.get("category") or "").strip().lower()
    if not pending_cat:
        return False

    ext = extract_expense_items(text)
    if len(ext.items) == 1 and not ext.malformed:
        item = ext.items[0]
        cat = str(item.category or "").strip().lower()
        if cat == pending_cat:
            pair = _extract_route_pair(text, item)
            if pair:
                return True

    if parse_category_token(text) and str(parse_category_token(text) or "").lower() == pending_cat:
        if parse_from_to_locations(text):
            return True

    from chat.services.expense.command_parser import parse_correction_plan

    plan = parse_correction_plan(text, item_count=99)
    if plan.set_amounts and len(plan.set_amounts) == 1:
        cat, _ = plan.set_amounts[0]
        if str(cat or "").lower() == pending_cat and parse_from_to_locations(text):
            return True
    return False


def _extract_route_pair(text: str, item: Any) -> tuple[str, str] | None:
    pair = parse_from_to_locations(text)
    if pair and route_explicit_in_user_message(text, pair[0], pair[1]):
        return pair
    frm = str(getattr(item, "from_location", "") or "").strip()
    to = str(getattr(item, "to_location", "") or "").strip()
    if frm and to and route_explicit_in_user_message(text, frm, to):
        return frm, to
    return None


def pending_amount_update_for_category(message: str, pending_cat: str) -> float | None:
    """Amount in message for pending category, or None."""
    return _parse_pending_amount_update(message, pending_cat)


def _parse_pending_amount_update(message: str, pending_cat: str) -> float | None:
    from chat.services.expense.command_parser import parse_correction_plan

    text = (message or "").strip()
    plan = parse_correction_plan(text, item_count=99)
    for cat, amt in plan.set_amounts + plan.update_amounts:
        if str(cat or "").lower() == pending_cat:
            return float(amt)
    m = re.search(
        rf"\b{re.escape(pending_cat)}\b\s+(\d+(?:[.,]\d{{1,2}})?)\s*(?:taka|tk|টাকা)?",
        text,
        re.I | re.UNICODE,
    )
    if m:
        try:
            return float(str(m.group(1)).replace(",", "."))
        except (TypeError, ValueError):
            return None
    m = re.search(
        r"(\d+(?:[.,]\d{1,2})?)\s*(?:taka|tk|টাকা)?\s*(?:koro|kor|kore|habe|hobe|হবে)",
        text,
        re.I | re.UNICODE,
    )
    if m and parse_category_token(text):
        try:
            return float(str(m.group(1)).replace(",", "."))
        except (TypeError, ValueError):
            return None
    return None


def try_resolve_pending_travel_message(
    message: str,
    *,
    block: dict[str, Any],
    items: list[dict[str, Any]],
    lang: str | None = None,
) -> PendingSlotResult:
    """
    Apply amount/route updates to the open pending travel line before ingest/correction.

    Examples:
    - pending Bike 120 (no route) + ``bike 130 koro`` → amount 130, still need route
    - pending Bike 130 (no route) + ``bike gulshan to baridhara 145 taka`` → finalize 145
    - pending Bike + ``gulshan to dhanmondi`` → route only (single gap)
    """
    pending = block.get("pending_line")
    if not isinstance(pending, dict) or not pending.get("amount"):
        return PendingSlotResult(False, items, block)
    step = str(block.get("pending_step") or "").strip().lower()
    if step != "from_to":
        return PendingSlotResult(False, items, block)

    pending_cat = str(pending.get("category") or "").strip()
    if not pending_cat or not is_travel_category(pending_cat):
        return PendingSlotResult(False, items, block)

    text = (message or "").strip()
    if not text:
        return PendingSlotResult(False, items, block)

    view = ExpenseDraftView(items, block)
    gap_lines = [
        ln
        for ln in view.pending_gap_lines()
        if ln.pending_gap == "From/To needed"
    ]
    if len(gap_lines) > 1:
        route_apply = try_apply_pending_routes(block, items, text)
        if route_apply.applied_count > 0:
            from chat.services.expense_workflow import _advance_pending_queue

            new_items = consolidate_incomplete_travel_duplicates(route_apply.items)
            if str(block.get("pending_step") or "") == "from_to":
                q = build_pending_routes_prompt(block, new_items, lang=lang)
                return PendingSlotResult(True, new_items, route_apply.block, q)
            new_items, q = _advance_pending_queue(
                route_apply.block, new_items, inc_iso=str(block.get("incurred_date_iso") or "")
            )
            return PendingSlotResult(True, new_items, route_apply.block, q)
        if route_apply.routes_found and not route_apply.applied_count:
            q = build_pending_routes_prompt(block, items, lang=lang)
            return PendingSlotResult(True, items, block, q)

    pending_row = dict(pending)
    pending_cat_l = pending_cat.lower()

    ext = extract_expense_items(text)
    if len(ext.items) == 1 and not ext.malformed:
        item = ext.items[0]
        cat = str(item.category or "").strip().lower()
        if cat == pending_cat_l:
            pair = _extract_route_pair(text, item)
            try:
                amt = float(item.amount or 0)
            except (TypeError, ValueError):
                amt = 0.0
            try:
                old_amt = round(float(pending.get("amount") or 0), 2)
            except (TypeError, ValueError):
                old_amt = 0.0
            if amt > 0:
                pending_row["amount"] = amt
            if pair:
                pending_row["from_location"], pending_row["to_location"] = pair
                return _finalize_pending_row(
                    block, items, pending_row, lang=lang
                )
            if amt > 0 and abs(amt - old_amt) >= 0.01:
                block["pending_line"] = pending_row
                from chat.services.expense_workflow import _ask_from_to_prompt

                q, _ = _ask_from_to_prompt(
                    block, items, pending_cat, amt, lang=lang
                )
                return PendingSlotResult(True, items, block, q)

    pair = parse_from_to_locations(text)
    if pair and route_explicit_in_user_message(text, pair[0], pair[1]):
        cat_tok = parse_category_token(text)
        if not cat_tok or str(cat_tok).lower() == pending_cat_l:
            pending_row["from_location"], pending_row["to_location"] = pair
            return _finalize_pending_row(block, items, pending_row, lang=lang)

    amt_upd = _parse_pending_amount_update(text, pending_cat_l)
    if amt_upd is not None and amt_upd > 0:
        try:
            pending_amt = round(float(pending_row.get("amount") or 0), 2)
        except (TypeError, ValueError):
            pending_amt = 0.0
        if abs(amt_upd - pending_amt) >= 0.01:
            pending_row["amount"] = amt_upd
            block["pending_line"] = pending_row
            from chat.services.expense_workflow import _ask_from_to_prompt

            q, _ = _ask_from_to_prompt(
                block, items, pending_cat, amt_upd, lang=lang
            )
            return PendingSlotResult(True, items, block, q)

    return PendingSlotResult(False, items, block)


def _finalize_pending_row(
    block: dict[str, Any],
    items: list[dict[str, Any]],
    pending_row: dict[str, Any],
    *,
    lang: str | None,
) -> PendingSlotResult:
    from chat.services.expense_workflow import (
        _advance_pending_queue,
        _finalize_pending_line,
        _stash_expense_ack_items,
    )

    row = _finalize_pending_line(pending_row)
    if not row:
        block["pending_line"] = pending_row
        q = build_pending_routes_prompt(block, items, lang=lang)
        return PendingSlotResult(True, items, block, q)

    new_items = ensure_line_ids(list(items) + [row])
    new_items = consolidate_incomplete_travel_duplicates(new_items)
    _stash_expense_ack_items(block, [row])
    block.pop("pending_line", None)
    block.pop("pending_step", None)
    new_items, q = _advance_pending_queue(
        block, new_items, inc_iso=str(block.get("incurred_date_iso") or "")
    )
    return PendingSlotResult(True, new_items, block, q)


def rebalance_expense_draft_state(
    block: dict[str, Any],
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Single source of truth cleanup — drop duplicate incomplete travel rows."""
    from chat.services.expense.pending_routes import (
        _drop_redundant_pending_travel,
        prepare_draft_items_for_submit,
    )

    out_block = dict(block)
    cleaned = prepare_draft_items_for_submit(out_block, list(items))
    _drop_redundant_pending_travel(out_block, cleaned)

    queue = [
        dict(x)
        for x in list(out_block.get("pending_queue") or [])
        if isinstance(x, dict) and x.get("amount")
    ]
    complete_keys = {
        (
            str(r.get("category") or "").strip().lower(),
            round(float(r.get("amount") or 0), 2),
        )
        for r in cleaned
        if str(r.get("from_location") or "").strip()
        and str(r.get("to_location") or "").strip()
    }
    filtered_queue: list[dict[str, Any]] = []
    for row in queue:
        cat = str(row.get("category") or "").strip().lower()
        amt = round(float(row.get("amount") or 0), 2)
        frm = str(row.get("from_location") or "").strip()
        to = str(row.get("to_location") or "").strip()
        if frm and to:
            continue
        if (cat, amt) in complete_keys:
            continue
        filtered_queue.append(row)
    out_block["pending_queue"] = filtered_queue

    pending = out_block.get("pending_line")
    if isinstance(pending, dict) and pending.get("amount"):
        cat = str(pending.get("category") or "").strip().lower()
        amt = round(float(pending.get("amount") or 0), 2)
        frm = str(pending.get("from_location") or "").strip()
        to = str(pending.get("to_location") or "").strip()
        if (not frm or not to) and (cat, amt) in complete_keys:
            out_block.pop("pending_line", None)
            out_block.pop("pending_step", None)
            if filtered_queue:
                out_block["pending_line"] = filtered_queue[0]
                out_block["pending_queue"] = filtered_queue[1:]
                out_block["pending_step"] = "from_to"

    out_block.pop("ack_items", None)
    return cleaned, out_block
