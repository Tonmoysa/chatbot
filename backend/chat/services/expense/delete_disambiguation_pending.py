"""Follow-up resolution after bare ``delete koro`` (which line to remove)."""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense.confusion_handler import (
    build_delete_entry_disambiguation_prompt,
    build_remove_disambiguation_prompt,
    list_amount_correction_targets,
)
from chat.services.expense_copy import normalize_reply_lang
from chat.services.expense_extraction import parse_category_token

_KEY_DELETE_DISAMBIGUATION_PENDING = "delete_disambiguation_pending"

_CAT_AMT_DELETE_SUFFIX_RE = re.compile(
    r"(?P<cat>[a-zA-Z\u0980-\u09FF]+)[-\s]+"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?\s*"
    r"(?:delete|remove|baad|bad|বাদ|মুছ|drop)",
    re.I | re.UNICODE,
)


def has_delete_disambiguation_pending(block: dict[str, Any] | None) -> bool:
    return bool((block or {}).get(_KEY_DELETE_DISAMBIGUATION_PENDING))


def mark_delete_disambiguation_pending(block: dict[str, Any]) -> dict[str, Any]:
    block[_KEY_DELETE_DISAMBIGUATION_PENDING] = True
    return block


def clear_delete_disambiguation_pending(block: dict[str, Any]) -> dict[str, Any]:
    block.pop(_KEY_DELETE_DISAMBIGUATION_PENDING, None)
    return block


def _target_rows_for_category(
    targets: list[dict[str, Any]], category: str
) -> list[dict[str, Any]]:
    cat_l = (category or "").strip().lower()
    return [
        t
        for t in targets
        if str(t.get("category") or "").strip().lower() == cat_l
    ]


def _target_to_row_dict(target: dict[str, Any]) -> dict[str, Any]:
    return {
        "category": str(target.get("category") or ""),
        "amount": float(target.get("amount") or 0),
        "from_location": str(target.get("from_location") or "").strip(),
        "to_location": str(target.get("to_location") or "").strip(),
    }


def _narrow_by_amount(
    targets: list[dict[str, Any]], amount: float
) -> list[dict[str, Any]]:
    hint = round(float(amount), 2)
    return [
        t
        for t in targets
        if abs(round(float(t.get("amount") or 0), 2) - hint) < 0.01
    ]


def _parse_category_amount_delete(message: str) -> tuple[str, float] | None:
    from chat.services.expense.command_parser import parse_correction_plan
    from chat.services.expense_extraction import normalize_category

    plan = parse_correction_plan(message)
    for cat, amt in plan.remove_by_amount:
        return normalize_category(cat), float(amt)

    m = _CAT_AMT_DELETE_SUFFIX_RE.search(message or "")
    if m:
        try:
            return normalize_category(m.group("cat")), float(
                str(m.group("amt")).replace(",", ".")
            )
        except (TypeError, ValueError):
            return None
    return None


def _parse_bare_category(message: str) -> str | None:
    text = (message or "").strip()
    if not text or re.search(r"\d", text):
        return None
    if re.search(
        r"\b(delete|remove|baad|bad|বাদ|মুছ|koro|kor|daw|dao)\b",
        text,
        re.I | re.UNICODE,
    ):
        return None
    cat = parse_category_token(text)
    if not cat:
        return None
    words = re.findall(r"[\w\u0980-\u09FF]+", text, re.UNICODE)
    if len(words) <= 1:
        return cat
    return None


def resolve_delete_disambiguation_reply(
    message: str,
    items: list[dict[str, Any]],
    block: dict[str, Any] | None,
    *,
    lang: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Resolve a follow-up to delete_disambiguation_pending.

    Returns (target, prompt_if_still_ambiguous).
    """
    text = (message or "").strip()
    if not text:
        return None, None

    reply_lang = normalize_reply_lang(lang)
    targets = list_amount_correction_targets(items, block)

    parsed = _parse_category_amount_delete(text)
    if parsed:
        cat, amt = parsed
        cat_targets = _target_rows_for_category(targets, cat)
        narrowed = _narrow_by_amount(cat_targets, amt)
        if len(narrowed) == 1:
            return narrowed[0], None
        if len(narrowed) > 1:
            rows = [_target_to_row_dict(t) for t in narrowed]
            return None, build_remove_disambiguation_prompt(cat, rows, lang=reply_lang)

    from chat.services.expense.command_parser import parse_correction_plan

    plan = parse_correction_plan(text, item_count=len(items))
    for cat in plan.remove_verb_first:
        cat_targets = _target_rows_for_category(targets, cat)
        if len(cat_targets) == 1:
            return cat_targets[0], None
        if len(cat_targets) > 1:
            rows = [_target_to_row_dict(t) for t in cat_targets]
            return None, build_remove_disambiguation_prompt(cat, rows, lang=reply_lang)

    for cat in plan.remove_loose:
        cat_targets = _target_rows_for_category(targets, cat)
        if len(cat_targets) == 1:
            return cat_targets[0], None
        if len(cat_targets) > 1:
            rows = [_target_to_row_dict(t) for t in cat_targets]
            return None, build_remove_disambiguation_prompt(cat, rows, lang=reply_lang)

    bare_cat = _parse_bare_category(text)
    if bare_cat:
        cat_targets = _target_rows_for_category(targets, bare_cat)
        if len(cat_targets) == 1:
            return cat_targets[0], None
        if len(cat_targets) > 1:
            rows = [_target_to_row_dict(t) for t in cat_targets]
            return None, build_remove_disambiguation_prompt(bare_cat, rows, lang=reply_lang)

    if targets:
        return None, build_delete_entry_disambiguation_prompt(items, block, lang=reply_lang)
    return None, None


def apply_delete_target(
    items: list[dict[str, Any]],
    block: dict[str, Any],
    target: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    """Remove one draft line (committed item or open pending entry)."""
    kind = str(target.get("kind") or "item")
    if kind == "item":
        out = [dict(x) for x in items]
        try:
            idx = int(target.get("index"))
        except (TypeError, ValueError):
            return items, False
        if 0 <= idx < len(out):
            del out[idx]
            return out, True
        return items, False

    if kind == "pending":
        from chat.services.expense.pending_discard import remove_pending_entry_by_amount

        amt = float(target.get("amount") or 0)
        remove_pending_entry_by_amount(block, amt)
        return items, True

    if kind == "pending_queue":
        try:
            qi = int(target.get("index"))
        except (TypeError, ValueError):
            return items, False
        queue = list(block.get("pending_queue") or [])
        if 0 <= qi < len(queue):
            del queue[qi]
            block["pending_queue"] = queue
            return items, True
    return items, False
