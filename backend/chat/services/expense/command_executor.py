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


def _set_amount_on_pending(
    block: dict[str, Any],
    cat: str,
    new_amt: float,
) -> bool:
    cat_l = cat.lower()
    changed = False
    pending = block.get("pending_line")
    if isinstance(pending, dict):
        if str(pending.get("category") or "").lower() == cat_l:
            pending["amount"] = new_amt
            changed = True
    queue = block.get("pending_queue")
    if isinstance(queue, list):
        for row in queue:
            if isinstance(row, dict) and str(row.get("category") or "").lower() == cat_l:
                row["amount"] = new_amt
                changed = True
    return changed


def _target_row_index(target: dict[str, Any], *, key: str = "index") -> int:
    """Row index; ``0`` is valid — never use ``or -1`` on index fields."""
    raw = target.get(key)
    if raw is None:
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _apply_bare_amount_to_target(
    items: list[dict[str, Any]],
    block: dict[str, Any] | None,
    target: dict[str, Any],
    new_amt: float,
) -> bool:
    kind = str(target.get("kind") or "")
    if kind == "item":
        idx = _target_row_index(target)
        if 0 <= idx < len(items):
            items[idx]["amount"] = new_amt
            return True
        return False
    if block is None:
        return False
    if kind == "pending":
        pending = block.get("pending_line")
        if isinstance(pending, dict) and pending.get("amount"):
            pending["amount"] = new_amt
            return True
        return False
    if kind == "pending_queue":
        qi = _target_row_index(target)
        queue = block.get("pending_queue") or []
        if 0 <= qi < len(queue) and isinstance(queue[qi], dict):
            queue[qi]["amount"] = new_amt
            return True
    return False


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


def _remove_pending_by_cat_amount(
    block: dict[str, Any],
    cat: str,
    amount: float,
) -> bool:
    """Remove one open pending line matching category + amount."""
    cat_l = cat.lower()
    target_amt = round(float(amount), 2)
    pending = block.get("pending_line")
    if isinstance(pending, dict) and pending.get("amount"):
        if (
            str(pending.get("category") or "").strip().lower() == cat_l
            and abs(round(float(pending.get("amount") or 0), 2) - target_amt) < 0.01
        ):
            from chat.services.expense.pending_discard import remove_pending_entry_by_amount

            remove_pending_entry_by_amount(block, target_amt)
            return True
    queue = list(block.get("pending_queue") or [])
    for qi, row in enumerate(queue):
        if not isinstance(row, dict):
            continue
        if (
            str(row.get("category") or "").strip().lower() == cat_l
            and abs(round(float(row.get("amount") or 0), 2) - target_amt) < 0.01
        ):
            del queue[qi]
            block["pending_queue"] = queue
            return True
    return False


def _replace_category_targets(
    items: list[dict[str, Any]],
    block: dict[str, Any] | None,
    from_cat: str,
    to_cat: str,
) -> bool:
    """Replace exactly one matching draft line (item or pending); skip if ambiguous."""
    from chat.services.expense.confusion_handler import list_amount_correction_targets

    from_l = from_cat.lower()
    targets = [
        t
        for t in list_amount_correction_targets(items, block)
        if str(t.get("category") or "").strip().lower() == from_l
    ]
    if len(targets) != 1:
        return False
    target = targets[0]
    kind = str(target.get("kind") or "")
    if kind == "item":
        idx = _target_row_index(target)
        if 0 <= idx < len(items):
            items[idx]["category"] = to_cat
            return True
        return False
    if block is None:
        return False
    if kind == "pending":
        pending = block.get("pending_line")
        if isinstance(pending, dict):
            pending["category"] = to_cat
            return True
        return False
    if kind == "pending_queue":
        qi = _target_row_index(target)
        queue = block.get("pending_queue") or []
        if 0 <= qi < len(queue) and isinstance(queue[qi], dict):
            queue[qi]["category"] = to_cat
            return True
    return False


def _set_category_route(
    out: list[dict[str, Any]], cat: str, frm: str, to: str
) -> bool:
    from chat.services.expense.normalization import normalize_location

    cat_l = cat.lower()
    frm_n = normalize_location(frm)
    to_n = normalize_location(to)
    for row in out:
        if str(row.get("category") or "").lower() == cat_l:
            row["from_location"] = frm_n
            row["to_location"] = to_n
            return True
    return False


