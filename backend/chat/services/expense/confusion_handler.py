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


def list_amount_correction_targets(
    items: list[dict[str, Any]],
    block: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Draft lines + open pending entries that a bare amount correction could apply to."""
    targets: list[dict[str, Any]] = []
    for idx, row in enumerate(items):
        cat = str(row.get("category") or "").strip()
        if not cat:
            continue
        targets.append(
            {
                "kind": "item",
                "index": idx,
                "category": cat,
                "amount": float(row.get("amount") or 0),
                "from_location": str(row.get("from_location") or "").strip(),
                "to_location": str(row.get("to_location") or "").strip(),
            }
        )
    if not block:
        return targets
    pending = block.get("pending_line")
    if isinstance(pending, dict) and pending.get("amount"):
        cat = str(pending.get("category") or "").strip() or "?"
        targets.append(
            {
                "kind": "pending",
                "index": 0,
                "category": cat,
                "amount": float(pending.get("amount") or 0),
                "from_location": str(pending.get("from_location") or "").strip(),
                "to_location": str(pending.get("to_location") or "").strip(),
            }
        )
    for qi, row in enumerate(block.get("pending_queue") or []):
        if not isinstance(row, dict) or not row.get("amount"):
            continue
        cat = str(row.get("category") or "").strip() or "?"
        targets.append(
            {
                "kind": "pending_queue",
                "index": qi,
                "category": cat,
                "amount": float(row.get("amount") or 0),
                "from_location": str(row.get("from_location") or "").strip(),
                "to_location": str(row.get("to_location") or "").strip(),
            }
        )
    return targets


def _format_amount_target_line(target: dict[str, Any], *, lang: str) -> str:
    cat = str(target.get("category") or "?")
    amt = float(target.get("amount") or 0)
    frm = str(target.get("from_location") or "").strip()
    to = str(target.get("to_location") or "").strip()
    if frm and to:
        return f"- **{cat}** · {frm} → {to} · **{amt:g} Tk**"
    if lang == "en":
        return f"- **{cat}** · **{amt:g} Tk**"
    return f"- **{cat}** · **{amt:g} Tk**"


def build_delete_entry_disambiguation_prompt(
    items: list[dict[str, Any]],
    block: dict[str, Any] | None,
    *,
    lang: str | None = None,
) -> str:
    """Ask which expense line to delete when user says ``delete koro`` only."""
    from chat.services.expense.confusion_handler import list_amount_correction_targets

    targets = list_amount_correction_targets(items, block)
    reply_lang = normalize_reply_lang(lang)
    lines = [_format_amount_target_line(t, lang=reply_lang) for t in targets]
    body = "\n".join(lines) if lines else "- (no lines yet)"
    if reply_lang == "en":
        return (
            "Which entry should I delete?\n\n"
            f"{body}\n\n"
            "Reply specifically — e.g. **`lunch baad daw`** or **`remove bus 100`**."
        )
    if reply_lang == "banglish":
        return (
            "Kon entry delete korbo?\n\n"
            f"{body}\n\n"
            "Specific bolen — e.g. **`lunch baad daw`** ba **`remove bus 100`**."
        )
    return (
        "কোন entry **delete** করব?\n\n"
        f"{body}\n\n"
        "নির্দিষ্ট করে বলুন — যেমন: **`lunch baad daw`** বা **`remove bus 100`**।"
    )


def build_amount_correction_disambiguation_prompt(
    targets: list[dict[str, Any]],
    new_amount: float,
    *,
    lang: str | None = None,
) -> str:
    """Ask which line should receive a bare amount update."""
    reply_lang = normalize_reply_lang(lang)
    lines = [_format_amount_target_line(t, lang=reply_lang) for t in targets]
    body = "\n".join(lines)
    if reply_lang == "en":
        return (
            f"**{new_amount:g} Tk** — which expense should I update?\n\n"
            f"{body}\n\n"
            "Reply specifically, e.g. **`bus 200 hobe`** or **`amount ta 200 kore dao bus er`**."
        )
    if reply_lang == "banglish":
        return (
            f"**{new_amount:g} Tk** — konta expense update korbo?\n\n"
            f"{body}\n\n"
            "Specific bolen — e.g. **`bus 200 hobe`** ba **`bus er amount 200 kore dao`**."
        )
    return (
        f"**{new_amount:g} Tk** — কোন expense-এর amount বদলাব?\n\n"
        f"{body}\n\n"
        "নির্দিষ্ট করে বলুন — যেমন: **`bus 200 hobe`** বা **`bus er amount 200 kore dao`**।"
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
