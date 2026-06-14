"""
Route draft-aware turn decisions inside the expense wizard (Phases A–D).

Single entry: rules/LLM turn parse → deterministic handler.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense.command_executor import execute_correction_plan
from chat.services.expense.expense_confirm import (
    build_correction_failure_notice,
    correction_unclear_notice,
    dedupe_expense_items,
    duplicate_reentry_notice,
    looks_like_duplicate_expense_reentry,
    review_denial_hints,
    wants_travel_group_remove,
)
from chat.services.expense.expense_ingest_guard import clear_ingest_lock
from chat.services.expense.expense_draft_snapshots import push_expense_snapshot
from chat.services.expense.turn_parser import resolve_expense_turn
from chat.services.expense.turn_schema import (
    TURN_ADD_LINES,
    TURN_CLARIFY_REPLY,
    TURN_CONFIRM,
    TURN_DENY,
    TURN_EDIT_DRAFT,
    TURN_FILL_SLOT,
    TURN_NAVIGATE,
    TURN_PRAISE,
    TURN_UNCLEAR,
    TurnDecision,
    TurnRouteResult,
)
from chat.services.expense.slots import STAGE_COLLECTING, STAGE_REVIEW, STAGE_SUBMIT_CONFIRM
from chat.services.expense_validation import validate_expense_items


def _format_summary(
    items: list[dict[str, Any]],
    block: dict[str, Any],
    **kwargs: Any,
) -> str:
    from chat.services.expense_workflow import format_expense_summary

    return format_expense_summary(items, block=block, **kwargs)


def _prompt_message_facts(
    question: str,
    *,
    items: list[dict[str, Any]],
    lang: str | None,
    prompt_kind: str,
    message_type: str = "expense_disambiguation",
    target_index: int | None = None,
    target_amount: float | None = None,
) -> dict[str, Any] | None:
    from chat.services.expense_copy import normalize_reply_lang
    from chat.services.expense_message_facts import (
        message_meta_for_disambiguation_or_confirm,
    )

    return message_meta_for_disambiguation_or_confirm(
        question,
        items=items,
        lang=normalize_reply_lang(lang),
        prompt_kind=prompt_kind,
        message_type=message_type,  # type: ignore[arg-type]
        target_index=target_index,
        target_amount=target_amount,
    )


def _handle_amount_correction_pending_turn(
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
    from chat.services.expense.amount_correction_pending import (
        apply_amount_to_target,
        clear_amount_correction_pending,
        has_amount_correction_pending,
        read_amount_correction_pending,
        resolve_amount_correction_reply,
    )
    from chat.services.expense.expense_confirm import is_confirmation_no
    from chat.services.expense.expense_fsm import set_expense_stage
    from chat.services.expense_workflow import _pack, format_expense_summary
    from chat.services.expense.command_executor import dedupe_expense_items
    from chat.services.expense_validation import validate_expense_items

    if not has_amount_correction_pending(block):
        return None
    pending = read_amount_correction_pending(block)
    if not pending:
        return None

    from chat.services.expense.interactive_pending import (
        clear_expense_interactive_pending,
        message_abandons_expense_interactive_pending,
    )

    if message_abandons_expense_interactive_pending(message, block):
        clear_expense_interactive_pending(block)
        return None

    if is_confirmation_no(message):
        block = clear_amount_correction_pending(block)
        q = "Amount update **cancelled** — draft unchanged.\n\n"
        q += _format_summary(
            items,
            block,
            incurred_date_iso=inc_iso,
            warnings=[],
            line_flags=[],
            lang=lang,
        )
        return TurnRouteResult(
            handled=True,
            pack=_pack(wf, block, items=items, question=q, inc_iso=inc_iso),
        )

    target, prompt, updated_pending = resolve_amount_correction_reply(
        message, items, block, pending
    )
    from chat.services.expense.amount_correction_pending import (
        mark_amount_correction_pending,
    )

    mark_amount_correction_pending(
        block,
        amount=float(updated_pending.get("amount") or 0),
        mode=str(updated_pending.get("mode") or "set"),
        category=str(updated_pending.get("category") or ""),
    )

    if prompt:
        val = validate_expense_items(
            items,
            incurred_date_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            message=message,
        )
        facts = _prompt_message_facts(
            prompt,
            items=items,
            lang=lang,
            prompt_kind="correction",
        )
        return TurnRouteResult(
            handled=True,
            pack=_pack(
                wf,
                block,
                items=items,
                question=prompt,
                warnings=val.warnings,
                inc_iso=inc_iso,
                message_facts=facts,
            ),
        )

    if target:
        amount = float(pending.get("amount") or 0)
        mode = str(pending.get("mode") or "set")
        items, applied = apply_amount_to_target(
            items, block, target, amount=amount, mode=mode
        )
        if not applied:
            from chat.services.expense.confusion_handler import (
                build_amount_correction_disambiguation_prompt,
                list_amount_correction_targets,
            )

            q = build_amount_correction_disambiguation_prompt(
                list_amount_correction_targets(items, block),
                amount,
                lang=lang,
            )
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                message=message,
            )
            facts = _prompt_message_facts(
                q, items=items, lang=lang, prompt_kind="correction"
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
                    message_facts=facts,
                ),
            )
        items = dedupe_expense_items(items)
        block = clear_amount_correction_pending(block)
        set_expense_stage(block, STAGE_COLLECTING)
        val = validate_expense_items(
            items,
            incurred_date_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            message=message,
        )
        from chat.services.expense.slots import SLOT_CATEGORY, SLOT_FROM_TO, SLOT_MORE_LINES
        from chat.services.expense_workflow import (
            _build_wizard_question,
            _has_pending_expense_line,
            _try_advance_to_review,
        )

        facts = None
        if _has_pending_expense_line(block):
            step = str(block.get("pending_step") or "").lower()
            if step == "from_to":
                slot = SLOT_FROM_TO
            elif step == "category":
                slot = SLOT_CATEGORY
            else:
                slot = SLOT_MORE_LINES
            follow_q, facts = _build_wizard_question(
                block, items, primary_slot=slot, lang=lang
            )
            q = "Updated.\n\n" + follow_q
        else:
            adv = _try_advance_to_review(
                wf,
                block,
                items,
                inc_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                message=message,
            )
            if adv:
                adv_q = str(adv.get("question") or "")
                adv["question"] = "Updated.\n\n" + adv_q
                return TurnRouteResult(handled=True, pack=adv)
            q = "Updated.\n\n" + _format_summary(
                items,
                block,
                incurred_date_iso=inc_iso,
                warnings=val.warnings,
                line_flags=val.line_flags,
                lang=lang,
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
                message_facts=facts,
            ),
        )

    from chat.services.expense.amount_correction_pending import (
        build_duplicate_category_amount_prompt,
    )
    from chat.services.expense.confusion_handler import (
        build_amount_correction_disambiguation_prompt,
        list_amount_correction_targets,
    )

    amount = float(pending.get("amount") or 0)
    category = str(pending.get("category") or "").strip()
    targets = list_amount_correction_targets(items, block)
    if category:
        targets = [
            t
            for t in targets
            if str(t.get("category") or "").strip().lower() == category.lower()
        ]
    if len(targets) > 1:
        q = build_duplicate_category_amount_prompt(
            targets,
            amount=amount,
            mode=str(pending.get("mode") or "set"),
            category=category or str(targets[0].get("category") or "?"),
            lang=lang,
        )
    else:
        q = build_amount_correction_disambiguation_prompt(
            list_amount_correction_targets(items, block),
            amount,
            lang=lang,
        )
    val = validate_expense_items(
        items,
        incurred_date_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        message=message,
    )
    facts = _prompt_message_facts(q, items=items, lang=lang, prompt_kind="correction")
    return TurnRouteResult(
        handled=True,
        pack=_pack(
            wf,
            block,
            items=items,
            question=q,
            warnings=val.warnings,
            inc_iso=inc_iso,
            message_facts=facts,
        ),
    )


def _handle_ordinal_amount_confirm_turn(
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
    from chat.services.expense.expense_confirm import (
        build_ordinal_amount_confirm_prompt,
        clear_ordinal_amount_confirm,
        has_ordinal_amount_confirm_pending,
        is_confirmation_no,
        is_confirmation_yes,
        read_ordinal_amount_confirm,
    )
    from chat.services.expense.expense_fsm import set_expense_stage
    from chat.services.expense_workflow import _pack, format_expense_summary

    if not has_ordinal_amount_confirm_pending(block):
        return None
    parsed = read_ordinal_amount_confirm(block)
    if not parsed:
        return None
    idx, new_amt = parsed
    if is_confirmation_no(message):
        block = clear_ordinal_amount_confirm(block)
        q = "আপডেট **বাতিল** — draft **অপরিবর্তিত**।\n\n"
        q += _format_summary(
            items,
            block,
            incurred_date_iso=inc_iso,
            warnings=[],
            line_flags=[],
            lang=lang,
        )
        return TurnRouteResult(
            handled=True,
            pack=_pack(wf, block, items=items, question=q, inc_iso=inc_iso),
        )
    if is_confirmation_yes(message):
        items = [dict(x) for x in items]
        if 0 <= idx < len(items):
            items[idx]["amount"] = new_amt
        block = clear_ordinal_amount_confirm(block)
        set_expense_stage(block, STAGE_COLLECTING)
        q = "আপডেট করা হয়েছে।\n\n" + _format_summary(
            items,
            block,
            incurred_date_iso=inc_iso,
            warnings=[],
            line_flags=[],
            lang=lang,
        )
        return TurnRouteResult(
            handled=True,
            pack=_pack(wf, block, items=items, question=q, inc_iso=inc_iso),
        )
    q = build_ordinal_amount_confirm_prompt(items, idx, new_amt, lang=lang)
    facts = _prompt_message_facts(
        q,
        items=items,
        lang=lang,
        prompt_kind="ordinal_amount",
        message_type="expense_confirm_prompt",
        target_index=idx,
        target_amount=new_amt,
    )
    return TurnRouteResult(
        handled=True,
        pack=_pack(
            wf,
            block,
            items=items,
            question=q,
            inc_iso=inc_iso,
            message_facts=facts,
        ),
    )


def _handle_delete_verify_turn(
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
    """Process pending delete confirmation on any wizard stage."""
    from chat.services.expense.expense_confirm import (
        clear_expense_delete_verify,
        is_confirmation_no,
        is_confirmation_yes,
        is_expense_delete_verify_pending,
        read_expense_delete_verify_index,
    )
    from chat.services.expense.expense_fsm import set_expense_stage
    from chat.services.expense_workflow import _pack, format_expense_summary

    if not is_expense_delete_verify_pending(block):
        return None

    from chat.services.expense.interactive_pending import (
        clear_expense_interactive_pending,
        message_abandons_expense_interactive_pending,
    )

    if message_abandons_expense_interactive_pending(message, block):
        clear_expense_interactive_pending(block)
        return None

    idx = read_expense_delete_verify_index(block)
    if is_confirmation_no(message):
        block = clear_expense_delete_verify(block)
        q = "মুছে ফেলা **বাতিল** করা হয়েছে — draft **অপরিবর্তিত**।\n\n"
        q += _format_summary(
            items,
            block,
            incurred_date_iso=inc_iso,
            warnings=[],
            line_flags=[],
            lang=lang,
        )
        return TurnRouteResult(
            handled=True,
            pack=_pack(wf, block, items=items, question=q, inc_iso=inc_iso),
        )
    if is_confirmation_yes(message):
        if 0 <= idx < len(items):
            items = [dict(x) for x in items]
            del items[idx]
        block = clear_expense_delete_verify(block)
        set_expense_stage(block, STAGE_REVIEW)
        val = validate_expense_items(
            items,
            incurred_date_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            message=message,
        )
        q = "মুছে ফেলা হয়েছে।\n\n" + _format_summary(
            items,
            block,
            incurred_date_iso=inc_iso,
            warnings=val.warnings,
            line_flags=val.line_flags,
            lang=lang,
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
    return None


def _handle_delete_disambiguation_turn(
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
    """Resolve follow-up after bare ``delete koro`` (which line to remove)."""
    from chat.services.expense.delete_disambiguation_pending import (
        apply_delete_target,
        clear_delete_disambiguation_pending,
        has_delete_disambiguation_pending,
        mark_delete_disambiguation_pending,
        resolve_delete_disambiguation_reply,
    )
    from chat.services.expense.expense_confirm import is_confirmation_no
    from chat.services.expense_workflow import (
        _build_wizard_question,
        _has_pending_expense_line,
        _pack,
    )

    if not has_delete_disambiguation_pending(block):
        return None

    from chat.services.expense.active_prompt import (
        KIND_DELETE_CONFIRM,
        KIND_DELETE_PICK,
        active_prompt_kind,
    )

    if active_prompt_kind(block) in (KIND_DELETE_PICK, KIND_DELETE_CONFIRM):
        return None

    from chat.services.expense.interactive_pending import (
        clear_expense_interactive_pending,
        message_abandons_expense_interactive_pending,
    )

    if message_abandons_expense_interactive_pending(message, block):
        clear_expense_interactive_pending(block)
        return None

    reply_lang = lang or "banglish"
    if is_confirmation_no(message):
        clear_delete_disambiguation_pending(block)
        prefix = "Delete **cancelled** — draft unchanged.\n\n"
        if _has_pending_expense_line(block):
            pending_step = str(block.get("pending_step") or "")
            slot = "from_to" if pending_step == "from_to" else "more_lines"
            q, facts = _build_wizard_question(
                block, items, primary_slot=slot, lang=reply_lang
            )
            return TurnRouteResult(
                handled=True,
                pack=_pack(
                    wf,
                    block,
                    items=items,
                    question=prefix + q,
                    inc_iso=inc_iso,
                    message_facts=facts,
                ),
            )
        q, facts = _build_wizard_question(
            block, items, primary_slot="more_lines", lang=reply_lang
        )
        return TurnRouteResult(
            handled=True,
            pack=_pack(
                wf,
                block,
                items=items,
                question=prefix + q,
                inc_iso=inc_iso,
                message_facts=facts,
            ),
        )

    target, prompt = resolve_delete_disambiguation_reply(
        message, items, block, lang=reply_lang
    )
    if prompt:
        mark_delete_disambiguation_pending(block)
        facts = _prompt_message_facts(
            prompt, items=items, lang=reply_lang, prompt_kind="correction"
        )
        return TurnRouteResult(
            handled=True,
            pack=_pack(
                wf,
                block,
                items=items,
                question=prompt,
                inc_iso=inc_iso,
                message_facts=facts,
            ),
        )

    if not target:
        return None

    items, changed = apply_delete_target(items, block, target)
    if not changed:
        return None

    clear_delete_disambiguation_pending(block)
    cat = str(target.get("category") or "").strip()
    amt = float(target.get("amount") or 0)
    prefix = f"Removed **{cat} — {amt:g} Tk**.\n\n"
    pending_step = str(block.get("pending_step") or "")
    if _has_pending_expense_line(block):
        slot = "from_to" if pending_step == "from_to" else "more_lines"
        q, facts = _build_wizard_question(
            block, items, primary_slot=slot, lang=reply_lang
        )
    else:
        q, facts = _build_wizard_question(
            block, items, primary_slot="more_lines", lang=reply_lang
        )
    return TurnRouteResult(
        handled=True,
        pack=_pack(
            wf,
            block,
            items=items,
            question=prefix + q,
            inc_iso=inc_iso,
            message_facts=facts,
        ),
    )


def route_expense_wizard_turn(
    *,
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    stage: str,
    inc_iso: str,
    day_logged_total: float,
    daily_cap: float,
    pipeline_result: Any = None,
    trace_id: str = "",
    lang: str | None = None,
    last_question: str = "",
    router_turn: TurnDecision | None = None,
) -> TurnRouteResult:
    """
    Return handled=True when this turn is fully processed by the unified router.
    """
    pending_step = str(block.get("pending_step") or "")
    pending_line = (
        block.get("pending_line") if isinstance(block.get("pending_line"), dict) else None
    )
    has_pending = bool(pending_line and pending_line.get("amount"))

    from chat.services.expense.add_modify_residual import route_add_modify_residual
    from chat.services.expense.active_prompt_handlers import handle_active_prompt_turn

    residual_turn = route_add_modify_residual(
        wf=wf,
        block=block,
        items=items,
        message=message,
        inc_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        lang=lang,
    )
    if residual_turn is not None:
        return residual_turn

    active_prompt_turn = handle_active_prompt_turn(
        wf=wf,
        block=block,
        items=items,
        message=message,
        inc_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        lang=lang,
    )
    if active_prompt_turn is not None:
        return active_prompt_turn

    delete_turn = _handle_delete_verify_turn(
        wf=wf,
        block=block,
        items=items,
        message=message,
        inc_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        lang=lang,
    )
    if delete_turn is not None:
        return delete_turn

    delete_disambig_turn = _handle_delete_disambiguation_turn(
        wf=wf,
        block=block,
        items=items,
        message=message,
        inc_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        lang=lang,
    )
    if delete_disambig_turn is not None:
        return delete_disambig_turn

    amount_pending_turn = _handle_amount_correction_pending_turn(
        wf=wf,
        block=block,
        items=items,
        message=message,
        inc_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        lang=lang,
    )
    if amount_pending_turn is not None:
        return amount_pending_turn

    ordinal_turn = _handle_ordinal_amount_confirm_turn(
        wf=wf,
        block=block,
        items=items,
        message=message,
        inc_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        lang=lang,
    )
    if ordinal_turn is not None:
        return ordinal_turn

    if stage not in (STAGE_COLLECTING, STAGE_REVIEW, STAGE_SUBMIT_CONFIRM):
        return TurnRouteResult(handled=False)

    if stage == STAGE_SUBMIT_CONFIRM:
        from chat.services.leave_fsm import is_awaiting_leave_confirmation

        if is_awaiting_leave_confirmation(wf):
            return TurnRouteResult(handled=False)

        from chat.services.expense_workflow import handle_submit_confirm_turn

        return TurnRouteResult(
            handled=True,
            pack=handle_submit_confirm_turn(
                wf,
                block,
                items,
                message,
                inc_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                lang=lang,
            ),
        )

    if stage == STAGE_COLLECTING and not items and not pending_step:
        return TurnRouteResult(handled=False)

    decision = resolve_expense_turn(
        message,
        items=items,
        stage=stage,
        pending_step=pending_step,
        pending_line=pending_line,
        has_pending_line=has_pending,
        block=block,
        last_question=last_question,
        trace_id=trace_id,
        router_turn=router_turn,
    )

    if not decision.is_handled():
        return TurnRouteResult(handled=False)

    if decision.turn_type == TURN_FILL_SLOT:
        return _route_fill_slot(
            wf=wf,
            block=block,
            items=items,
            message=message,
            inc_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            trace_id=trace_id,
            last_question=last_question,
        )

    if decision.turn_type == TURN_CLARIFY_REPLY:
        return _route_fill_slot(
            wf=wf,
            block=block,
            items=items,
            message=message,
            inc_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            trace_id=trace_id,
            last_question=last_question,
        )

    if decision.turn_type == TURN_NAVIGATE:
        if stage == STAGE_REVIEW and decision.submit_draft:
            return _route_review_confirm(
                wf=wf,
                block=block,
                items=items,
                message=message,
                inc_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                lang=lang,
                trace_id=trace_id,
                last_question=last_question,
            )
        return _route_navigate(
            wf=wf,
            block=block,
            items=items,
            message=message,
            inc_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            lang=lang,
        )

    if decision.turn_type == TURN_CONFIRM:
        if stage == STAGE_REVIEW:
            return _route_review_confirm(
                wf=wf,
                block=block,
                items=items,
                message=message,
                inc_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                lang=lang,
                trace_id=trace_id,
                last_question=last_question,
            )
        return TurnRouteResult(handled=False)

    if decision.turn_type == TURN_DENY:
        if stage == STAGE_REVIEW:
            return _route_review_deny(
                wf=wf,
                block=block,
                items=items,
                message=message,
                inc_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                lang=lang,
            )
        return TurnRouteResult(handled=False)

    if decision.turn_type == TURN_ADD_LINES:
        from chat.services.expense.expense_confirm import (
            looks_like_new_expense_during_pending_slot,
        )
        from chat.services.expense.wizard_commands import (
            message_has_ingestible_claim_body,
            strip_expense_submit_tail_for_parse,
            wants_expense_submit_command,
        )

        ingest_message = message
        if wants_expense_submit_command(message):
            stripped = strip_expense_submit_tail_for_parse(message)
            if message_has_ingestible_claim_body(stripped, original=message):
                ingest_message = stripped

        if pending_step in ("from_to", "category"):
            pending_dict = (
                pending_line if isinstance(pending_line, dict) else {}
            )
            allow_pending_ingest = (
                decision.source
                in (
                    "rules_submit_with_claims",
                    "rules_fresh_multi_during_slot",
                    "rules_new_line_during_slot",
                )
                or looks_like_new_expense_during_pending_slot(
                    ingest_message,
                    pending_dict,
                    items,
                    block,
                    pending_step=pending_step,
                )
            )
            if allow_pending_ingest:
                from chat.services.expense_workflow import (
                    _ingest_new_claim_preserving_pending_category,
                    _ingest_new_claim_preserving_pending_from_to,
                    try_finish_collecting_after_ingest,
                )

                if pending_step == "from_to":
                    pack = _ingest_new_claim_preserving_pending_from_to(
                        wf,
                        block,
                        items,
                        pending_dict,
                        ingest_message,
                        inc_iso=inc_iso,
                        day_logged_total=day_logged_total,
                        daily_cap=daily_cap,
                        trace_id=trace_id,
                        pipeline_result=pipeline_result,
                    )
                else:
                    pack = _ingest_new_claim_preserving_pending_category(
                        wf,
                        block,
                        items,
                        pending_dict,
                        ingest_message,
                        inc_iso=inc_iso,
                        day_logged_total=day_logged_total,
                        daily_cap=daily_cap,
                        trace_id=trace_id,
                        pipeline_result=pipeline_result,
                    )
                if decision.submit_draft or decision.finish_collecting:
                    finish = try_finish_collecting_after_ingest(
                        wf,
                        block,
                        list(pack.get("items") or items),
                        message,
                        inc_iso=inc_iso,
                        lang=lang or "banglish",
                        day_logged_total=day_logged_total,
                        daily_cap=daily_cap,
                        trace_id=trace_id,
                        last_question=last_question,
                    )
                    if finish:
                        pack = finish
                return TurnRouteResult(handled=True, pack=pack)
            return TurnRouteResult(handled=False)
        return _route_add_lines(
            wf=wf,
            block=block,
            items=items,
            message=message,
            inc_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            pipeline_result=pipeline_result,
            lang=lang,
            submit_after_ingest=bool(
                decision.submit_draft or decision.finish_collecting
            ),
            trace_id=trace_id,
            last_question=last_question,
        )

    if decision.turn_type == TURN_PRAISE and stage in (STAGE_REVIEW, STAGE_SUBMIT_CONFIRM):
        return _route_praise(
            wf=wf,
            block=block,
            items=items,
            message=message,
            stage=stage,
            inc_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            lang=lang,
            trace_id=trace_id,
            last_question=last_question,
        )

    if decision.turn_type == TURN_EDIT_DRAFT:
        return _route_edit_draft(
            wf=wf,
            block=block,
            items=items,
            message=message,
            stage=stage,
            inc_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            decision_plan=decision.plan,
            decision_source=decision.source,
            uncertain_note=decision.uncertain_note,
            lang=lang,
            trace_id=trace_id,
            last_question=last_question,
            pending_step=pending_step,
            pending_line=pending_line,
        )

    if decision.turn_type == TURN_UNCLEAR:
        return _route_unclear(
            wf=wf,
            block=block,
            items=items,
            message=message,
            stage=stage,
            inc_iso=inc_iso,
            uncertain_note=decision.uncertain_note,
            lang=lang,
        )

    return TurnRouteResult(handled=False)


def _route_fill_slot(
    *,
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    inc_iso: str,
    day_logged_total: float,
    daily_cap: float,
    trace_id: str = "",
    last_question: str = "",
) -> TurnRouteResult:
    from chat.services.expense_workflow import _handle_pending_line, _pack

    pending = block.get("pending_line")
    if not isinstance(pending, dict) or not pending.get("amount"):
        return TurnRouteResult(handled=False)

    pending_step = str(block.get("pending_step") or "")
    from chat.services.expense.expense_confirm import looks_like_new_expense_during_pending_slot

    if pending_step in ("from_to", "category") and looks_like_new_expense_during_pending_slot(
        message,
        dict(pending),
        items,
        block,
        pending_step=pending_step,
    ):
        from chat.services.expense_workflow import (
            _ingest_new_claim_preserving_pending_category,
            _ingest_new_claim_preserving_pending_from_to,
        )

        if pending_step == "from_to":
            pack = _ingest_new_claim_preserving_pending_from_to(
                wf,
                block,
                items,
                dict(pending),
                message,
                inc_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                trace_id=trace_id,
            )
        else:
            pack = _ingest_new_claim_preserving_pending_category(
                wf,
                block,
                items,
                dict(pending),
                message,
                inc_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                trace_id=trace_id,
            )
        return TurnRouteResult(handled=True, pack=pack)

    pack = _handle_pending_line(
        wf,
        block,
        items,
        dict(pending),
        message,
        inc_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        trace_id=trace_id,
        last_question=last_question,
    )
    return TurnRouteResult(handled=True, pack=pack)


def _route_navigate(
    *,
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    inc_iso: str,
    day_logged_total: float,
    daily_cap: float,
    lang: str | None,
) -> TurnRouteResult:
    from chat.services.expense.wizard_commands import (
        message_has_ingestible_claim_body,
        strip_expense_submit_tail_for_parse,
        wants_expense_submit_command,
    )
    from chat.services.expense_workflow import (
        _ingest_extracted_lines,
        _pack,
        _pack_ingest_interrupt,
        _respond_done_while_incomplete,
        _try_advance_to_review,
        try_finish_collecting_after_ingest,
    )

    if wants_expense_submit_command(message):
        body = strip_expense_submit_tail_for_parse(message)
        if message_has_ingestible_claim_body(body, original=message):
            from chat.services.expense_extraction import extract_expense_items

            ext = extract_expense_items(body)
            if ext.items:
                items, blocked = _ingest_extracted_lines(
                    block,
                    items,
                    ext,
                    inc_iso=inc_iso,
                    message=body,
                    wf=wf,
                )
                if blocked:
                    return TurnRouteResult(
                        handled=True,
                        pack=_pack_ingest_interrupt(
                            wf, block, items, blocked, inc_iso=inc_iso
                        ),
                    )
                finish = try_finish_collecting_after_ingest(
                    wf,
                    block,
                    items,
                    message,
                    inc_iso=inc_iso,
                    lang=lang or "banglish",
                    day_logged_total=day_logged_total,
                    daily_cap=daily_cap,
                )
                if finish:
                    return TurnRouteResult(handled=True, pack=finish)

    done_incomplete = _respond_done_while_incomplete(
        wf,
        block,
        items,
        inc_iso=inc_iso,
        lang=lang or "banglish",
    )
    if done_incomplete:
        return TurnRouteResult(handled=True, pack=done_incomplete)

    is_submit = wants_expense_submit_command(message)

    adv = _try_advance_to_review(
        wf,
        block,
        items,
        inc_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        message=message,
    )
    if is_submit:
        from chat.services.expense_workflow import _try_enter_submit_confirm

        submit_pack = _try_enter_submit_confirm(
            wf,
            block,
            items,
            message=message,
            inc_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            lang=lang or "banglish",
            trace_id="",
        )
        if submit_pack:
            return TurnRouteResult(handled=True, pack=submit_pack)
    if adv:
        return TurnRouteResult(handled=True, pack=adv)
    return TurnRouteResult(handled=False)


def _route_add_lines(
    *,
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    inc_iso: str,
    day_logged_total: float,
    daily_cap: float,
    pipeline_result: Any,
    lang: str | None,
    submit_after_ingest: bool = False,
    trace_id: str = "",
    last_question: str = "",
) -> TurnRouteResult:
    from chat.services.expense.add_modify import (
        build_add_modify_prompt,
        should_prompt_add_modify,
        start_add_modify_prompt,
        user_explicitly_wants_add,
    )
    from chat.services.expense.draft_summary import format_numbered_draft_summary
    from chat.services.expense.draft_view import ExpenseDraftView
    from chat.services.expense.expense_ingest_guard import should_block_compound_reingest
    from chat.services.expense_extraction import extract_expense_items
    from chat.services.expense.wizard_commands import (
        message_has_ingestible_claim_body,
        strip_expense_submit_tail_for_parse,
        wants_expense_submit_command,
    )
    from chat.services.expense_workflow import (
        _ask_more_lines_prompt,
        _ingest_extracted_lines,
        _pack,
        _pack_ingest_interrupt,
        _try_advance_to_review,
        _unallocated_total_prompt,
        format_expense_summary,
        try_finish_collecting_after_ingest,
    )

    ingest_message = message
    if wants_expense_submit_command(message):
        stripped = strip_expense_submit_tail_for_parse(message)
        if message_has_ingestible_claim_body(stripped, original=message):
            ingest_message = stripped
            submit_after_ingest = True

    force_add = block.pop("_add_modify_force_add", None)
    if isinstance(force_add, dict) and force_add.get("category"):
        cat = str(force_add.get("category") or "")
        amt = float(force_add.get("amount") or 0)
        ingest_msg = f"{cat} {amt:g} taka"
        ext = extract_expense_items(ingest_msg)
        before_count = len(items)
        items, blocked = _ingest_extracted_lines(
            block, items, ext, inc_iso=inc_iso, message=ingest_msg, wf=wf
        )
        if blocked:
            return TurnRouteResult(
                handled=True,
                pack=_pack_ingest_interrupt(wf, block, items, blocked, inc_iso=inc_iso),
            )
        if len(items) > before_count:
            clear_ingest_lock(block)
        q = format_numbered_draft_summary(
            items, block, incurred_date_iso=inc_iso, lang=lang
        )
        adv = _try_advance_to_review(
            wf,
            block,
            items,
            inc_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            message=message,
        )
        if adv:
            return TurnRouteResult(handled=True, pack=adv)
        more_q, facts = _ask_more_lines_prompt(block, items, lang=lang)
        return TurnRouteResult(
            handled=True,
            pack=_pack(
                wf,
                block,
                items=items,
                question=q + "\n\n" + more_q,
                inc_iso=inc_iso,
                message_facts=facts,
            ),
        )

    if not user_explicitly_wants_add(message):
        ext_probe = extract_expense_items(message)
        if ext_probe.items:
            raw_row = ext_probe.items[0]
            row = (
                raw_row
                if isinstance(raw_row, dict)
                else {
                    "category": getattr(raw_row, "category", ""),
                    "amount": getattr(raw_row, "amount", 0),
                    "from_location": getattr(raw_row, "from_location", ""),
                    "to_location": getattr(raw_row, "to_location", ""),
                }
            )
            cat = str(row.get("category") or "")
            amt = float(row.get("amount") or 0)
            view = ExpenseDraftView(items, block)
            if should_prompt_add_modify(
                message,
                view,
                category=cat,
                amount=amt,
                from_location=str(row.get("from_location") or ""),
                to_location=str(row.get("to_location") or ""),
            ):
                start_add_modify_prompt(
                    block,
                    category=cat,
                    amount=amt,
                    from_location=str(row.get("from_location") or ""),
                    to_location=str(row.get("to_location") or ""),
                )
                q = build_add_modify_prompt(view, category=cat, amount=amt, lang=lang)
                facts = _prompt_message_facts(
                    q, items=items, lang=lang, prompt_kind="add_modify"
                )
                return TurnRouteResult(
                    handled=True,
                    pack=_pack(
                        wf,
                        block,
                        items=items,
                        question=q,
                        inc_iso=inc_iso,
                        message_facts=facts,
                    ),
                )

    if items and should_block_compound_reingest(block, message, items):
        val = validate_expense_items(
            items,
            incurred_date_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            message=message,
        )
        q = (
            duplicate_reentry_notice(lang)
            + "\n\n"
            + _format_summary(
                items,
                block,
                incurred_date_iso=inc_iso,
                warnings=val.warnings,
                line_flags=val.line_flags,
                lang=lang,
            )
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

    if looks_like_duplicate_expense_reentry(message, items):
        return TurnRouteResult(
            handled=True,
            pack=_pack(
                wf,
                block,
                items=items,
                question=duplicate_reentry_notice(lang),
                inc_iso=inc_iso,
            ),
        )

    if pipeline_result and pipeline_result.extraction is not None:
        ext = pipeline_result.extraction
    else:
        from chat.services.expense_extraction import extract_expense_items

        ext = extract_expense_items(ingest_message)
    if not ext.items and not ext.malformed:
        return TurnRouteResult(handled=False)

    before_count = len(items)
    items, blocked = _ingest_extracted_lines(
        block, items, ext, inc_iso=inc_iso, message=ingest_message, wf=wf
    )
    if blocked:
        return TurnRouteResult(
            handled=True,
            pack=_pack_ingest_interrupt(
                wf, block, items, blocked, inc_iso=inc_iso
            ),
        )

    if len(items) > before_count:
        clear_ingest_lock(block)

    if submit_after_ingest:
        finish = try_finish_collecting_after_ingest(
            wf,
            block,
            items,
            message,
            inc_iso=inc_iso,
            lang=lang or "banglish",
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            trace_id=trace_id,
            last_question=last_question,
        )
        if finish:
            return TurnRouteResult(handled=True, pack=finish)

    gap_q = _unallocated_total_prompt(ingest_message, items, lang=lang)
    if gap_q:
        return TurnRouteResult(
            handled=True,
            pack=_pack(wf, block, items=items, question=gap_q, inc_iso=inc_iso),
        )

    adv = _try_advance_to_review(
        wf,
        block,
        items,
        inc_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        message=message,
    )
    if adv and len(ext.items) >= 2 and not block.get("pending_line"):
        return TurnRouteResult(handled=True, pack=adv)

    q, facts = _ask_more_lines_prompt(block, items, lang=lang)
    return TurnRouteResult(
        handled=True,
        pack=_pack(
            wf,
            block,
            items=items,
            question=q,
            inc_iso=inc_iso,
            message_facts=facts,
        ),
    )


def _route_edit_draft(
    *,
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    stage: str,
    inc_iso: str,
    day_logged_total: float,
    daily_cap: float,
    decision_plan: Any,
    decision_source: str,
    uncertain_note: str,
    lang: str | None,
    trace_id: str = "",
    last_question: str = "",
    pending_step: str = "",
    pending_line: dict[str, Any] | None = None,
) -> TurnRouteResult:
    from chat.services.expense.command_executor import apply_message_corrections
    from chat.services.expense.expense_confirm import (
        build_delete_confirm_prompt,
        clear_expense_delete_verify,
        is_confirmation_no,
        is_confirmation_yes,
        is_expense_delete_verify_pending,
        mark_expense_delete_verify,
        parse_ordinal_delete_index,
        read_expense_delete_verify_index,
    )
    from chat.services.expense.expense_fsm import set_expense_stage
    from chat.services.expense.session_action_memory import record_expense_corrected
    from chat.services.expense_workflow import (
        _ask_more_lines_prompt,
        _pack,
        format_expense_summary,
    )

    from chat.services.expense.command_parser import parse_ordinal_set_amount
    from chat.services.expense.expense_confirm import (
        build_ordinal_amount_confirm_prompt,
        has_ordinal_amount_confirm_pending,
        mark_ordinal_amount_confirm,
    )

    if not is_expense_delete_verify_pending(block) and not has_ordinal_amount_confirm_pending(
        block
    ):
        ord_set = parse_ordinal_set_amount(message, item_count=len(items))
        if ord_set and items:
            idx, new_amt = ord_set
            if 0 <= idx < len(items):
                mark_ordinal_amount_confirm(block, index=idx, amount=new_amt)
                q = build_ordinal_amount_confirm_prompt(items, idx, new_amt, lang=lang)
                val = validate_expense_items(
                    items,
                    incurred_date_iso=inc_iso,
                    day_logged_total=day_logged_total,
                    daily_cap=daily_cap,
                    message=message,
                )
                q += "\n\n" + _format_summary(
                items,
                block,
                    incurred_date_iso=inc_iso,
                    warnings=val.warnings,
                    line_flags=val.line_flags,
                    lang=lang,
                )
                head = build_ordinal_amount_confirm_prompt(
                    items, idx, new_amt, lang=lang
                )
                facts = _prompt_message_facts(
                    head,
                    items=items,
                    lang=lang,
                    prompt_kind="ordinal_amount",
                    message_type="expense_confirm_prompt",
                    target_index=idx,
                    target_amount=new_amt,
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
                        message_facts=facts,
                    ),
                )

    if is_expense_delete_verify_pending(block):
        idx = read_expense_delete_verify_index(block)
        if is_confirmation_no(message):
            block = clear_expense_delete_verify(block)
            q = "মুছে ফেলা **বাতিল** করা হয়েছে — draft **অপরিবর্তিত**।\n\n"
            q += _format_summary(
                items,
                block,
                incurred_date_iso=inc_iso,
                warnings=[],
                line_flags=[],
                lang=lang,
            )
            return TurnRouteResult(
                handled=True,
                pack=_pack(wf, block, items=items, question=q, inc_iso=inc_iso),
            )
        if is_confirmation_yes(message):
            if 0 <= idx < len(items):
                items = [dict(x) for x in items]
                del items[idx]
            block = clear_expense_delete_verify(block)
            set_expense_stage(block, STAGE_REVIEW)
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                message=message,
            )
            q = "মুছে ফেলা হয়েছে।\n\n" + _format_summary(
                items,
                block,
                incurred_date_iso=inc_iso,
                warnings=val.warnings,
                line_flags=val.line_flags,
                lang=lang,
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
    else:
        del_idx = parse_ordinal_delete_index(message)
        if del_idx is not None and 0 <= del_idx < len(items):
            block = mark_expense_delete_verify(block, del_idx)
            q = build_delete_confirm_prompt(items, del_idx, lang=lang)
            return TurnRouteResult(
                handled=True,
                pack=_pack(wf, block, items=items, question=q, inc_iso=inc_iso),
            )

    from chat.services.expense.amount_correction_pending import (
        build_duplicate_category_amount_prompt,
        clear_amount_correction_pending,
        mark_amount_correction_pending,
        plan_has_ambiguous_category_op,
    )
    from chat.services.expense.confusion_handler import list_amount_correction_targets
    from chat.services.expense.expense_confirm import parse_bare_amount_correction

    review_snapshot = [dict(x) for x in items]
    if stage == STAGE_REVIEW:
        wf = push_expense_snapshot(
            wf,
            items=items,
            stage=STAGE_REVIEW,
            action_type="before_correction",
            incurred_date_iso=inc_iso,
            lang=lang,
        )

    active_plan = decision_plan
    if not active_plan.has_any_correction():
        from chat.services.expense.command_parser import parse_correction_plan

        active_plan = parse_correction_plan(message, item_count=len(items))

    amb = plan_has_ambiguous_category_op(active_plan, items)
    if amb:
        cat, amt, mode = amb
        mark_amount_correction_pending(block, amount=amt, mode=mode, category=cat)
        # If the same message already disambiguates the line (ordinal / route /
        # amount hint), e.g. "second bus 90 taka hobe", resolve it immediately
        # instead of asking "which one?" and getting stuck in a clarify loop.
        resolved = _handle_amount_correction_pending_turn(
            wf=wf,
            block=block,
            items=items,
            message=message,
            inc_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            lang=lang,
        )
        if resolved is not None:
            return resolved
        mark_amount_correction_pending(block, amount=amt, mode=mode, category=cat)
        cat_targets = [
            t
            for t in list_amount_correction_targets(items, block)
            if str(t.get("category") or "").strip().lower() == cat.lower()
        ]
        q = build_duplicate_category_amount_prompt(
            cat_targets,
            amount=amt,
            mode=mode,
            category=cat,
            lang=lang,
        )
        val = validate_expense_items(
            items,
            incurred_date_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            message=message,
        )
        facts = _prompt_message_facts(
            q, items=items, lang=lang, prompt_kind="correction"
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
                message_facts=facts,
            ),
        )

    for from_cat, to_cat in getattr(active_plan, "replacements", None) or []:
        replace_targets = [
            t
            for t in list_amount_correction_targets(items, block)
            if str(t.get("category") or "").strip().lower() == from_cat.lower()
        ]
        if len(replace_targets) > 1:
            from chat.services.expense.confusion_handler import (
                build_category_replace_disambiguation_prompt,
            )

            q = build_category_replace_disambiguation_prompt(
                replace_targets,
                from_category=from_cat,
                to_category=to_cat,
                lang=lang,
            )
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                message=message,
            )
            facts = _prompt_message_facts(
                q, items=items, lang=lang, prompt_kind="correction"
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
                    message_facts=facts,
                ),
            )

    if decision_plan.has_any_correction():
        result = execute_correction_plan(items, decision_plan, block=block)
        items = dedupe_expense_items(result.items)
        corrected = result.changed
        parse_source = decision_source
    else:
        corr = apply_message_corrections(
            items,
            message,
            extract_lines=None,
            trace_id=trace_id,
            use_llm=True,
            review_stage=(stage == STAGE_REVIEW),
            block=block,
            last_question=last_question,
            stage=stage,
            pending_step=pending_step,
            pending_line=pending_line,
        )
        items = dedupe_expense_items(corr.items)
        corrected = corr.changed
        parse_source = corr.parse_source

    if corrected:
        clear_amount_correction_pending(block)

    if not corrected:
        fail = build_correction_failure_notice(
            message, items, lang=lang, block=block
        )
        if fail:
            bare_amt = parse_bare_amount_correction(message)
            if bare_amt is not None and len(list_amount_correction_targets(items, block)) > 1:
                mark_amount_correction_pending(block, amount=bare_amt, mode="set")
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                message=message,
            )
            q = fail
            if stage == STAGE_REVIEW:
                q += "\n\n" + _format_summary(
                items,
                block,
                    incurred_date_iso=inc_iso,
                    warnings=val.warnings,
                    line_flags=val.line_flags,
                    lang=lang,
                )
            facts = _prompt_message_facts(
                fail,
                items=items,
                lang=lang,
                prompt_kind="correction",
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
                    message_facts=facts,
                ),
            )
        return TurnRouteResult(handled=False)

    if not items and review_snapshot:
        items = review_snapshot
        val = validate_expense_items(
            items,
            incurred_date_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            message=message,
        )
        q = (
            correction_unclear_notice(lang)
            + "\n\n"
            + review_denial_hints(lang)
            + "\n\n"
            + _format_summary(
                items,
                block,
                incurred_date_iso=inc_iso,
                warnings=val.warnings,
                line_flags=val.line_flags,
                lang=lang,
            )
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

    val = validate_expense_items(
        items,
        incurred_date_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        message=message,
    )

    if stage == STAGE_COLLECTING:
        pending = block.get("pending_line")
        pending_step = str(block.get("pending_step") or "")
        prefix = "আপডেট করা হয়েছে।\n\n" if corrected else ""
        if (
            isinstance(pending, dict)
            and pending.get("amount")
            and pending_step == "from_to"
        ):
            from chat.services.expense_workflow import _ask_from_to_prompt

            cat = str(pending.get("category") or "").strip()
            amt = float(pending.get("amount") or 0)
            q, facts = _ask_from_to_prompt(block, items, cat, amt, lang=lang)
            return TurnRouteResult(
                handled=True,
                pack=_pack(
                    wf,
                    block,
                    items=items,
                    question=prefix + q,
                    warnings=val.warnings,
                    inc_iso=inc_iso,
                    message_facts=facts,
                ),
            )
        q, facts = _ask_more_lines_prompt(block, items, lang=lang)
        return TurnRouteResult(
            handled=True,
            pack=_pack(
                wf,
                block,
                items=items,
                question=prefix + q,
                warnings=val.warnings,
                inc_iso=inc_iso,
                message_facts=facts,
            ),
        )

    set_expense_stage(block, STAGE_REVIEW)
    block["review_line_flags"] = val.line_flags
    q = "আপডেট করা হয়েছে।\n\n" + _format_summary(
                items,
                block,
        incurred_date_iso=inc_iso,
        warnings=val.warnings,
        line_flags=val.line_flags,
        lang=lang,
    )
    if wants_travel_group_remove(message):
        from chat.services.expense.expense_ingest_guard import REASON_TRAVEL_REMOVED, set_ingest_lock

        set_ingest_lock(block, reason=REASON_TRAVEL_REMOVED)
        action_type = "after_travel_remove"
    else:
        clear_ingest_lock(block)
        action_type = "after_correction"

    wf = push_expense_snapshot(
        wf,
        items=items,
        stage=STAGE_REVIEW,
        action_type=action_type,
        incurred_date_iso=inc_iso,
        lang=lang,
    )
    wf = record_expense_corrected(
        wf,
        items=items,
        incurred_date_iso=inc_iso,
        stage="review",
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


def _route_praise(
    *,
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    stage: str,
    inc_iso: str,
    day_logged_total: float,
    daily_cap: float,
    lang: str | None,
    trace_id: str = "",
    last_question: str = "",
) -> TurnRouteResult:
    from chat.services.expense_workflow import _build_review_praise_response, _pack

    val = validate_expense_items(
        items,
        incurred_date_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        message=message,
    )
    q, facts = _build_review_praise_response(
        message=message,
        items=items,
        inc_iso=inc_iso,
        warnings=val.warnings,
        line_flags=val.line_flags,
        lang=lang,
        trace_id=trace_id,
        last_question=last_question,
        wizard_stage=stage,
        submit_command=False,
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
            message_facts=facts,
        ),
    )


def _route_unclear(
    *,
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    stage: str,
    inc_iso: str,
    uncertain_note: str,
    lang: str | None,
) -> TurnRouteResult:
    from chat.services.expense_workflow import _pack, format_expense_summary

    val = validate_expense_items(
        items,
        incurred_date_iso=inc_iso,
        day_logged_total=0.0,
        daily_cap=300.0,
        message=message,
    )
    hint = uncertain_note or correction_unclear_notice(lang)
    hint += (
        "\n\nউদাহরণ: `lunch er jaigai snack`, `bus 70 hobe`, `lunch baad daw`"
    )
    if stage == STAGE_REVIEW:
        hint += "\n\n" + _format_summary(
            items,
            block,
            incurred_date_iso=inc_iso,
            warnings=val.warnings,
            line_flags=val.line_flags,
            lang=lang,
        )
    return TurnRouteResult(
        handled=True,
        pack=_pack(
            wf,
            block,
            items=items,
            question=hint,
            warnings=val.warnings,
            inc_iso=inc_iso,
        ),
    )


def _route_review_confirm(
    *,
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    inc_iso: str,
    day_logged_total: float,
    daily_cap: float,
    lang: str | None,
    trace_id: str = "",
    last_question: str = "",
) -> TurnRouteResult:
    from chat.services.expense.expense_fsm import set_expense_stage
    from chat.services.expense.slots import STAGE_SUBMIT_CONFIRM
    from chat.services.expense_incurred_date import expense_submit_date_block_reason
    from chat.services.expense_workflow import (
        _build_submit_confirm_response,
        _pack,
    )

    val = validate_expense_items(
        items,
        incurred_date_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
    )
    if not val.ok:
        set_expense_stage(block, STAGE_COLLECTING)
        return TurnRouteResult(
            handled=True,
            pack=_pack(
                wf,
                block,
                items=items,
                question=val.blocking_message,
                warnings=val.warnings,
                inc_iso=inc_iso,
                validation_blocked=True,
            ),
        )
    # No explicit `today=`: the helper resolves it from its own module-level
    # `date`, keeping one patchable clock for all submit-date policy checks.
    date_block = expense_submit_date_block_reason(inc_iso)
    if date_block:
        return TurnRouteResult(
            handled=True,
            pack=_pack(
                wf,
                block,
                items=items,
                question=(
                    f"{date_block}\n\n"
                    "তারিখ ঠিক করে আবার বলুন (যেমন **ajke** lunch 100), তারপর summary দেখুন।"
                ),
                warnings=val.warnings,
                inc_iso=inc_iso,
                validation_blocked=True,
            ),
        )
    set_expense_stage(block, STAGE_SUBMIT_CONFIRM)
    submit_q, submit_facts = _build_submit_confirm_response(
        message,
        lang=lang,
        trace_id=trace_id,
        last_question=last_question,
    )
    return TurnRouteResult(
        handled=True,
        pack=_pack(
            wf,
            block,
            items=items,
            question=submit_q,
            warnings=val.warnings,
            inc_iso=inc_iso,
            message_facts=submit_facts,
        ),
    )


def _route_review_deny(
    *,
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    inc_iso: str,
    day_logged_total: float,
    daily_cap: float,
    lang: str | None,
) -> TurnRouteResult:
    from chat.services.expense_workflow import _pack, format_expense_summary

    val = validate_expense_items(
        items,
        incurred_date_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        message=message,
    )
    q = (
        review_denial_hints(lang)
        + "\n\n"
        + _format_summary(
            items,
            block,
            incurred_date_iso=inc_iso,
            warnings=val.warnings,
            line_flags=val.line_flags,
            lang=lang,
        )
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
