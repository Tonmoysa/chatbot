"""Add vs modify disambiguation when category/amount collides with existing lines."""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense.active_prompt import (
    KIND_ADD_MODIFY_CHOICE,
    clear_active_prompt,
    read_active_prompt,
    set_active_prompt,
)
from chat.services.expense.draft_summary import format_numbered_draft_summary
from chat.services.expense.draft_view import DraftLine, ExpenseDraftView
from chat.services.expense.expense_confirm import expense_line_fingerprint, looks_like_expense_correction
from chat.services.expense_copy import normalize_reply_lang

_EXPLICIT_ADD_RE = re.compile(
    r"\b(add|abar|notun|new|another|extra|plus|\+|যোগ|আরেক|আবার)\b",
    re.I | re.UNICODE,
)
_EXPLICIT_MODIFY_RE = re.compile(
    r"\b(modify|change|update|edit|badl|bodle|poriborto|hobe|hoy|habe|ঠিক|বদল)\b",
    re.I | re.UNICODE,
)
_CHOICE_ADD_RE = re.compile(
    r"^(?:add|notun|new|abar|যোগ|add\s*korbo|notun\s*line)\b",
    re.I | re.UNICODE,
)
_CHOICE_MODIFY_RE = re.compile(
    r"^(?:modify|change|update|edit|badl|modify\s*korbo)\b",
    re.I | re.UNICODE,
)


def user_explicitly_wants_add(message: str) -> bool:
    return bool(_EXPLICIT_ADD_RE.search(message or ""))


def user_explicitly_wants_modify(message: str) -> bool:
    return bool(_EXPLICIT_MODIFY_RE.search(message or ""))


def parse_add_modify_choice_reply(message: str, block: dict[str, Any] | None = None) -> str | None:
    """Returns 'add', 'modify', or None."""
    t = (message or "").strip()
    if not t:
        return None
    if _CHOICE_ADD_RE.search(t):
        return "add"
    if _CHOICE_MODIFY_RE.search(t):
        return "modify"
    prompt = read_active_prompt(block)
    m = re.match(r"^(\d{1,2})\s*(?:number|নম্বর|no)?\s*$", t, re.I)
    if m and prompt and str(prompt.get("kind") or "") == KIND_ADD_MODIFY_CHOICE:
        return "modify"
    return None


def find_category_collision(
    view: ExpenseDraftView,
    *,
    category: str,
    amount: float,
    from_location: str = "",
    to_location: str = "",
) -> list[DraftLine]:
    """Lines with same category when user did not say add/modify explicitly."""
    cat_l = (category or "").strip().lower()
    if not cat_l:
        return []
    matches = view.lines_by_category(cat_l)
    if not matches:
        return []
    fp = expense_line_fingerprint(
        {
            "category": category,
            "amount": amount,
            "from_location": from_location,
            "to_location": to_location,
        }
    )
    exact = [
        ln
        for ln in matches
        if expense_line_fingerprint(
            {
                "category": ln.category,
                "amount": ln.amount,
                "from_location": ln.from_location,
                "to_location": ln.to_location,
            }
        )
        == fp
    ]
    if exact and not user_explicitly_wants_add(message=""):
        return exact
    if len(matches) >= 1 and not user_explicitly_wants_add(message=""):
        return matches
    return []


def should_prompt_add_modify(
    message: str,
    view: ExpenseDraftView,
    *,
    category: str,
    amount: float,
    from_location: str = "",
    to_location: str = "",
) -> bool:
    """Prompt on exact duplicate OR when multiple same-category lines exist."""
    if looks_like_expense_correction(message):
        return False
    if user_explicitly_wants_add(message) or user_explicitly_wants_modify(message):
        return False
    committed = [
        ln for ln in view.lines_by_category(category) if ln.kind == "committed"
    ]
    if not committed:
        return False
    if len(committed) >= 2:
        return True
    fp = expense_line_fingerprint(
        {
            "category": category,
            "amount": amount,
            "from_location": from_location,
            "to_location": to_location,
        }
    )
    for ln in committed:
        if expense_line_fingerprint(
            {
                "category": ln.category,
                "amount": ln.amount,
                "from_location": ln.from_location,
                "to_location": ln.to_location,
            }
        ) == fp:
            return True
    return False


def build_add_modify_prompt(
    view: ExpenseDraftView,
    *,
    category: str,
    amount: float,
    lang: str | None = None,
) -> str:
    reply_lang = normalize_reply_lang(lang)
    matches = view.lines_by_category(category)
    cat_disp = category.capitalize() if category else "?"
    lines = "\n".join(ln.display_label(lang=reply_lang) for ln in matches)
    if reply_lang == "en":
        return (
            f"**{cat_disp} {amount:g} Tk** — already in draft.\n\n"
            f"{lines}\n\n"
            "What do you want?\n"
            "• **modify** — update an existing line\n"
            "• **add** — add a new line\n\n"
            "Example: `add korbo` or `modify korbo`"
        )
    return (
        f"**{cat_disp} {amount:g} Tk** — draft-এ আছে।\n\n"
        f"{lines}\n\n"
        "আপনি কী করতে চান?\n"
        "• **modify** — আগের line বদলাবেন\n"
        "• **add** — নতুন line যোগ করব\n\n"
        "উদাহরণ: `add korbo` / `modify korbo`"
    )


def start_add_modify_prompt(
    block: dict[str, Any],
    *,
    category: str,
    amount: float,
    from_location: str = "",
    to_location: str = "",
    line_ids: list[str] | None = None,
) -> dict[str, Any]:
    return set_active_prompt(
        block,
        KIND_ADD_MODIFY_CHOICE,
        category=category,
        amount=amount,
        from_location=from_location,
        to_location=to_location,
        candidate_line_ids=list(line_ids or []),
    )


def handle_add_modify_prompt_turn(
    *,
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    inc_iso: str,
    lang: str | None,
) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    """
    Returns (question, choice, items) where choice is 'add'|'modify'|None.
  If question set, caller should return it to user.
    """
    prompt = read_active_prompt(block)
    if not prompt or str(prompt.get("kind") or "") != KIND_ADD_MODIFY_CHOICE:
        return None, None, items

    choice = parse_add_modify_choice_reply(message, block)
    if not choice:
        view = ExpenseDraftView(items, block)
        cat = str(prompt.get("category") or "")
        amt = float(prompt.get("amount") or 0)
        return build_add_modify_prompt(view, category=cat, amount=amt, lang=lang), None, items

    clear_active_prompt(block)
    return None, choice, items
