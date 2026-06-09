"""Execute typed expense command plans (Phase 2)."""

from __future__ import annotations

from typing import Any, Callable

from chat.services.expense.command_schema import CommandExecuteResult, CorrectionCommandPlan
from chat.services.expense.expense_confirm import (
    ExpenseLineItem,
    _adjust_category_amount,
    _prune_zero_lines,
    _remove_travel_lines,
    _replace_category,
    _set_category_amount,
    dedupe_expense_items,
)


def draft_category_names(
    items: list[dict[str, Any]],
    block: dict[str, Any] | None = None,
) -> set[str]:
    """Categories in committed lines plus open pending queue entries."""
    cats = {
        str(row.get("category") or "").lower()
        for row in items
        if str(row.get("category") or "").strip()
    }
    if not block:
        return cats
    pending = block.get("pending_line")
    if isinstance(pending, dict):
        cat = str(pending.get("category") or "").strip()
        if cat:
            cats.add(cat.lower())
    for row in block.get("pending_queue") or []:
        if isinstance(row, dict):
            cat = str(row.get("category") or "").strip()
            if cat:
                cats.add(cat.lower())
    return cats


def _replace_category_in_pending(
    block: dict[str, Any],
    from_cat: str,
    to_cat: str,
) -> bool:
    from_l = from_cat.lower()
    to_l = to_cat.lower()
    if from_l == to_l:
        return False
    changed = False
    pending = block.get("pending_line")
    if isinstance(pending, dict):
        if str(pending.get("category") or "").lower() == from_l:
            pending["category"] = to_cat
            changed = True
    queue = block.get("pending_queue")
    if isinstance(queue, list):
        for row in queue:
            if isinstance(row, dict) and str(row.get("category") or "").lower() == from_l:
                row["category"] = to_cat
                changed = True
    return changed


def execute_correction_plan(
    items: list[dict[str, Any]],
    plan: CorrectionCommandPlan,
    block: dict[str, Any] | None = None,
) -> CommandExecuteResult:
    """Apply a correction plan — mirrors legacy apply_corrections ordering."""
    changed = False
    out = [dict(x) for x in items]

    for from_cat, to_cat in plan.replacements:
        if _replace_category(out, from_cat, to_cat):
            changed = True
        if block is not None and _replace_category_in_pending(block, from_cat, to_cat):
            changed = True

    if plan.remove_travel_group:
        out, removed = _remove_travel_lines(out)
        if removed > 0:
            changed = True

    for from_cat, to_cat, amt in plan.transfers:
        if from_cat.lower() == to_cat.lower():
            continue
        if _adjust_category_amount(out, from_cat, -amt):
            if not _adjust_category_amount(out, to_cat, amt):
                out.append(ExpenseLineItem(category=to_cat, amount=amt).to_dict())
            changed = True

    if not changed and not plan.has_transfer_pattern:
        for cat, amt in plan.partial_deducts:
            if _adjust_category_amount(out, cat, -amt):
                changed = True

    for cat in plan.remove_one:
        for i, row in enumerate(out):
            if str(row.get("category") or "").lower() == cat.lower():
                del out[i]
                changed = True
                break

    for cat in plan.remove_loose:
        for i, row in enumerate(out):
            if str(row.get("category") or "").lower() == cat.lower():
                del out[i]
                changed = True
                break

    for cat, rm_amt in plan.remove_by_amount:
        target_amt = round(float(rm_amt), 2)
        before = len(out)
        out = [
            r
            for r in out
            if not (
                str(r.get("category") or "").lower() == cat.lower()
                and round(float(r.get("amount") or 0), 2) == target_amt
            )
        ]
        if len(out) < before:
            changed = True

    for cat in plan.remove_verb_first:
        if any(c.lower() == cat.lower() for c, _ in plan.remove_by_amount):
            continue
        matches = [
            r
            for r in out
            if str(r.get("category") or "").lower() == cat.lower()
        ]
        if len(matches) > 1:
            continue
        before = len(out)
        out = [r for r in out if str(r.get("category") or "").lower() != cat.lower()]
        if len(out) < before:
            changed = True

    for cat in plan.remove_category_suffix:
        before = len(out)
        out = [r for r in out if str(r.get("category") or "").lower() != cat.lower()]
        if len(out) < before:
            changed = True

    for cat, new_amt in plan.update_amounts:
        if _set_category_amount(out, cat, new_amt):
            changed = True

    for cat, new_amt in plan.set_amounts:
        if _set_category_amount(out, cat, new_amt):
            changed = True

    for new_amt, old_amt in plan.amount_replacements:
        applied = False
        if old_amt > 0:
            for row in out:
                if round(float(row.get("amount") or 0), 2) == round(old_amt, 2):
                    row["amount"] = new_amt
                    changed = True
                    applied = True
                    break
        if not applied and len(out) == 1:
            out[0]["amount"] = new_amt
            changed = True

    for cat, new_amt in plan.cat_er_amounts:
        if _set_category_amount(out, cat, new_amt):
            changed = True

    for cat, new_amt in plan.add_amounts:
        found = False
        for row in out:
            if str(row.get("category") or "").lower() == cat.lower():
                row["amount"] = float(row.get("amount") or 0) + new_amt
                found = True
                changed = True
                break
        if not found:
            out.append(ExpenseLineItem(category=cat, amount=new_amt).to_dict())
            changed = True

    if plan.set_category_only:
        from chat.services.expense.reconcile import apply_category_hobe_correction

        pending = None
        if block and isinstance(block.get("pending_line"), dict):
            pending = block.get("pending_line")
        out, slot_changed = apply_category_hobe_correction(
            out, plan.set_category_only, pending=pending
        )
        if slot_changed:
            changed = True

    if changed:
        out = _prune_zero_lines(dedupe_expense_items(out))
    return CommandExecuteResult(items=out, changed=changed)


