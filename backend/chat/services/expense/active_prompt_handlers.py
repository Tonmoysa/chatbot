"""Execute active expense prompts (delete pick, add/modify, delete confirm)."""

from __future__ import annotations

from typing import Any

from chat.services.expense.active_prompt import (
    KIND_ADD_MODIFY_CHOICE,
    KIND_DELETE_CONFIRM,
    KIND_DELETE_PICK,
    KIND_MODIFY_TARGET,
    clear_active_prompt,
    read_active_prompt,
)
from chat.services.expense.modify_flow import handle_modify_target_turn
from chat.services.expense.add_modify import handle_add_modify_prompt_turn
from chat.services.expense.active_prompt import set_active_prompt
from chat.services.expense.delete_flow import (
    apply_delete_line,
    build_delete_confirm_prompt,
    build_numbered_delete_prompt,
    format_after_delete_summary,
    resolve_delete_pick,
    start_delete_confirm,
    start_numbered_delete,
)
from chat.services.expense.draft_view import ExpenseDraftView
from chat.services.expense.expense_confirm import is_confirmation_no, is_confirmation_yes
from chat.services.expense.interactive_pending import message_abandons_expense_interactive_pending
from chat.services.expense.turn_schema import TurnRouteResult
from chat.services.expense_validation import validate_expense_items


def _message_facts(
    question: str,
    *,
    items: list[dict[str, Any]],
    lang: str | None,
    prompt_kind: str,
) -> dict[str, Any] | None:
    from chat.services.expense_copy import normalize_reply_lang
    from chat.services.expense_message_facts import message_meta_for_disambiguation_or_confirm

    return message_meta_for_disambiguation_or_confirm(
        question,
        items=items,
        lang=normalize_reply_lang(lang),
        prompt_kind=prompt_kind,
        message_type="expense_disambiguation",
    )


def handle_active_prompt_turn(
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
    """
    Handle unified active_prompt state. Returns None to fall through when prompt
    cleared and caller should continue normal routing (e.g. add after 'add korbo').
    """
    from chat.services.expense_workflow import _pack

    prompt = read_active_prompt(block)
    if not prompt:
        return None

    if message_abandons_expense_interactive_pending(message, block):
        clear_active_prompt(block)
        return None

    kind = str(prompt.get("kind") or "")
    reply_lang = lang or "banglish"
    view = ExpenseDraftView(items, block)

    if kind == KIND_ADD_MODIFY_CHOICE:
        q, choice, items = handle_add_modify_prompt_turn(
            block=block,
            items=items,
            message=message,
            inc_iso=inc_iso,
            lang=lang,
        )
        if q:
            facts = _message_facts(q, items=items, lang=lang, prompt_kind="add_modify")
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
        if choice == "add":
            block["_add_modify_force_add"] = {
                "category": prompt.get("category"),
                "amount": prompt.get("amount"),
                "from_location": prompt.get("from_location"),
                "to_location": prompt.get("to_location"),
            }
            return None
        if choice == "modify":
            block["_add_modify_force_modify"] = dict(prompt)
            return None
        return None

    if kind == KIND_MODIFY_TARGET:
        q, items = handle_modify_target_turn(
            block=block,
            items=items,
            message=message,
            inc_iso=inc_iso,
            lang=lang,
        )
        if q:
            if read_active_prompt(block):
                facts = _message_facts(q, items=items, lang=lang, prompt_kind="modify_target")
            else:
                facts = None
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
                message=message,
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
        return None

    if kind == KIND_DELETE_PICK:
        from chat.services.expense.delete_flow import (
            apply_multi_delete_lines,
            parse_multi_delete_pick_numbers,
        )

        multi_nums = parse_multi_delete_pick_numbers(message)
        if multi_nums:
            items, block, removed = apply_multi_delete_lines(view, multi_nums)
            clear_active_prompt(block)
            if removed:
                val = validate_expense_items(
                    items,
                    incurred_date_iso=inc_iso,
                    day_logged_total=day_logged_total,
                    daily_cap=daily_cap,
                    message=message,
                )
                labels = ", ".join(
                    f"#{ln.number} {ln.category} — {ln.amount:g} Tk" for ln in removed
                )
                if reply_lang == "en":
                    prefix = f"Removed **{labels}**.\n\n"
                else:
                    prefix = f"মুছে ফেলা হয়েছে — **{labels}**.\n\n"
                from chat.services.expense.draft_summary import format_numbered_draft_summary

                q = prefix + format_numbered_draft_summary(
                    items,
                    block,
                    incurred_date_iso=inc_iso,
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

        pick = resolve_delete_pick(message, view, block=block, lang=reply_lang)
        if pick.prompt:
            if pick.prompt_state:
                state = {k: v for k, v in pick.prompt_state.items() if k != "kind"}
                set_active_prompt(block, KIND_DELETE_PICK, **state)
            facts = _message_facts(
                pick.prompt, items=items, lang=lang, prompt_kind="delete_pick"
            )
            return TurnRouteResult(
                handled=True,
                pack=_pack(
                    wf,
                    block,
                    items=items,
                    question=pick.prompt,
                    inc_iso=inc_iso,
                    message_facts=facts,
                ),
            )
        if pick.line:
            if pick.require_confirm:
                start_delete_confirm(block, pick.line)
                view.apply_items_to_block()
                q = build_delete_confirm_prompt(pick.line, lang=reply_lang)
                facts = _message_facts(
                    q, items=items, lang=lang, prompt_kind="delete_confirm"
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
            items, block, changed = apply_delete_line(view, pick.line)
            clear_active_prompt(block)
            if changed:
                val = validate_expense_items(
                    items,
                    incurred_date_iso=inc_iso,
                    day_logged_total=day_logged_total,
                    daily_cap=daily_cap,
                    message=message,
                )
                q = format_after_delete_summary(
                    items,
                    block,
                    line=pick.line,
                    incurred_date_iso=inc_iso,
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

    if kind == KIND_DELETE_CONFIRM:
        if is_confirmation_no(message):
            start_numbered_delete(block)
            q = build_numbered_delete_prompt(ExpenseDraftView(items, block), lang=reply_lang)
            return TurnRouteResult(
                handled=True,
                pack=_pack(wf, block, items=items, question=q, inc_iso=inc_iso),
            )
        if is_confirmation_yes(message):
            pick_num = int(prompt.get("number") or 0)
            line = view.line_by_number(pick_num) if pick_num else None
            if line is None:
                line_id = str(prompt.get("line_id") or "")
                line = next((ln for ln in view.lines if ln.line_id == line_id), None)
            if line:
                items, block, changed = apply_delete_line(view, line)
                clear_active_prompt(block)
                if changed:
                    val = validate_expense_items(
                        items,
                        incurred_date_iso=inc_iso,
                        day_logged_total=day_logged_total,
                        daily_cap=daily_cap,
                        message=message,
                    )
                    q = format_after_delete_summary(
                        items,
                        block,
                        line=line,
                        incurred_date_iso=inc_iso,
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

    return None