def _set_category_route_at_index(
    out: list[dict[str, Any]], index: int, frm: str, to: str
) -> bool:
    from chat.services.expense.normalization import normalize_location

    if not (0 <= index < len(out)):
        return False
    frm_n = normalize_location(frm)
    to_n = normalize_location(to)
    out[index]["from_location"] = frm_n
    out[index]["to_location"] = to_n
    return True


def execute_correction_plan(
    items: list[dict[str, Any]],
    plan: CorrectionCommandPlan,
    block: dict[str, Any] | None = None,
) -> CommandExecuteResult:
    """Apply a correction plan — mirrors legacy apply_corrections ordering."""
    changed = False
    out = [dict(x) for x in items]

    for idx, frm, to in plan.set_routes_by_index:
        if _set_category_route_at_index(out, idx, frm, to):
            changed = True

    for cat, frm, to in plan.set_routes:
        if _set_category_route(out, cat, frm, to):
            changed = True

    for from_cat, to_cat in plan.replacements:
        if _replace_category_targets(out, block, from_cat, to_cat):
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
        matches = [
            i
            for i, row in enumerate(out)
            if str(row.get("category") or "").lower() == cat.lower()
        ]
        if len(matches) != 1:
            continue
        del out[matches[0]]
        changed = True

    for cat, rm_amt in plan.remove_by_amount:
        target_amt = round(float(rm_amt), 2)
        from chat.services.expense.confusion_handler import list_amount_correction_targets

        matches = [
            t
            for t in list_amount_correction_targets(out, block)
            if str(t.get("category") or "").strip().lower() == cat.lower()
            and abs(round(float(t.get("amount") or 0), 2) - target_amt) < 0.01
        ]
        if len(matches) != 1:
            continue
        target = matches[0]
        kind = str(target.get("kind") or "")
        if kind == "item":
            idx = _target_row_index(target)
            if 0 <= idx < len(out):
                del out[idx]
                changed = True
        elif block is not None and _remove_pending_by_cat_amount(block, cat, target_amt):
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

    if plan.update_amount_by_index is not None:
        idx, new_amt = plan.update_amount_by_index
        if 0 <= idx < len(out):
            out[idx]["amount"] = new_amt
            changed = True

    if plan.remove_by_index is not None and 0 <= plan.remove_by_index < len(out):
        del out[plan.remove_by_index]
        changed = True

    for cat, new_amt in plan.update_amounts:
        matches = [
            r
            for r in out
            if str(r.get("category") or "").lower() == cat.lower()
        ]
        if len(matches) > 1:
            continue
        if _set_category_amount(out, cat, new_amt):
            changed = True

    for cat, new_amt in plan.set_amounts:
        matches = [
            r
            for r in out
            if str(r.get("category") or "").lower() == cat.lower()
        ]
        if len(matches) > 1:
            continue
        if _set_category_amount(out, cat, new_amt):
            changed = True
        elif block is not None and _set_amount_on_pending(block, cat, new_amt):
            changed = True

    if plan.bare_amount_set is not None:
        from chat.services.expense.confusion_handler import list_amount_correction_targets

        targets = list_amount_correction_targets(out, block)
        if len(targets) == 1:
            if _apply_bare_amount_to_target(
                out, block, targets[0], plan.bare_amount_set
            ):
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
        if not applied:
            from chat.services.expense.confusion_handler import list_amount_correction_targets

            targets = list_amount_correction_targets(out, block)
            if len(targets) == 1 and old_amt <= 0:
                if _apply_bare_amount_to_target(out, block, targets[0], new_amt):
                    changed = True
                    applied = True
        if not applied and len(out) == 1:
            out[0]["amount"] = new_amt
            changed = True

    for cat, new_amt in plan.cat_er_amounts:
        matches = [
            r
            for r in out
            if str(r.get("category") or "").lower() == cat.lower()
        ]
        if len(matches) > 1:
            continue
        if _set_category_amount(out, cat, new_amt):
            changed = True

    for cat, new_amt in plan.add_amounts:
        matches = [
            (i, row)
            for i, row in enumerate(out)
            if str(row.get("category") or "").lower() == cat.lower()
        ]
        if len(matches) > 1:
            continue
        if len(matches) == 1:
            matches[0][1]["amount"] = float(matches[0][1].get("amount") or 0) + new_amt
            changed = True
        else:
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
    if changed and block is not None:
        from chat.services.expense.expense_draft_gate import finalize_expense_draft

        out, finalized_block = finalize_expense_draft(block, out)
        block.clear()
        block.update(finalized_block)
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

    rules_plan = parse_correction_plan(message, item_count=len(items))
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