def apply_message_corrections(
    items: list[dict[str, Any]],
    message: str,
    *,
    extract_lines: Callable[[str], Any] | None = None,
    trace_id: str = "",
    use_llm: bool = True,
    review_stage: bool = False,
    block: dict[str, Any] | None = None,
    last_question: str = "",
    stage: str = "",
    pending_step: str = "",
    pending_line: dict[str, Any] | None = None,
) -> CommandExecuteResult:
    """Parse + execute corrections; rules first, LLM gap-fill, then extract."""
    from chat.services.expense.command_llm_gate import correction_llm_should_use
    from chat.services.expense.command_llm_parser import parse_correction_plan_llm
    from chat.services.expense.command_parser import (
        parse_correction_plan,
        resolve_correction_plan,
    )

    review = review_stage or extract_lines is None
    collecting = bool(block and not review and items)
    parse_result = resolve_correction_plan(
        message,
        items,
        trace_id=trace_id,
        use_llm=use_llm,
        review_stage=review,
        collecting_stage=collecting,
        stage=stage,
        pending_step=pending_step,
        pending_line=pending_line,
        block=block,
        last_question=last_question,
    )
    result = execute_correction_plan(items, parse_result.plan, block=block)
    if result.changed:
        return CommandExecuteResult(
            items=result.items,
            changed=True,
            parse_source=parse_result.source,
        )

    rules_plan = parse_correction_plan(message)
    llm_kwargs = dict(
        stage=stage,
        pending_step=pending_step,
        pending_line=pending_line,
        block=block,
        last_question=last_question,
    )
    if use_llm and rules_plan.has_any_correction():
        if review and correction_llm_should_use(message, items, review_stage=True):
            llm_plan = parse_correction_plan_llm(message, items, trace_id, **llm_kwargs)
            if llm_plan and llm_plan.has_any_correction():
                llm_result = execute_correction_plan(items, llm_plan, block=block)
                if llm_result.changed:
                    return CommandExecuteResult(
                        items=llm_result.items,
                        changed=True,
                        parse_source="llm",
                    )
        elif collecting and correction_llm_should_use(
            message, items, collecting_stage=True
        ):
            llm_plan = parse_correction_plan_llm(message, items, trace_id, **llm_kwargs)
            if llm_plan and llm_plan.has_any_correction():
                llm_result = execute_correction_plan(items, llm_plan, block=block)
                if llm_result.changed:
                    return CommandExecuteResult(
                        items=llm_result.items,
                        changed=True,
                        parse_source="llm",
                    )

    if extract_lines is None:
        return CommandExecuteResult(
            items=result.items,
            changed=False,
            parse_source=parse_result.source,
        )

    ext = extract_lines(message)
    out = [dict(x) for x in result.items]
    changed = False
    for ni in ext.items:
        cat = ni.category
        if _set_category_amount(out, cat, float(ni.amount)):
            row = next(
                r for r in out if str(r.get("category") or "").lower() == cat.lower()
            )
            if ni.from_location:
                row["from_location"] = ni.from_location
            if ni.to_location:
                row["to_location"] = ni.to_location
            changed = True
        else:
            out.append(ni.to_dict())
            changed = True
    if changed:
        out = _prune_zero_lines(dedupe_expense_items(out))
        return CommandExecuteResult(
            items=out,
            changed=True,
            parse_source=parse_result.source,
        )
    return CommandExecuteResult(
        items=result.items,
        changed=False,
        parse_source=parse_result.source,
    )
