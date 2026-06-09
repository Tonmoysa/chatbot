"""Parse user messages into typed expense command plans (Phase 2)."""

from __future__ import annotations

from chat.services.expense.command_schema import (
    CorrectionCommandPlan,
    CorrectionParseResult,
    WizardFlowCommandPlan,
)
from chat.services.expense.expense_confirm import (
    _ADD_RE,
    _AMOUNT_INSTEAD_RE,
    _CAT_AMT_BAAD_RE,
    _CAT_ER_EXPENSE_AMOUNT_RE,
    _CAT_HOBE_RE,
    _CONTEXTUAL_CAT_AMOUNT_HOBE_RE,
    _PARTIAL_DEDUCT_RE,
    _REMOVE_LOOSE_RE,
    _REMOVE_ONE_RE,
    _REMOVE_RE,
    _REMOVE_TRAVEL_GROUP_ALT_RE,
    _REMOVE_TRAVEL_GROUP_RE,
    _REMOVE_VERB_CAT_AMT_RE,
    _REMOVE_VERB_CAT_RE,
    _REPLACE_KORE_DAW_RE,
    _REPLACE_RE,
    _REPLACE_TA_CAT_RE,
    _REPLACE_KE_KORO_RE,
    _REPLACE_SETAKE_RE,
    _SET_AMOUNT_RE,
    _TRANSFER_RE,
    _UPDATE_AMOUNT_RE,
    _normalize_correction_message,
    wants_travel_group_remove,
)
from chat.services.expense_extraction import normalize_category
from chat.services.expense.wizard_commands import (
    wants_expense_done_command,
    wants_expense_submit_command,
)


def _normalize_remove_cat(raw: str) -> str:
    if raw in ("rtain", "rtrain", "tran", "trin"):
        return normalize_category("train")
    return normalize_category(raw)


def parse_correction_plan(message: str) -> CorrectionCommandPlan:
    """Build a correction plan from regex matches (same coverage as legacy apply_corrections)."""
    plan = CorrectionCommandPlan()
    low = _normalize_correction_message(message)
    if not low.strip():
        return plan

    for m in _REPLACE_RE.finditer(low):
        plan.replacements.append(
            (normalize_category(m.group("from_cat")), normalize_category(m.group("to_cat")))
        )

    for m in _REPLACE_KORE_DAW_RE.finditer(low):
        pair = (
            normalize_category(m.group("from_cat")),
            normalize_category(m.group("to_cat")),
        )
        if pair not in plan.replacements:
            plan.replacements.append(pair)

    for m in _REPLACE_TA_CAT_RE.finditer(low):
        pair = (
            normalize_category(m.group("from_cat")),
            normalize_category(m.group("to_cat")),
        )
        if pair not in plan.replacements:
            plan.replacements.append(pair)

    for m in _REPLACE_KE_KORO_RE.finditer(low):
        pair = (
            normalize_category(m.group("from_cat")),
            normalize_category(m.group("to_cat")),
        )
        if pair not in plan.replacements:
            plan.replacements.append(pair)

    for m in _REPLACE_SETAKE_RE.finditer(low):
        pair = (
            normalize_category(m.group("from_cat")),
            normalize_category(m.group("to_cat")),
        )
        if pair not in plan.replacements:
            plan.replacements.append(pair)

    if wants_travel_group_remove(low):
        plan.remove_travel_group = True

    for m in _TRANSFER_RE.finditer(low):
        try:
            amt = float(m.group("amt").replace(",", "."))
        except (TypeError, ValueError):
            continue
        plan.transfers.append(
            (
                normalize_category(m.group("from_cat")),
                normalize_category(m.group("to_cat")),
                amt,
            )
        )
    plan.has_transfer_pattern = bool(_TRANSFER_RE.search(low))

    for m in _PARTIAL_DEDUCT_RE.finditer(low):
        try:
            amt = float(m.group("amt").replace(",", "."))
        except (TypeError, ValueError):
            continue
        plan.partial_deducts.append((normalize_category(m.group("cat")), amt))
    plan.has_partial_deduct_pattern = bool(_PARTIAL_DEDUCT_RE.search(low))

    for m in _REMOVE_ONE_RE.finditer(low):
        plan.remove_one.append(normalize_category(m.group("cat")))
    plan.has_remove_one_pattern = bool(_REMOVE_ONE_RE.search(low))

    for m in _REMOVE_LOOSE_RE.finditer(low):
        if (
            plan.has_remove_one_pattern
            or plan.has_transfer_pattern
            or plan.has_partial_deduct_pattern
        ):
            continue
        plan.remove_loose.append(normalize_category(m.group("cat")))

    for m in _REMOVE_VERB_CAT_AMT_RE.finditer(low):
        try:
            amt = float(m.group("amt").replace(",", "."))
        except (TypeError, ValueError):
            continue
        plan.remove_by_amount.append((_normalize_remove_cat(m.group("cat")), amt))

    for m in _CAT_AMT_BAAD_RE.finditer(low):
        try:
            amt = float(m.group("amt").replace(",", "."))
        except (TypeError, ValueError):
            continue
        pair = (_normalize_remove_cat(m.group("cat")), amt)
        if pair not in plan.remove_by_amount:
            plan.remove_by_amount.append(pair)

    for m in _REMOVE_VERB_CAT_RE.finditer(low):
        if _REMOVE_VERB_CAT_AMT_RE.search(m.group(0) or ""):
            continue
        plan.remove_verb_first.append(_normalize_remove_cat(m.group("cat")))

    for m in _REMOVE_RE.finditer(low):
        plan.remove_category_suffix.append(normalize_category(m.group("cat")))

    for m in _UPDATE_AMOUNT_RE.finditer(low):
        try:
            amt = float(m.group("amt").replace(",", "."))
        except ValueError:
            continue
        plan.update_amounts.append((normalize_category(m.group("cat")), amt))
    plan.has_update_amount_pattern = bool(_UPDATE_AMOUNT_RE.search(low))

    has_remove = bool(
        plan.remove_by_amount
        or plan.remove_verb_first
        or plan.remove_one
        or plan.remove_loose
        or plan.remove_category_suffix
        or plan.remove_travel_group
    )

    for m in _SET_AMOUNT_RE.finditer(low):
        if plan.has_update_amount_pattern or has_remove:
            continue
        try:
            amt = float(m.group("amt").replace(",", "."))
        except ValueError:
            continue
        plan.set_amounts.append((normalize_category(m.group("cat")), amt))

    for m in _CONTEXTUAL_CAT_AMOUNT_HOBE_RE.finditer(low):
        if plan.has_update_amount_pattern or has_remove:
            continue
        try:
            amt = float(m.group("amt").replace(",", "."))
        except ValueError:
            continue
        pair = (normalize_category(m.group("cat")), amt)
        if pair not in plan.set_amounts:
            plan.set_amounts.append(pair)

    for m in _CAT_ER_EXPENSE_AMOUNT_RE.finditer(low):
        try:
            amt = float(m.group("amt").replace(",", "."))
        except ValueError:
            continue
        plan.cat_er_amounts.append((normalize_category(m.group("cat")), amt))

    for m in _ADD_RE.finditer(low):
        cat_g = m.group("cat") or m.group("cat2")
        amt_g = m.group("amt") or m.group("amt2")
        if not cat_g or not amt_g:
            continue
        try:
            amt = float(amt_g.replace(",", "."))
        except ValueError:
            continue
        plan.add_amounts.append((normalize_category(cat_g), amt))

    m_hobe = _CAT_HOBE_RE.search(low)
    if m_hobe and not plan.has_any_correction():
        plan.set_category_only = _normalize_remove_cat(m_hobe.group("cat"))

    for m in _AMOUNT_INSTEAD_RE.finditer(low):
        raw_new = m.group("new_amt") or m.group("new_amt2")
        raw_old = m.group("old_amt") or m.group("old_amt2")
        if not raw_new:
            continue
        try:
            new_amt = float(raw_new.replace(",", "."))
            old_amt = float(raw_old.replace(",", ".")) if raw_old else 0.0
        except (TypeError, ValueError):
            continue
        pair = (new_amt, old_amt)
        if pair not in plan.amount_replacements:
            plan.amount_replacements.append(pair)

    return plan


