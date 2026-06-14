"""Modify existing draft lines — target pick + amount apply."""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense.active_prompt import (
    KIND_MODIFY_TARGET,
    clear_active_prompt,
    read_active_prompt,
    set_active_prompt,
)
from chat.services.expense.draft_summary import format_numbered_draft_summary
from chat.services.expense.draft_view import DraftLine, ExpenseDraftView, ensure_line_ids
from chat.services.expense_copy import normalize_reply_lang


def parse_modify_target_number(message: str) -> int | None:
    t = (message or "").strip()
    m = re.match(r"^#?(\d{1,2})\s*(?:number|নম্বর|no)?\s*$", t, re.I)
    if m:
        return int(m.group(1))
    return None


def apply_amount_to_line_id(
    items: list[dict[str, Any]],
    line_id: str,
    amount: float,
) -> list[dict[str, Any]]:
    items = ensure_line_ids(items)
    out: list[dict[str, Any]] = []
    for row in items:
        r = dict(row)
        if str(r.get("line_id") or "") == line_id:
            r["amount"] = float(amount)
        out.append(r)
    return out


def build_modify_target_prompt(
    matches: list[DraftLine],
    *,
    category: str,
    amount: float,
    lang: str | None = None,
) -> str:
    reply_lang = normalize_reply_lang(lang)
    cat_disp = category.capitalize() if category else "?"
    lines = "\n".join(ln.display_label(lang=reply_lang) for ln in matches)
    if reply_lang == "en":
        return (
            f"**{cat_disp}** — which line should I update to **{amount:g} Tk**?\n\n"
            f"{lines}\n\n"
            "Reply with the **line number** (e.g. `1`)."
        )
    return (
        f"**{cat_disp}** — কোন line **{amount:g} Tk** করব?\n\n"
        f"{lines}\n\n"
        "**নম্বর** বলুন — যেমন: `1`"
    )


def start_modify_target_prompt(
    block: dict[str, Any],
    *,
    category: str,
    amount: float,
    candidate_numbers: list[int],
) -> dict[str, Any]:
    return set_active_prompt(
        block,
        KIND_MODIFY_TARGET,
        category=category,
        amount=amount,
        candidate_numbers=list(candidate_numbers),
    )


def handle_modify_target_turn(
    *,
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    inc_iso: str,
    lang: str | None,
) -> tuple[str | None, list[dict[str, Any]]]:
    """Returns (summary_question, items) when line updated."""
    prompt = read_active_prompt(block)
    if not prompt or str(prompt.get("kind") or "") != KIND_MODIFY_TARGET:
        return None, items

    num = parse_modify_target_number(message)
    if num is None:
        view = ExpenseDraftView(items, block)
        cat = str(prompt.get("category") or "")
        amt = float(prompt.get("amount") or 0)
        matches = view.lines_by_category(cat)
        committed = [ln for ln in matches if ln.kind == "committed"]
        return (
            build_modify_target_prompt(committed, category=cat, amount=amt, lang=lang),
            items,
        )

    view = ExpenseDraftView(items, block)
    line = view.line_by_number(num)
    if not line or line.kind != "committed":
        cat = str(prompt.get("category") or "")
        amt = float(prompt.get("amount") or 0)
        matches = [ln for ln in view.lines_by_category(cat) if ln.kind == "committed"]
        return build_modify_target_prompt(matches, category=cat, amount=amt, lang=lang), items

    new_amt = float(prompt.get("amount") or 0)
    items = apply_amount_to_line_id(items, line.line_id, new_amt)
    block["items"] = items
    clear_active_prompt(block)
    q = format_numbered_draft_summary(items, block, incurred_date_iso=inc_iso, lang=lang)
    if lang == "en" or lang == "banglish":
        prefix = f"Updated **#{num} {line.category}** → **{new_amt:g} Tk**.\n\n"
    else:
        prefix = f"আপডেট — **#{num} {line.category}** → **{new_amt:g} Tk**.\n\n"
    return prefix + q, items
