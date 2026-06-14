"""
Confirm-before-delete for incomplete pending expense lines (no category / no route).
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense.expense_confirm import is_confirmation_no, is_confirmation_yes
from chat.services.expense_extraction import is_travel_category

KEY_PENDING_DISCARD_CONFIRM = "pending_discard_confirm"

_DISCARD_INCOMPLETE_RE = re.compile(
    r"(?:"
    r"vule\s+diyechi|ভুলে\s+দিয়েছি|by\s+mistake|mistake|"
    r"lagbe\s+nah|লাগবে\s+না|"
    r"ei\s+expense\s+baad|এই\s+expense\s+বাদ|"
    r"eta\s+baad|এটা\s+বাদ|"
    r"expense\s+ta\s+baad|expense\s+baad|"
    r"remove\s+it|delete\s+it|"
    r"baad\s+dite\s+chai|বাদ\s+দিতে\s+চাই"
    r")",
    re.I | re.UNICODE,
)

_DISCARD_AMOUNT_RE = re.compile(
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?\s*"
    r"(?:baad|bad|বাদ|remove|delete|drop)",
    re.I | re.UNICODE,
)


def pending_entry_is_incomplete(entry: dict[str, Any]) -> bool:
    cat = str(entry.get("category") or "").strip()
    if not cat:
        return True
    if is_travel_category(cat):
        frm = str(entry.get("from_location") or "").strip()
        to = str(entry.get("to_location") or "").strip()
        return not frm or not to
    return False


def wants_discard_incomplete_pending(message: str) -> bool:
    raw = (message or "").strip()
    if not raw:
        return False
    from chat.services.expense.command_parser import parse_correction_plan
    from chat.services.expense.expense_confirm import looks_like_expense_correction
    from chat.services.expense_extraction import parse_category_token

    if looks_like_expense_correction(raw):
        plan = parse_correction_plan(raw)
        if (
            plan.remove_by_amount
            or plan.remove_verb_first
            or plan.remove_loose
            or plan.remove_one
            or plan.remove_category_suffix
        ):
            return False
    if parse_category_token(raw) and _DISCARD_AMOUNT_RE.search(raw):
        return False
    if _DISCARD_INCOMPLETE_RE.search(raw):
        return True
    return bool(_DISCARD_AMOUNT_RE.search(raw))


def has_pending_discard_confirm(block: dict[str, Any]) -> bool:
    row = block.get(KEY_PENDING_DISCARD_CONFIRM)
    return isinstance(row, dict) and bool(row.get("amount"))


def clear_pending_discard_confirm(block: dict[str, Any]) -> None:
    block.pop(KEY_PENDING_DISCARD_CONFIRM, None)


def set_pending_discard_confirm(block: dict[str, Any], entry: dict[str, Any]) -> None:
    block[KEY_PENDING_DISCARD_CONFIRM] = {
        "amount": float(entry.get("amount") or 0),
        "category": str(entry.get("category") or "").strip(),
        "from_location": str(entry.get("from_location") or "").strip(),
        "to_location": str(entry.get("to_location") or "").strip(),
    }


def resolve_discard_target(
    block: dict[str, Any], message: str
) -> dict[str, Any] | None:
    from chat.services.expense_workflow import _pending_entries_list

    entries = _pending_entries_list(block)
    if not entries:
        return None

    raw = (message or "").strip()
    m = _DISCARD_AMOUNT_RE.search(raw)
    if m:
        try:
            target_amt = round(float(m.group("amt").replace(",", ".")), 2)
        except (TypeError, ValueError):
            target_amt = 0.0
        if target_amt > 0:
            for entry in entries:
                if round(float(entry.get("amount") or 0), 2) == target_amt:
                    return dict(entry)

    for entry in entries:
        if pending_entry_is_incomplete(entry):
            return dict(entry)
    return None


def _entry_label(entry: dict[str, Any], *, lang: str) -> str:
    amt = float(entry.get("amount") or 0)
    cat = str(entry.get("category") or "").strip()
    if not cat:
        if lang == "en":
            return f"**{amt:g} Tk** (category not set)"
        if lang == "banglish":
            return f"**{amt:g} Tk** (category dewa hoyni)"
        return f"**{amt:g} Tk** (category দেওয়া হয়নি)"
    if is_travel_category(cat):
        frm = str(entry.get("from_location") or "").strip()
        to = str(entry.get("to_location") or "").strip()
        if not frm or not to:
            if lang == "en":
                return f"**{cat} {amt:g} Tk** (route not set)"
            if lang == "banglish":
                return f"**{cat} {amt:g} Tk** (route dewa hoyni)"
            return f"**{cat} {amt:g} Tk** (route দেওয়া হয়নি)"
    return f"**{cat} {amt:g} Tk**"


def format_pending_discard_confirm_prompt(
    entry: dict[str, Any], *, lang: str | None = None
) -> str:
    from chat.services.expense_copy import normalize_reply_lang

    reply_lang = normalize_reply_lang(lang)
    label = _entry_label(entry, lang=reply_lang)
    if reply_lang == "en":
        return (
            f"Remove this incomplete expense line from your draft?\n"
            f"- {label}\n\n"
            "Reply **yes** to delete, or **no** to keep it."
        )
    if reply_lang == "banglish":
        return (
            f"Ei incomplete expense line draft theke **delete** korbo?\n"
            f"- {label}\n\n"
            "**yes** = delete · **no** = rakho"
        )
    return (
        f"এই incomplete expense line draft থেকে **মুছে** দিব?\n"
        f"- {label}\n\n"
        "**হ্যাঁ** = delete · **না** = রাখব"
    )


def format_pending_discard_cancelled(*, lang: str | None = None) -> str:
    from chat.services.expense_copy import normalize_reply_lang

    reply_lang = normalize_reply_lang(lang)
    if reply_lang == "en":
        return "OK — kept the line. Continue with category or add more expenses."
    if reply_lang == "banglish":
        return "Thik ache — line rakha holo. Category din ba aro expense add korun."
    return "ঠিক আছে — line রাখা হলো। Category দিন বা আরো expense add করুন।"


def format_pending_discard_done(
    entry: dict[str, Any], *, lang: str | None = None
) -> str:
    from chat.services.expense_copy import normalize_reply_lang

    reply_lang = normalize_reply_lang(lang)
    label = _entry_label(entry, lang=reply_lang)
    if reply_lang == "en":
        return f"Removed from draft: {label}."
    if reply_lang == "banglish":
        return f"Draft theke remove korechi: {label}."
    return f"Draft থেকে সরিয়ে দিয়েছি: {label}."


def remove_pending_entry_by_amount(
    block: dict[str, Any], amount: float
) -> list[dict[str, Any]]:
    from chat.services.expense_workflow import (
        _pending_entries_list,
        _store_pending_entries,
    )

    target = round(float(amount or 0), 2)
    remaining = [
        dict(e)
        for e in _pending_entries_list(block)
        if round(float(e.get("amount") or 0), 2) != target
    ]
    _store_pending_entries(block, remaining)
    return remaining


def _prompt_after_pending_change(
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    inc_iso: str,
    lang: str,
    day_logged_total: float = 0.0,
    daily_cap: float = 300.0,
    prefix: str = "",
) -> dict[str, Any]:
    from chat.services.expense_workflow import (
        STAGE_COLLECTING,
        _advance_pending_queue,
        _ask_category_prompt,
        _ask_from_to_prompt,
        _pack,
        _pending_entries_list,
        _try_advance_to_review,
        set_expense_stage,
    )

    remaining = _pending_entries_list(block)
    if remaining:
        first = remaining[0]
        cat = str(first.get("category") or "").strip()
        amt = float(first.get("amount") or 0)
        if not cat:
            block["pending_step"] = "category"
            set_expense_stage(block, STAGE_COLLECTING)
            q, facts = _ask_category_prompt(block, items, amt, lang=lang)
        elif is_travel_category(cat):
            frm = str(first.get("from_location") or "").strip()
            to = str(first.get("to_location") or "").strip()
            if not frm or not to:
                block["pending_step"] = "from_to"
                set_expense_stage(block, STAGE_COLLECTING)
                q, facts = _ask_from_to_prompt(block, items, cat, amt, lang=lang)
            else:
                items, q = _advance_pending_queue(block, items, inc_iso=inc_iso)
                facts = None
        else:
            items, q = _advance_pending_queue(block, items, inc_iso=inc_iso)
            facts = None
        if prefix:
            q = prefix.rstrip() + "\n\n" + q
        return _pack(
            wf,
            block,
            items=items,
            question=q,
            inc_iso=inc_iso,
            message_facts=facts,
        )

    block.pop("pending_step", None)
    set_expense_stage(block, STAGE_COLLECTING)
    adv = _try_advance_to_review(
        wf,
        block,
        items,
        inc_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
    )
    if adv:
        if prefix:
            adv["question"] = prefix.rstrip() + "\n\n" + str(adv.get("question") or "")
        return adv

    from chat.services.expense_workflow import _ask_more_lines_prompt

    q, facts = _ask_more_lines_prompt(block, items, lang=lang)
    if prefix:
        q = prefix.rstrip() + "\n\n" + q
    return _pack(
        wf,
        block,
        items=items,
        question=q,
        inc_iso=inc_iso,
        message_facts=facts,
    )


def try_handle_pending_discard_turn(
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    *,
    inc_iso: str,
    lang: str | None = None,
    day_logged_total: float = 0.0,
    daily_cap: float = 300.0,
) -> dict[str, Any] | None:
    """Handle discard confirm / cancel / apply for incomplete pending lines."""
    from chat.services.expense.active_prompt import (
        KIND_DELETE_CONFIRM,
        KIND_DELETE_PICK,
        active_prompt_kind,
    )
    from chat.services.expense.delete_flow import (
        parse_delete_pick_number,
        parse_multi_delete_pick_numbers,
    )
    from chat.services.expense.session_action_memory import record_pending_expense_discarded
    from chat.services.expense_workflow import _has_pending_expense_line

    if active_prompt_kind(block) in (KIND_DELETE_PICK, KIND_DELETE_CONFIRM):
        return None
    if active_prompt_kind(block) == KIND_DELETE_PICK:
        if parse_multi_delete_pick_numbers(message):
            return None
        if parse_delete_pick_number(message, block=block) is not None:
            return None

    reply_lang = lang or "bn"

    if has_pending_discard_confirm(block):
        confirm = dict(block.get(KEY_PENDING_DISCARD_CONFIRM) or {})
        if is_confirmation_yes(message):
            clear_pending_discard_confirm(block)
            amt = float(confirm.get("amount") or 0)
            remove_pending_entry_by_amount(block, amt)
            wf = record_pending_expense_discarded(
                wf,
                entry=confirm,
                items=items,
                incurred_date_iso=inc_iso,
                stage=str(block.get("stage") or "collecting"),
            )
            prefix = format_pending_discard_done(confirm, lang=reply_lang)
            return _prompt_after_pending_change(
                wf,
                block,
                items,
                inc_iso=inc_iso,
                lang=reply_lang,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                prefix=prefix,
            )
        if is_confirmation_no(message):
            clear_pending_discard_confirm(block)
            prefix = format_pending_discard_cancelled(lang=reply_lang)
            return _prompt_after_pending_change(
                wf,
                block,
                items,
                inc_iso=inc_iso,
                lang=reply_lang,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                prefix=prefix,
            )
        remind = format_pending_discard_confirm_prompt(confirm, lang=reply_lang)
        from chat.services.expense_workflow import _pack

        return _pack(
            wf,
            block,
            items=items,
            question=remind,
            inc_iso=inc_iso,
        )

    if not _has_pending_expense_line(block):
        return None
    if not wants_discard_incomplete_pending(message):
        return None

    target = resolve_discard_target(block, message)
    if not target or not pending_entry_is_incomplete(target):
        return None

    set_pending_discard_confirm(block, target)
    from chat.services.expense_workflow import _pack

    return _pack(
        wf,
        block,
        items=items,
        question=format_pending_discard_confirm_prompt(target, lang=reply_lang),
        inc_iso=inc_iso,
    )