def resolve_correction_plan(
    message: str,
    items: list[dict] | None = None,
    *,
    trace_id: str = "",
    use_llm: bool = False,
    review_stage: bool = False,
    collecting_stage: bool = False,
    stage: str = "",
    pending_step: str = "",
    pending_line: dict[str, Any] | None = None,
    block: dict[str, Any] | None = None,
    last_question: str = "",
) -> CorrectionParseResult:
    """
    Rules-first correction parse; optional LLM gap-fill at review/collecting edit.
    """
    from chat.services.expense.command_llm_gate import correction_llm_should_use
    from chat.services.expense.command_llm_parser import parse_correction_plan_llm

    rules_plan = parse_correction_plan(message)
    if rules_plan.has_any_correction():
        return CorrectionParseResult(plan=rules_plan, source="rules")

    llm_kwargs = dict(
        stage=stage,
        pending_step=pending_step,
        pending_line=pending_line,
        block=block,
        last_question=last_question,
    )
    if (
        use_llm
        and review_stage
        and correction_llm_should_use(message, items, review_stage=True)
    ):
        llm_plan = parse_correction_plan_llm(
            message, items or [], trace_id, **llm_kwargs
        )
        if llm_plan and llm_plan.has_any_correction():
            return CorrectionParseResult(plan=llm_plan, source="llm")

    if (
        use_llm
        and collecting_stage
        and correction_llm_should_use(message, items, collecting_stage=True)
    ):
        llm_plan = parse_correction_plan_llm(
            message, items or [], trace_id, **llm_kwargs
        )
        if llm_plan and llm_plan.has_any_correction():
            return CorrectionParseResult(plan=llm_plan, source="llm")

    return CorrectionParseResult(plan=rules_plan, source="rules")


def parse_wizard_flow_plan(
    message: str,
    *,
    trace_id: str = "",
) -> WizardFlowCommandPlan:
    """Parse finish/submit navigation intents during collecting."""
    return WizardFlowCommandPlan(
        finish_collecting=wants_expense_done_command(
            message, trace_id=trace_id, use_llm=True
        ),
        submit_draft=wants_expense_submit_command(message),
    )
