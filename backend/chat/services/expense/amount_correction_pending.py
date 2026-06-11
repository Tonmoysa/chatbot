"""Pending bare amount / add-to-category disambiguation (duplicate lines)."""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense.confusion_handler import (
    _format_amount_target_line,
    list_amount_correction_targets,
)
from chat.services.expense_copy import normalize_reply_lang
from chat.services.expense_extraction import parse_category_token

_KEY_AMOUNT_CORRECTION_PENDING = "amount_correction_pending"

_AMOUNT_HINT_RE = re.compile(
    r"(?:"
    r"(?:jetate|jeta|ota|oi|se|the|otate|otay|otayte|ওটাতে)\s*"
    r"(?P<a1>\d+(?:[.,]\d{1,2})?)"
    r"|"
    r"(?P<a2>\d+(?:[.,]\d{1,2})?)\s*(?:tk|taka|টাকা)?\s*"
    r"(?:ache|aache|থাক|\bta\b|টা|otate|otay|otayte|ওটাতে)"
    r"|"
    r"(?:prothom|first|1st|ditio|second|2nd|tritio|third|3rd)\b"
    r")",
    re.I | re.UNICODE,
)


def has_amount_correction_pending(block: dict[str, Any] | None) -> bool:
    row = (block or {}).get(_KEY_AMOUNT_CORRECTION_PENDING)
    return isinstance(row, dict) and row.get("amount") is not None


def read_amount_correction_pending(block: dict[str, Any] | None) -> dict[str, Any] | None:
    row = (block or {}).get(_KEY_AMOUNT_CORRECTION_PENDING)
    return row if isinstance(row, dict) else None


def mark_amount_correction_pending(
    block: dict[str, Any],
    *,
    amount: float,
    mode: str = "set",
    category: str = "",
) -> dict[str, Any]:
    block[_KEY_AMOUNT_CORRECTION_PENDING] = {
        "amount": float(amount),
        "mode": mode if mode in ("set", "add") else "set",
        "category": str(category or "").strip(),
    }
    return block


def clear_amount_correction_pending(block: dict[str, Any]) -> dict[str, Any]:
    block.pop(_KEY_AMOUNT_CORRECTION_PENDING, None)
    return block


def count_category_lines(items: list[dict[str, Any]], category: str) -> int:
    cat_l = (category or "").strip().lower()
    if not cat_l:
        return 0
    return sum(
        1
        for row in items
        if str(row.get("category") or "").strip().lower() == cat_l
    )


def plan_has_ambiguous_category_op(
    plan: Any,
    items: list[dict[str, Any]],
) -> tuple[str, float, str] | None:
    """Return (category, amount, mode) when a category op matches multiple lines."""
    for cat, amt in getattr(plan, "add_amounts", None) or []:
        if count_category_lines(items, cat) > 1:
            return (cat, float(amt), "add")
    for cat, amt in getattr(plan, "update_amounts", None) or []:
        if count_category_lines(items, cat) > 1:
            return (cat, float(amt), "set")
    for cat, amt in getattr(plan, "set_amounts", None) or []:
        if count_category_lines(items, cat) > 1:
            return (cat, float(amt), "set")
    for cat, amt in getattr(plan, "cat_er_amounts", None) or []:
        if count_category_lines(items, cat) > 1:
            return (cat, float(amt), "set")
    return None


def _filter_targets(
    targets: list[dict[str, Any]],
    *,
    category: str = "",
    amount_hint: float | None = None,
    ordinal_index: int | None = None,
) -> list[dict[str, Any]]:
    out = list(targets)
    cat_l = (category or "").strip().lower()
    if cat_l:
        out = [
            t
            for t in out
            if str(t.get("category") or "").strip().lower() == cat_l
        ]
    if amount_hint is not None:
        hint = round(float(amount_hint), 2)
        narrowed = [
            t
            for t in out
            if abs(round(float(t.get("amount") or 0), 2) - hint) < 0.01
        ]
        if narrowed:
            out = narrowed
    if ordinal_index is not None and out:
        if 0 <= ordinal_index < len(out):
            out = [out[ordinal_index]]
    return out


def _parse_ordinal_hint(message: str, *, item_count: int) -> int | None:
    from chat.services.expense.command_parser import _ordinal_index_from_message

    return _ordinal_index_from_message((message or "").lower(), item_count=item_count)


