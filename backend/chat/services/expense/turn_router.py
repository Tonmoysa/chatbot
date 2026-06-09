"""
Route draft-aware turn decisions inside the expense wizard (Phases A–D).

Single entry: rules/LLM turn parse → deterministic handler.
"""

from __future__ import annotations

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
    TurnRouteResult,
)
from chat.services.expense.slots import STAGE_COLLECTING, STAGE_REVIEW, STAGE_SUBMIT_CONFIRM
from chat.services.expense_validation import validate_expense_items


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
) -> TurnRouteResult:
    """
    Return handled=True when this turn is fully processed by the unified router.
    """
    pending_step = str(block.get("pending_step") or "")
    pending_line = (
        block.get("pending_line") if isinstance(block.get("pending_line"), dict) else None
    )
    has_pending = bool(pending_line and pending_line.get("amount"))

    if stage not in (STAGE_COLLECTING, STAGE_REVIEW, STAGE_SUBMIT_CONFIRM):
        return TurnRouteResult(handled=False)

    if stage == STAGE_SUBMIT_CONFIRM:
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
        # Let legacy workflow handle compound input while a slot is open.
        if pending_step in ("from_to", "category"):
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
    from chat.services.expense_workflow import (
        _pack,
        _respond_done_while_incomplete,
        _try_advance_to_review,
    )

    done_incomplete = _respond_done_while_incomplete(
        wf,
        block,
        items,
        inc_iso=inc_iso,
        lang=lang or "banglish",
    )
    if done_incomplete:
        return TurnRouteResult(handled=True, pack=done_incomplete)

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
) -> TurnRouteResult:
    from chat.services.expense.expense_ingest_guard import should_block_compound_reingest
    from chat.services.expense_workflow import (
        _ask_more_lines_prompt,
        _ingest_extracted_lines,
        _pack,
        _pack_ingest_interrupt,
        _try_advance_to_review,
        _unallocated_total_prompt,
        format_expense_summary,
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
            + format_expense_summary(
                items,
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

        ext = extract_expense_items(message)
    if not ext.items and not ext.malformed:
        return TurnRouteResult(handled=False)

    before_count = len(items)
    items, blocked = _ingest_extracted_lines(
        block, items, ext, inc_iso=inc_iso, message=message, wf=wf
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

    gap_q = _unallocated_total_prompt(message, items, lang=lang)
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
    from chat.services.expense.expense_fsm import set_expense_stage
    from chat.services.expense.session_action_memory import record_expense_corrected
    from chat.services.expense_workflow import (
        _ask_more_lines_prompt,
        _pack,
        format_expense_summary,
    )

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

    if not corrected:
        fail = build_correction_failure_notice(message, items, lang=lang)
        if fail:
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                message=message,
            )
            q = fail
            if stage == STAGE_REVIEW:
                q += "\n\n" + format_expense_summary(
                    items,
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
            + format_expense_summary(
                items,
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
    q = "আপডেট করা হয়েছে।\n\n" + format_expense_summary(
        items,
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
        hint += "\n\n" + format_expense_summary(
            items,
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
    from datetime import date

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
    date_block = expense_submit_date_block_reason(inc_iso, today=date.today())
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
        + format_expense_summary(
            items,
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
