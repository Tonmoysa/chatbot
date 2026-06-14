"""Numbered delete flow — context-aware pick (all → category → duplicate → delete)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from chat.services.expense.active_prompt import (
    KIND_DELETE_CONFIRM,
    KIND_DELETE_PICK,
    clear_active_prompt,
    read_active_prompt,
    set_active_prompt,
)
from chat.services.expense.draft_summary import format_numbered_draft_summary
from chat.services.expense.draft_view import DraftLine, ExpenseDraftView
from chat.services.expense_copy import normalize_reply_lang

_SCOPE_ALL = "all"
_SCOPE_CATEGORY = "category"
_SCOPE_DUPLICATE = "duplicate"

_ORDINAL_RE = re.compile(
    r"^(?:(?P<num>\d{1,2})|(?P<ord>first|second|third|fourth|fifth|"
    r"prothom|dwitiyo|tritiyo|1st|2nd|3rd))\s*(?:number|নম্বর|no|ta)?\s*$",
    re.I | re.UNICODE,
)
_ORD_MAP = {
    "first": 1,
    "1st": 1,
    "prothom": 1,
    "second": 2,
    "2nd": 2,
    "dwitiyo": 2,
    "third": 3,
    "3rd": 3,
    "tritiyo": 3,
    "fourth": 4,
    "fifth": 5,
}
_CAT_AMT_LOOSE_RE = re.compile(
    r"^(?P<cat>[a-zA-Z\u0980-\u09FF]+)\s+"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:tk|taka|টাকা)?\s*"
    r"(?:baad|bad|বাদ|মুছ|delete|remove|koro|kor|daw|dao)?\s*\.?$",
    re.I | re.UNICODE,
)


@dataclass(frozen=True)
class DeletePickResult:
    line: DraftLine | None = None
    prompt: str | None = None
    prompt_state: dict[str, Any] | None = None
    require_confirm: bool = False


def build_numbered_delete_prompt(
    view: ExpenseDraftView,
    *,
    lang: str | None = None,
    lines: list[DraftLine] | None = None,
    header: str | None = None,
    local_numbers: bool = False,
) -> str:
    reply_lang = normalize_reply_lang(lang)
    show = lines if lines is not None else view.lines
    if not show:
        if reply_lang == "en":
            return "No expense lines to delete."
        return "Delete korar moto kono line nei."

    body_lines: list[str] = []
    for i, ln in enumerate(show, start=1):
        label = ln.display_label(lang=reply_lang)
        if local_numbers:
            cat = ln.category.capitalize() if ln.category else "?"
            route = ""
            if ln.from_location and ln.to_location:
                route = f" ({ln.from_location} → {ln.to_location})"
            elif ln.pending_gap:
                route = f" ⚠ {ln.pending_gap}"
            body_lines.append(f"{i}. {cat} — {ln.amount:g} Tk{route}")
        else:
            body_lines.append(label)

    body = "\n".join(body_lines)
    if header:
        head = header
    elif reply_lang == "en":
        head = "Which entry should I delete?"
    else:
        head = "Kon entry delete korbo?"

    if reply_lang == "en":
        foot = "Reply with a **number** — e.g. `2` or `lunch 120 baad daw`."
    else:
        foot = "**নম্বর** বলুন — যেমন: `2` / `lunch 120 baad daw`।"

    return f"{head}\n\n{body}\n\n{foot}"


def start_numbered_delete(block: dict[str, Any]) -> dict[str, Any]:
    return set_active_prompt(
        block,
        KIND_DELETE_PICK,
        pick_scope=_SCOPE_ALL,
        numbered_mode="global",
        candidate_numbers=[],
    )


def message_answers_delete_pick(message: str, block: dict[str, Any] | None = None) -> bool:
    """True when message is a delete-pick reply (number or category+amount)."""
    text = (message or "").strip()
    if not text:
        return False
    from chat.services.expense.wizard_commands import (
        wants_cancel_expense_command,
        wants_expense_done_command_rules,
        wants_expense_submit_command,
    )

    if (
        wants_expense_submit_command(text)
        or wants_cancel_expense_command(text)
        or wants_expense_done_command_rules(text)
    ):
        return False
    if parse_delete_pick_number(message, block=block) is not None:
        return True
    if _parse_category_amount_loose(text):
        return True
    if re.search(
        r"\b(delete|remove|baad|bad|বাদ|মুছ)\b",
        text,
        re.I | re.UNICODE,
    ):
        return bool(re.search(r"\d", text) or re.search(r"[a-zA-Z\u0980-\u09FF]{3,}", text))
    if re.search(
        r"\b(?:baad|bad|বাদ|মুছ)\s*(?:daw|dao|koro|kor)\b",
        text,
        re.I | re.UNICODE,
    ):
        return True
    from chat.services.expense_extraction import parse_category_token

    cat = parse_category_token(text)
    if cat and len(text.split()) <= 3 and not re.search(
        r"\b(delete|remove|baad|bad|বাদ|মুছ|koro|kor|daw|dao)\b",
        text,
        re.I | re.UNICODE,
    ):
        return True
    return False


def parse_delete_pick_number(
    message: str,
    *,
    block: dict[str, Any] | None = None,
) -> int | None:
    t = (message or "").strip()
    if not t:
        return None
    local_num: int | None = None
    m = _ORDINAL_RE.match(t)
    if m:
        if m.group("num"):
            local_num = int(m.group("num"))
        else:
            local_num = _ORD_MAP.get((m.group("ord") or "").lower())
    else:
        m2 = re.match(r"^#?(\d{1,2})\b", t)
        if m2:
            rest = t[m2.end() :].strip()
            if not rest or re.match(
                r"^(?:number|নম্বর|no\.?|ta)\b",
                rest,
                re.I | re.UNICODE,
            ):
                local_num = int(m2.group(1))

    if local_num is None and re.search(
        r"\b(delete|remove|baad|bad|বাদ|মুছ)\b",
        t,
        re.I | re.UNICODE,
    ):
        m3 = re.search(r"#?(\d{1,2})\b", t)
        if m3:
            local_num = int(m3.group(1))

    if local_num is None:
        return None

    prompt = read_active_prompt(block)
    if (
        prompt
        and str(prompt.get("kind") or "") == KIND_DELETE_PICK
        and str(prompt.get("numbered_mode") or "") == "local"
    ):
        candidates = list(prompt.get("candidate_numbers") or [])
        if 1 <= local_num <= len(candidates):
            return int(candidates[local_num - 1])
        return None

    return local_num


def parse_multi_delete_pick_numbers(message: str) -> list[int]:
    """Two or more line numbers with delete intent — e.g. ``#4 #6 delete koro``."""
    text = (message or "").strip()
    if not text:
        return []
    if not re.search(
        r"\b(delete|remove|baad|bad|বাদ|মুছ)\b",
        text,
        re.I | re.UNICODE,
    ):
        return []
    nums = sorted({int(m) for m in re.findall(r"#?(\d{1,2})\b", text)}, reverse=True)
    return nums if len(nums) >= 2 else []


