"""
Apply From/To routes to open pending travel lines — one or many per turn.

Uses draft numbering (#3, #4) when the user specifies targets; otherwise
maps routes in message order to pending slots (pending_line first, then queue).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from chat.services.expense.draft_view import DraftLine, ExpenseDraftView
from chat.services.expense.expense_fsm import set_expense_stage
from chat.services.expense.slots import STAGE_COLLECTING
from chat.services.expense_copy import ask_from_to_prompt, normalize_reply_lang
from chat.services.expense_extraction import (
    is_travel_category,
    parse_from_to_locations,
    route_explicit_in_user_message,
)

_NUMBERED_ROUTE_PART_RE = re.compile(
    r"^#?\s*(?P<num>\d{1,2})\s*(?:no|নম্বর|number)?\s*(?:er|এর|ta|টা)?\s*[:.\-]?\s*(?P<body>.+)$",
    re.I | re.UNICODE,
)


@dataclass
class PendingRouteApplyResult:
    items: list[dict[str, Any]]
    block: dict[str, Any]
    applied_count: int = 0
    routes_found: bool = False


def _answer_segments(message: str) -> list[str]:
    from chat.services.expense.clarify import _answer_segments as split

    parts = split(message)
    return parts if parts else [(message or "").strip()]


def parse_route_segments(message: str) -> list[tuple[str, str]]:
    """Explicit routes in message order (skips numbered clause bodies)."""
    pairs: list[tuple[str, str]] = []
    for seg in _answer_segments(message):
        text = (seg or "").strip()
        if not text:
            continue
        if _NUMBERED_ROUTE_PART_RE.match(text):
            continue
        pair = parse_from_to_locations(text)
        if pair and route_explicit_in_user_message(text, pair[0], pair[1]):
            pairs.append(pair)
    if not pairs:
        pair = parse_from_to_locations(message)
        if pair and route_explicit_in_user_message(message, pair[0], pair[1]):
            pairs.append(pair)
    return pairs


def parse_numbered_route_assignments(message: str) -> dict[int, tuple[str, str]]:
    """Map draft line numbers to routes, e.g. ``#3 mirpur to motijheel``."""
    out: dict[int, tuple[str, str]] = {}
    for seg in _answer_segments(message):
        text = (seg or "").strip()
        if not text:
            continue
        m = _NUMBERED_ROUTE_PART_RE.match(text)
        if not m:
            continue
        body = (m.group("body") or "").strip()
        pair = parse_from_to_locations(body)
        if pair and route_explicit_in_user_message(body, pair[0], pair[1]):
            out[int(m.group("num"))] = pair
    return out


def _pending_row_for_line(block: dict[str, Any], line: DraftLine) -> dict[str, Any]:
    if line.kind == "pending":
        return dict(block.get("pending_line") or {})
    if line.kind == "pending_queue":
        queue = list(block.get("pending_queue") or [])
        if 0 <= line.source_index < len(queue):
            return dict(queue[line.source_index])
    return {}


def _gap_route_lines(view: ExpenseDraftView) -> list[DraftLine]:
    return [
        ln
        for ln in view.lines
        if ln.pending_gap == "From/To needed"
        and ln.category
        and is_travel_category(ln.category)
    ]


def build_pending_routes_prompt(
    block: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    lang: str | None = None,
) -> str:
    """Ask for one or more missing routes with draft line numbers."""
    reply_lang = normalize_reply_lang(lang)
    view = ExpenseDraftView(items, block)
    gaps = _gap_route_lines(view)
    if not gaps:
        return ask_from_to_prompt("Bus", 0, reply_lang, include_lead=False)
    if len(gaps) == 1:
        ln = gaps[0]
        return ask_from_to_prompt(
            ln.category.capitalize() if ln.category else "Bus",
            ln.amount,
            reply_lang,
        )

    numbered = "\n".join(
        f"#{ln.number} {ln.category.capitalize()} — {ln.amount:g} Tk"
        for ln in gaps
    )
    if reply_lang == "en":
        return (
            "These lines still need **From** and **To**:\n"
            f"{numbered}\n\n"
            "Reply with **all routes in one message** — in order, or by number:\n"
            "`mirpur to motijheel and motijheel to mirpur`\n"
            "or `#3 mirpur to motijheel, #4 motijheel to mirpur`"
        )
    if reply_lang == "banglish":
        return (
            "Ei line gulote **From/To** lagbe:\n"
            f"{numbered}\n\n"
            "Ek message e **sob route** dite paren — order e, ba number diye:\n"
            "`mirpur to motijheel and motijheel to mirpur`\n"
            "ba `#3 mirpur to motijheel, #4 motijheel to mirpur`"
        )
    return (
        "এই লাইনগুলোতে **From/To** লাগে:\n"
        f"{numbered}\n\n"
        "এক মেসেজে **সব route** দিতে পারেন — ক্রমে, বা নম্বর দিয়ে:\n"
        "`mirpur to motijheel and motijheel to mirpur`\n"
        "বা `#3 mirpur to motijheel, #4 motijheel to mirpur`"
    )


def try_apply_pending_routes(
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
) -> PendingRouteApplyResult:
    """Apply explicit route(s) from the user reply to open pending travel lines."""
    from chat.services.expense_workflow import _finalize_pending_line

    view = ExpenseDraftView(items, block)
    gap_lines = _gap_route_lines(view)
    if not gap_lines:
        return PendingRouteApplyResult(list(items), block, 0, False)

    numbered = parse_numbered_route_assignments(message)
    routes = parse_route_segments(message)
    if not numbered and not routes:
        return PendingRouteApplyResult(list(items), block, 0, False)

    assignments: dict[int, tuple[str, str]] = dict(numbered)
    remaining = [ln for ln in gap_lines if ln.number not in assignments]
    route_idx = 0
    for ln in remaining:
        if route_idx >= len(routes):
            break
        assignments[ln.number] = routes[route_idx]
        route_idx += 1

    if not assignments:
        return PendingRouteApplyResult(list(items), block, 0, routes_found=True)

    new_items = list(items)
    still_pending: list[dict[str, Any]] = []
    applied = 0

    for ln in gap_lines:
        row = _pending_row_for_line(block, ln)
        if not row:
            continue
        if ln.number in assignments:
            frm, to = assignments[ln.number]
            row["from_location"] = frm
            row["to_location"] = to
            finalized = _finalize_pending_line(row)
            if finalized:
                from chat.services.expense.expense_confirm import expense_line_fingerprint

                fp = expense_line_fingerprint(finalized)
                if not any(expense_line_fingerprint(r) == fp for r in new_items):
                    new_items.append(finalized)
                    applied += 1
        else:
            still_pending.append(row)

    if still_pending:
        block["pending_line"] = still_pending[0]
        block["pending_queue"] = still_pending[1:]
        block["pending_step"] = "from_to"
        set_expense_stage(block, STAGE_COLLECTING)
    else:
        block.pop("pending_line", None)
        block.pop("pending_queue", None)
        block.pop("pending_step", None)

    return PendingRouteApplyResult(new_items, block, applied, routes_found=True)
