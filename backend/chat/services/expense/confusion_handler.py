"""Polite re-ask when expense parsing is uncertain (P0)."""

from __future__ import annotations

from typing import Any

from chat.services.expense_copy import normalize_reply_lang
from chat.services.expense_extraction import is_travel_category


def build_category_confusion_prompt(
    pending: dict[str, Any],
    *,
    lang: str | None = None,
) -> str:
    """Ask for travel/food category when amount (+ route) is known but category is not."""
    reply_lang = normalize_reply_lang(lang)
    try:
        amt = float(pending.get("amount") or 0)
    except (TypeError, ValueError):
        amt = 0.0
    frm = str(pending.get("from_location") or "").strip()
    to = str(pending.get("to_location") or "").strip()

    if reply_lang == "en":
        if frm and to:
            head = (
                f"I have **{amt:g} Tk** for **{frm} → {to}**, but the **category** is unclear."
            )
        else:
            head = f"I have **{amt:g} Tk**, but the **category** is unclear."
        return (
            f"{head}\n\n"
            "What was this expense? (e.g. **metro rail**, **bus**, **lunch**, **snack**)"
        )

    if frm and to:
        head = (
            f"**{amt:g} Tk** (**{frm} → {to}**) নোট করেছি — কিন্তু **category** স্পষ্ট নয়।"
        )
    else:
        head = f"**{amt:g} Tk** খরচ পেয়েছি — **category** স্পষ্ট নয়।"

    return (
        f"{head}\n\n"
        "এটা ki chilo? একটা word লিখুন — যেমন: **metro rail**, **bus**, **lunch**, **snack**"
    )


def build_remove_disambiguation_prompt(
    category: str,
    matches: list[dict[str, Any]],
    *,
    lang: str | None = None,
) -> str:
    """Ask which line to remove when multiple share the same category."""
    reply_lang = normalize_reply_lang(lang)
    lines: list[str] = []
    for row in matches:
        amt = float(row.get("amount") or 0)
        frm = str(row.get("from_location") or "").strip()
        to = str(row.get("to_location") or "").strip()
        if frm and to:
            lines.append(f"- **{category}** · {frm} → {to} · **{amt:g} Tk**")
        else:
            lines.append(f"- **{category}** · **{amt:g} Tk**")

    body = "\n".join(lines)
    if reply_lang == "en":
        return (
            f"Multiple **{category}** lines — which should I remove?\n\n"
            f"{body}\n\n"
            f"Reply e.g. **`{category.lower()} 100 baad`** or **`remove {category.lower()} 80`**."
        )
    return (
        f"**{category}** — একাধিক line আছে, কোনটি বাদ দিব?\n\n"
        f"{body}\n\n"
        f"লিখুন — যেমন: **`{category.lower()} 100 baad`** বা **`remove {category.lower()} 80`**।"
    )


def pending_needs_category_prompt(pending: dict[str, Any]) -> bool:
    if not pending:
        return False
    if str(pending.get("category") or "").strip():
        return False
    try:
        return float(pending.get("amount") or 0) > 0
    except (TypeError, ValueError):
        return False
