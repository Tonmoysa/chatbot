"""Pending route edit when multiple lines share the same travel category."""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense.confusion_handler import list_amount_correction_targets
from chat.services.expense_copy import normalize_reply_lang
from chat.services.expense.command_parser import _ordinal_index_from_message

_KEY_ROUTE_CORRECTION_PENDING = "route_correction_pending"


def has_route_correction_pending(block: dict[str, Any] | None) -> bool:
    row = (block or {}).get(_KEY_ROUTE_CORRECTION_PENDING)
    return isinstance(row, dict) and bool(str(row.get("category") or "").strip())


def read_route_correction_pending(block: dict[str, Any] | None) -> dict[str, Any] | None:
    row = (block or {}).get(_KEY_ROUTE_CORRECTION_PENDING)
    return row if isinstance(row, dict) else None


def mark_route_correction_pending(
    block: dict[str, Any],
    *,
    category: str,
    from_location: str,
    to_location: str,
) -> dict[str, Any]:
    block[_KEY_ROUTE_CORRECTION_PENDING] = {
        "category": str(category or "").strip(),
        "from_location": str(from_location or "").strip(),
        "to_location": str(to_location or "").strip(),
    }
    return block


def clear_route_correction_pending(block: dict[str, Any]) -> dict[str, Any]:
    block.pop(_KEY_ROUTE_CORRECTION_PENDING, None)
    return block


def _format_route_target_line(
    target: dict[str, Any],
    number: int,
    *,
    lang: str,
) -> str:
    cat = str(target.get("category") or "?")
    amt = float(target.get("amount") or 0)
    frm = str(target.get("from_location") or "").strip()
    to = str(target.get("to_location") or "").strip()
    route = f" · {frm} → {to}" if frm and to else ""
    if lang == "en":
        return f"{number}. **{cat}**{route} · **{amt:g} Tk**"
    return f"{number}. **{cat}**{route} · **{amt:g} Tk**"


def build_route_modify_disambiguation_prompt(
    targets: list[dict[str, Any]],
    *,
    category: str,
    from_location: str,
    to_location: str,
    lang: str | None = None,
) -> str:
    reply_lang = normalize_reply_lang(lang)
    lines = [
        _format_route_target_line(t, i + 1, lang=reply_lang)
        for i, t in enumerate(targets)
    ]
    body = "\n".join(lines)
    route = f"**{from_location} → {to_location}**"
    if reply_lang == "en":
        return (
            f"Multiple **{category}** lines — which route should become {route}?\n\n"
            f"{body}\n\n"
            "Reply with the **line number** (e.g. `1`) or say "
            f"**`first {category.lower()} {from_location} to {to_location}`**."
        )
    if reply_lang == "banglish":
        return (
            f"**{category}** — {len(targets)} ta line ache. Kontar route {route} korbo?\n\n"
            f"{body}\n\n"
            "Line **number** bolen — e.g. `1` ba "
            f"**`prothom {category.lower()} {from_location} to {to_location}`**."
        )
    return (
        f"**{category}** — {len(targets)} টা line আছে। কোনটার route {route} করব?\n\n"
        f"{body}\n\n"
        "লাইনের **নম্বর** বলুন — যেমন: `1` বা "
        f"**`prothom {category.lower()} {from_location} to {to_location}`**।"
    )


def resolve_route_correction_reply(
    message: str,
    items: list[dict[str, Any]],
    block: dict[str, Any] | None,
    pending: dict[str, Any],
) -> tuple[int | None, str | None]:
    """
    Resolve follow-up to route_correction_pending.

    Returns (item_index, prompt_if_still_ambiguous).
    """
    text = (message or "").strip()
    if not text:
        return None, None

    category = str(pending.get("category") or "").strip()
    frm = str(pending.get("from_location") or "").strip()
    to = str(pending.get("to_location") or "").strip()
    targets = [
        t
        for t in list_amount_correction_targets(items, block)
        if str(t.get("category") or "").strip().lower() == category.lower()
    ]
    if not targets:
        return None, None

    num_m = re.match(r"^#?(\d{1,2})\s*$", text)
    if num_m:
        pick = int(num_m.group(1)) - 1
        if 0 <= pick < len(targets):
            return int(targets[pick].get("index", pick)), None

    low = text.lower()
    ordinal = _ordinal_index_from_message(low, item_count=len(targets))
    if ordinal is not None and 0 <= ordinal < len(targets):
        return int(targets[ordinal].get("index", ordinal)), None

    if len(targets) > 1:
        prompt = build_route_modify_disambiguation_prompt(
            targets,
            category=category,
            from_location=frm,
            to_location=to,
            lang=(block or {}).get("reply_language"),
        )
        return None, prompt

    return int(targets[0].get("index", 0)), None