def _parse_amount_hint(message: str) -> float | None:
    low = (message or "").lower()
    if re.search(r"\b\d+\s*tatei\b", low, re.I | re.UNICODE):
        return None
    m = _AMOUNT_HINT_RE.search(message or "")
    if not m:
        return None
    raw = m.group("a1") or m.group("a2")
    if raw:
        try:
            return float(str(raw).replace(",", "."))
        except (TypeError, ValueError):
            return None
    low = (message or "").lower()
    if re.search(r"\b(?:prothom|first|1st)\b", low):
        return None
    return None


def build_duplicate_category_amount_prompt(
    targets: list[dict[str, Any]],
    *,
    amount: float,
    mode: str,
    category: str,
    lang: str | None = None,
) -> str:
    reply_lang = normalize_reply_lang(lang)
    lines = [_format_amount_target_line(t, lang=reply_lang) for t in targets]
    body = "\n".join(lines)
    action = "add" if mode == "add" else "set"
    if reply_lang == "en":
        verb = "add to" if action == "add" else "update to"
        return (
            f"**{category}** — multiple lines. Which one should I {verb} **{amount:g} Tk**?\n\n"
            f"{body}\n\n"
            f"Reply e.g. **`{category.lower()} with {targets[0].get('amount', 0):g} tk`** "
            f"or **`the one with {targets[0].get('amount', 0):g}`**."
        )
    if reply_lang == "banglish":
        verb = "add korbo" if action == "add" else "update korbo"
        return (
            f"**{category}** — {len(targets)} ta line ache. Kontate **{amount:g} Tk** {verb}?\n\n"
            f"{body}\n\n"
            f"Bolen — e.g. **`{targets[0].get('amount', 0):g} taka otate`** "
            f"ba **`prothom {category.lower()}`**."
        )
    return (
        f"**{category}** — {len(targets)} টা line আছে। কোনটায় **{amount:g} Tk** "
        f"{'যোগ' if action == 'add' else 'সেট'} করব?\n\n"
        f"{body}\n\n"
        f"লিখুন — যেমন: **`{targets[0].get('amount', 0):g} টাকা ওটাতে`**।"
    )


def resolve_amount_correction_reply(
    message: str,
    items: list[dict[str, Any]],
    block: dict[str, Any] | None,
    pending: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    """
    Resolve a follow-up to amount_correction_pending.

    Returns (target, prompt_if_still_ambiguous, updated_pending).
    """
    text = (message or "").strip()
    if not text:
        return None, None, pending

    amount = float(pending.get("amount") or 0)
    mode = str(pending.get("mode") or "set")
    category = str(pending.get("category") or "").strip()

    cat_from_msg = parse_category_token(text)
    if cat_from_msg and not category:
        category = cat_from_msg
        pending = {**pending, "category": category}

    amount_hint = _parse_amount_hint(text)
    targets = list_amount_correction_targets(items, block)
    cat_targets = _filter_targets(targets, category=category)
    ordinal = _parse_ordinal_hint(
        text,
        item_count=len(cat_targets) if cat_targets else len(targets),
    )

    narrowed = _filter_targets(
        targets,
        category=category,
        amount_hint=amount_hint,
        ordinal_index=ordinal,
    )

    if len(narrowed) == 1:
        return narrowed[0], None, pending

    if len(narrowed) > 1 and category:
        prompt = build_duplicate_category_amount_prompt(
            narrowed,
            amount=amount,
            mode=mode,
            category=category,
            lang=(block or {}).get("reply_language"),
        )
        return None, prompt, pending

    if category and count_category_lines(items, category) > 1:
        cat_targets = _filter_targets(targets, category=category)
        if len(cat_targets) > 1:
            prompt = build_duplicate_category_amount_prompt(
                cat_targets,
                amount=amount,
                mode=mode,
                category=category,
                lang=(block or {}).get("reply_language"),
            )
            return None, prompt, {**pending, "category": category}

    return None, None, pending


def apply_amount_to_target(
    items: list[dict[str, Any]],
    block: dict[str, Any] | None,
    target: dict[str, Any],
    *,
    amount: float,
    mode: str,
) -> tuple[list[dict[str, Any]], bool]:
    from chat.services.expense.command_executor import (
        _apply_bare_amount_to_target,
        _target_row_index,
    )

    out = [dict(x) for x in items]
    if mode == "add":
        kind = str(target.get("kind") or "")
        if kind == "item":
            idx = _target_row_index(target)
            if 0 <= idx < len(out):
                out[idx]["amount"] = round(float(out[idx].get("amount") or 0) + amount, 2)
                return out, True
        new_total = round(float(target.get("amount") or 0) + amount, 2)
        changed = _apply_bare_amount_to_target(out, block, target, new_total)
        return out, changed
    changed = _apply_bare_amount_to_target(out, block, target, amount)
    return out, changed
