"""Apply add/modify choice residuals after active_prompt clears."""

from __future__ import annotations

from typing import Any

from chat.services.expense.draft_summary import format_numbered_draft_summary
from chat.services.expense.draft_view import ExpenseDraftView
from chat.services.expense.modify_flow import (
    apply_amount_to_line_id,
    build_modify_target_prompt,
    start_modify_target_prompt,
)
from chat.services.expense.turn_schema import TurnRouteResult
from chat.services.expense_validation import validate_expense_items


def route_add_modify_residual(
    *,
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    inc_iso: str,
    day_logged_total: float,
    daily_cap: float,
    lang: str | None,
) -> TurnRouteResult | None:
    """Handle _add_modify_force_modify set by add/modify choice handler."""
    from chat.services.expense_workflow import _pack

    force_modify = block.pop("_add_modify_force_modify", None)
    if not isinstance(force_modify, dict) or not force_modify.get("category"):
        return None

    cat = str(force_modify.get("category") or "")
    new_amt = float(force_modify.get("amount") or 0)
    view = ExpenseDraftView(items, block)
    committed = [
        ln for ln in view.lines_by_category(cat) if ln.kind == "committed"
    ]
    if len(committed) == 1:
        items = apply_amount_to_line_id(items, committed[0].line_id, new_amt)
        block["items"] = items
        val = validate_expense_items(
            items,
            incurred_date_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            message=message,
        )
        ln = committed[0]
        if lang == "en":
            prefix = f"Updated **#{ln.number} {cat}** → **{new_amt:g} Tk**.\n\n"
        else:
            prefix = f"আপডেট — **#{ln.number} {cat}** → **{new_amt:g} Tk**.\n\n"
        q = prefix + format_numbered_draft_summary(
            items, block, incurred_date_iso=inc_iso, lang=lang
        )
        return TurnRouteResult(
            handled=True,
            pack=_pack(
                wf,
                block,
                items=items,
                question=q,
                warnings=val.warnings,
                inc_iso=inc_iso,
            ),
        )

    if len(committed) > 1:
        start_modify_target_prompt(
            block,
            category=cat,
            amount=new_amt,
            candidate_numbers=[ln.number for ln in committed],
        )
        q = build_modify_target_prompt(committed, category=cat, amount=new_amt, lang=lang)
        return TurnRouteResult(
            handled=True,
            pack=_pack(wf, block, items=items, question=q, inc_iso=inc_iso),
        )

    return None