def apply_multi_delete_lines(
    view: ExpenseDraftView,
    numbers: list[int],
) -> tuple[list[dict[str, Any]], dict[str, Any], list[DraftLine]]:
    """Delete several numbered lines (highest number first to keep indices stable)."""
    items = list(view.items)
    block = dict(view.block)
    removed: list[DraftLine] = []
    for num in numbers:
        current = ExpenseDraftView(items, block)
        line = current.line_by_number(num)
        if not line:
            continue
        items, block, changed = apply_delete_line(current, line)
        if changed:
            removed.append(line)
    return items, block, removed


def _parse_category_amount_loose(message: str) -> tuple[str, float] | None:
    from chat.services.expense.delete_disambiguation_pending import (
        _parse_category_amount_delete,
    )
    from chat.services.expense_extraction import normalize_category, parse_category_token

    parsed = _parse_category_amount_delete(message)
    if parsed:
        return parsed

    t = (message or "").strip()
    m = _CAT_AMT_LOOSE_RE.match(t)
    if not m:
        return None
    cat = parse_category_token(m.group("cat"))
    if not cat:
        return None
    try:
        amt = float(str(m.group("amt")).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return normalize_category(cat), amt


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
    from chat.services.expense_extraction import parse_category_token

    cat = parse_category_token(text)
    if not cat:
        return None
    words = re.findall(r"[\w\u0980-\u09FF]+", text, re.UNICODE)
    if len(words) <= 2:
        return cat
    return None


def _lines_for_category(view: ExpenseDraftView, category: str) -> list[DraftLine]:
    cat_l = (category or "").strip().lower()
    return [ln for ln in view.lines if ln.category.lower() == cat_l]


def _lines_for_category_amount(
    view: ExpenseDraftView,
    category: str,
    amount: float,
) -> list[DraftLine]:
    hint = round(float(amount), 2)
    return [
        ln
        for ln in _lines_for_category(view, category)
        if abs(round(ln.amount, 2) - hint) < 0.01
    ]


def _line_from_global_number(view: ExpenseDraftView, num: int) -> DraftLine | None:
    return view.line_by_number(num)


def _prompt_state_for_lines(
    *,
    lines: list[DraftLine],
    pick_scope: str,
    filter_category: str = "",
    filter_amount: float | None = None,
    local_numbers: bool = False,
) -> dict[str, Any]:
    return {
        "kind": KIND_DELETE_PICK,
        "pick_scope": pick_scope,
        "numbered_mode": "local" if local_numbers else "global",
        "filter_category": filter_category,
        "filter_amount": filter_amount,
        "candidate_numbers": [ln.number for ln in lines],
    }


def _category_narrow_prompt(
    view: ExpenseDraftView,
    lines: list[DraftLine],
    *,
    category: str,
    lang: str | None,
    duplicate_amount: float | None = None,
) -> tuple[str, dict[str, Any]]:
    reply_lang = normalize_reply_lang(lang)
    cat_disp = category.capitalize() if category else "?"
    if duplicate_amount is not None and len(lines) >= 2:
        if reply_lang == "en":
            header = (
                f"**{len(lines)} {cat_disp} lines** at **{duplicate_amount:g} Tk** — "
                "which number should I delete?"
            )
        else:
            header = (
                f"**{cat_disp} {duplicate_amount:g} Tk** — **{len(lines)} টা** একই line আছে। "
                "কোন **নম্বর** delete korbo?"
            )
        scope = _SCOPE_DUPLICATE
    elif len(lines) >= 2:
        if reply_lang == "en":
            header = f"**{cat_disp}** — pick the **number** to delete:"
        else:
            header = f"**{cat_disp}** — কোন **নম্বর** delete korben?"
        scope = _SCOPE_CATEGORY
    else:
        header = None
        scope = _SCOPE_CATEGORY

    prompt = build_numbered_delete_prompt(
        view,
        lang=lang,
        lines=lines,
        header=header,
        local_numbers=True,
    )
    state = _prompt_state_for_lines(
        lines=lines,
        pick_scope=scope,
        filter_category=category,
        filter_amount=duplicate_amount,
        local_numbers=True,
    )
    return prompt, state


def resolve_delete_pick(
    message: str,
    view: ExpenseDraftView,
    *,
    block: dict[str, Any] | None = None,
    lang: str | None = None,
) -> DeletePickResult:
    """Resolve delete-pick reply using draft line numbers and active_prompt context."""
    text = (message or "").strip()
    reply_lang = normalize_reply_lang(lang)
    prompt = read_active_prompt(block)

    # 1) Bare number (respects local vs global numbering from active_prompt)
    num = parse_delete_pick_number(text, block=block)
    if num is not None:
        ln = _line_from_global_number(view, num)
        if ln:
            require_confirm = (
                str((prompt or {}).get("numbered_mode") or "global") == "global"
                and str((prompt or {}).get("pick_scope") or _SCOPE_ALL) == _SCOPE_ALL
            )
            return DeletePickResult(line=ln, require_confirm=require_confirm)
        return DeletePickResult(
            prompt=build_numbered_delete_prompt(view, lang=lang),
            prompt_state=_prompt_state_for_lines(
                lines=view.lines,
                pick_scope=_SCOPE_ALL,
                local_numbers=False,
            ),
        )

    # 2) Category + amount — e.g. lunch 200 / lunch 200 baad daw
    cat_amt = _parse_category_amount_loose(text)
    if cat_amt:
        cat, amt = cat_amt
        matches = _lines_for_category_amount(view, cat, amt)
        if len(matches) == 1:
            return DeletePickResult(line=matches[0], require_confirm=False)
        if len(matches) >= 2:
            q, state = _category_narrow_prompt(
                view,
                matches,
                category=cat,
                lang=lang,
                duplicate_amount=amt,
            )
            return DeletePickResult(prompt=q, prompt_state=state)

    # 3) Bare category — e.g. lunch
    bare_cat = _parse_bare_category(text)
    if bare_cat:
        matches = _lines_for_category(view, bare_cat)
        if len(matches) == 1:
            return DeletePickResult(line=matches[0], require_confirm=False)
        if len(matches) >= 2:
            q, state = _category_narrow_prompt(
                view,
                matches,
                category=bare_cat,
                lang=lang,
            )
            return DeletePickResult(prompt=q, prompt_state=state)

    # 4) Re-prompt from current scope
    if prompt and list(prompt.get("candidate_numbers") or []):
        nums = [int(n) for n in prompt["candidate_numbers"]]
        lines = [view.line_by_number(n) for n in nums]
        lines = [ln for ln in lines if ln]
        if lines:
            q = build_numbered_delete_prompt(
                view,
                lang=lang,
                lines=lines,
                local_numbers=str(prompt.get("numbered_mode")) == "local",
            )
            return DeletePickResult(prompt=q, prompt_state=dict(prompt))

    return DeletePickResult(
        prompt=build_numbered_delete_prompt(view, lang=lang),
        prompt_state=_prompt_state_for_lines(
            lines=view.lines,
            pick_scope=_SCOPE_ALL,
            local_numbers=False,
        ),
    )


def start_delete_confirm(block: dict[str, Any], line: DraftLine) -> dict[str, Any]:
    return set_active_prompt(
        block,
        KIND_DELETE_CONFIRM,
        line_id=line.line_id,
        number=line.number,
        category=line.category,
        amount=line.amount,
    )


def build_delete_confirm_prompt(line: DraftLine, *, lang: str | None = None) -> str:
    reply_lang = normalize_reply_lang(lang)
    cat = line.category.capitalize() if line.category else "?"
    if reply_lang == "en":
        return (
            f"Delete **#{line.number} {cat} — {line.amount:g} Tk**?\n\n"
            "• **yes** — remove\n"
            "• **no** — cancel"
        )
    return (
        f"**#{line.number} {cat} — {line.amount:g} Tk** মুছে ফেলব?\n\n"
        "• **yes** — মুছুন\n"
        "• **no** — বাতিল"
    )


def apply_delete_line(
    view: ExpenseDraftView,
    line: DraftLine,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    """Remove line from items or pending structures."""
    from chat.services.expense_extraction import is_travel_category
    from chat.services.expense.slots import STAGE_COLLECTING
    from chat.services.expense.expense_fsm import set_expense_stage

    block = view.block
    changed = False
    if line.kind == "committed":
        changed = view.remove_committed_by_line_id(line.line_id)
    elif line.kind == "pending":
        block.pop("pending_line", None)
        queue = list(block.get("pending_queue") or [])
        if queue:
            block["pending_line"] = dict(queue[0])
            block["pending_queue"] = [dict(x) for x in queue[1:]]
            promoted = block["pending_line"]
            cat = str(promoted.get("category") or "").strip().lower()
            if not cat:
                block["pending_step"] = "category"
            elif is_travel_category(cat) and (
                not str(promoted.get("from_location") or "").strip()
                or not str(promoted.get("to_location") or "").strip()
            ):
                block["pending_step"] = "from_to"
            else:
                block["pending_step"] = "category"
            set_expense_stage(block, STAGE_COLLECTING)
        else:
            block.pop("pending_step", None)
            block.pop("pending_queue", None)
        changed = True
    elif line.kind == "pending_queue":
        queue = list(block.get("pending_queue") or [])
        if 0 <= line.source_index < len(queue):
            del queue[line.source_index]
            block["pending_queue"] = queue
            changed = True
    view._lines = view._build_lines()
    items, block = view.apply_items_to_block()
    return items, block, changed


def format_after_delete_summary(
    items: list[dict[str, Any]],
    block: dict[str, Any],
    *,
    line: DraftLine,
    incurred_date_iso: str = "",
    lang: str | None = None,
) -> str:
    reply_lang = normalize_reply_lang(lang)
    cat = line.category.capitalize() if line.category else "?"
    if reply_lang == "en":
        prefix = f"Removed **#{line.number} {cat} — {line.amount:g} Tk**.\n\n"
    else:
        prefix = f"মুছে ফেলা হয়েছে — **#{line.number} {cat} — {line.amount:g} Tk**.\n\n"
    return prefix + format_numbered_draft_summary(
        items,
        block,
        incurred_date_iso=incurred_date_iso,
        lang=lang,
        header=None,
    )
