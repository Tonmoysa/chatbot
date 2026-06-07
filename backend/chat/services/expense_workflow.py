"""
Enterprise conversational expense collection workflow.

Active lock: while expense_request.active, orchestrator routes all turns here
(no generic AI / unrelated intent handling).
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from chat.services.expense_extraction import (
    _route_from_clause_prefix,
    EXPENSE_CATEGORIES,
    ExpenseLineItem,
    _AMOUNT_RE,
    _CATEGORY_TOKEN,
    _looks_like_route_answer,
    _split_clauses,
    extract_expense_items,
    is_travel_category,
    merge_items,
    normalize_category,
    parse_amount_only,
    parse_category_token,
    parse_declared_day_total,
    parse_from_to_locations,
)
from chat.services.expense_incurred_date import (
    expense_submit_date_block_reason,
    infer_expense_incurred_date_iso,
)
import chat.services.expense_incurred_date as expense_incurred_date_mod
from chat.services.expense_copy import (
    ask_category_prompt,
    ask_from_to_prompt,
    ask_more_lines_prompt,
    collect_start_prompt,
    lang_from_block,
    normalize_reply_lang,
    review_confirm_footer,
    review_head,
    submitted_message,
    submit_confirm_prompt,
    total_label,
)
from chat.services.expense.normalization import (
    normalize_expense_items,
    normalize_expense_line,
    normalize_pending_line,
)
from chat.services.expense.slots import (
    SLOT_CATEGORY,
    SLOT_FROM_TO,
    SLOT_MORE_LINES,
    SLOT_REVIEW,
    SLOT_SUBMIT_CONFIRM,
)
from chat.services.expense.conversation_manager import ExpenseConversationManager
from chat.services.expense.clarify import (
    apply_clarification_reply,
    collect_clarification_issues,
    deserialize_clarification_issues,
    format_clarification_prompt,
    serialize_clarification_issues,
)
from chat.services.expense.entity_pipeline import (
    ExpenseEntityPipeline,
    ExpenseExtractionResult,
)
from chat.services.expense.expense_confirm import (
    apply_corrections,
    build_confirmation_question,
    build_correction_failure_notice,
    correction_unclear_notice,
    dedupe_expense_items,
    duplicate_reentry_notice,
    is_confirmation_no,
    is_confirmation_yes,
    looks_like_compound_expense_claim,
    looks_like_duplicate_expense_reentry,
    looks_like_expense_correction,
    review_denial_hints,
    wants_travel_group_remove,
)
from chat.services.expense.expense_ingest_guard import (
    REASON_TRAVEL_REMOVED,
    ingest_lock_notice,
    set_ingest_lock,
    should_block_compound_reingest,
)
from chat.services.expense.expense_draft_snapshots import (
    KEY_RESTORE_PENDING,
    apply_snapshot_to_block,
    clear_restore_pending,
    format_restore_menu,
    is_awaiting_restore_selection,
    parse_restore_selection,
    push_expense_snapshot,
    read_snapshots,
    restore_applied_notice,
    restore_cancel_notice,
    restore_pick_notice,
    restore_unavailable_notice,
    snapshots_for_restore_menu,
    wants_restore_expense_version,
    items_fingerprint,
)
from chat.services.expense.expense_fsm import (
    clone_workflow_state,
    deactivate_expense_session,
    ensure_expense_block_active,
    is_expense_collecting,
    is_expense_in_progress,
    is_expense_paused,
    normalize_expense_stage,
    pause_expense_session,
    read_expense_block,
    resume_expense_session,
    save_expense_last_submission,
    set_expense_stage,
)
from chat.services.expense.slots import STAGE_COLLECTING, STAGE_REVIEW, STAGE_SUBMIT_CONFIRM
from chat.services.expense.workflow_schema import get_expense_workflow_schema
from chat.services.expense_validation import validate_expense_items
from chat.services.translator import resolve_reply_language

__all__ = [
    "process_expense_turn",
    "is_expense_collecting",
    "is_expense_paused",
    "is_expense_in_progress",
    "pause_expense_session",
    "resume_expense_session",
    "save_expense_last_submission",
    "deactivate_expense_session",
    "format_expense_summary",
    "format_expense_submitted_message",
    "build_confirmation_question",
]

# Backward-compat aliases for orchestrator / turn_classifier imports.
_is_confirmation_yes = is_confirmation_yes
_is_confirmation_no = is_confirmation_no


def expense_pending_prompt(workflow_state: dict[str, Any] | None) -> str | None:
    """Current wizard question for resume-after-interrupt (e.g. policy lookup)."""
    if not is_expense_in_progress(workflow_state):
        return None
    block = _block(workflow_state)
    items = list(block.get("items") or [])
    schema = get_expense_workflow_schema()
    primary = schema.primary_slot(block, items)
    if not primary:
        return collect_start_prompt(lang_from_block(block))
    return _build_wizard_question(
        block,
        items,
        primary_slot=primary,
        lang=lang_from_block(block),
    )[0]


def _block(workflow_state: dict[str, Any]) -> dict[str, Any]:
    return read_expense_block(workflow_state)


_FINISH_COLLECT_RE = re.compile(
    r"^(?:"
    r"no\s+more|nothing\s+more|that'?s\s+all|done|finish|শেষ|আর\s*নাই|"
    r"আর\s*কিছু\s*নাই|না\s*আর|bas|শুধু\s*এটুকু"
    r")\s*\.?$",
    re.I,
)


def _wants_finish_collecting(message: str) -> bool:
    from chat.services.expense.command_parser import parse_wizard_flow_plan

    plan = parse_wizard_flow_plan(message)
    if plan.finish_collecting:
        return True
    t = (message or "").strip()
    if _FINISH_COLLECT_RE.match(t):
        return True
    return bool(re.search(r"\b(শেষ|আর\s*নেই|no\s+more)\b", t, re.I))


_EXPENSE_RECAP_GIVE_RE = re.compile(
    r"(bolo|দেখ|দেখাও|বল|dekhao|dekha|daw|dao|দাও|দিন|den|show|tell|give|list)\b",
    re.I,
)
_EXPENSE_RECAP_KIND_RE = re.compile(
    r"(summery|summary|সারাংশ|পর্যালোচনা|মোট|total|recap|"
    r"list|lists|লিস্ট|breakdown|overview|lines|line\s*items|details|detail)",
    re.I,
)


_RESUME_EXPENSE_NAV_RE = re.compile(
    r"(?:ekhon|এখন|now).{0,30}(?:expense|খরচ|exepense).{0,30}"
    r"(?:asho|as[o]|back|ferot|resume|continue|চালু|আসো|আস)|"
    r"(?:expense|খরচ|exepense).{0,25}(?:e\s+)?(?:asho|as[o]|back|ferot|resume|continue|চালু)"
    r"(?:\s*(?:koro|kor|কর|দাও|dao|daw))?|"
    r"(?:ager|আগের|previous|pending).{0,30}(?:expense|খরচ).{0,30}"
    r"(?:daw|dao|দাও|দেখ|show|dekhao|bolo|বল)|"
    r"(?:^|\b)(?:expense|খরচ|exepense)\s+e\s+(?:back|asho|as[o]|ferot|return|resume)"
    r"(?:\s*(?:koro|kor|কর|দাও|dao|daw))?",
    re.I | re.UNICODE,
)


def wants_resume_or_show_expense(message: str) -> bool:
    """Navigate back to an in-progress expense draft (not a new line item)."""
    from chat.services.leave_confirm import (
        wants_defer_expense_for_leave_submit,
        wants_defer_leave_for_expense_submit,
    )

    t = (message or "").strip()
    if not t:
        return False
    if wants_restore_expense_version(t):
        return False
    if wants_defer_expense_for_leave_submit(t) or wants_defer_leave_for_expense_submit(t):
        return False
    if _RESUME_EXPENSE_NAV_RE.search(t):
        return True
    low = t.lower()
    if re.search(r"\b(ager|আগের|previous)\b", low) and re.search(
        r"\b(expense|খরচ|exepense)\b", low
    ) and re.search(r"\b(daw|dao|দাও|দেখ|show|dekhao|bolo|বল|amake)\b", low, re.I):
        return True
    if re.search(r"\b(expense|খরচ|exepense)\b", low) and re.search(
        r"\b(again|abar|আবার)\b", low, re.I
    ) and re.search(r"\b(daw|dao|দাও|amake|আমাকে|show|dekhao|bolo|বল)\b", low, re.I):
        return True
    if re.search(r"\b(again|abar|আবার)\b", low, re.I) and re.search(
        r"\b(expense|খরচ|exepense)\b", low
    ):
        return True
    if re.search(r"\b(expense|খরচ|exepense)\b", low) and re.search(
        r"\b(amake|আমাকে|mke)\b", low, re.I
    ) and re.search(r"\b(daw|dao|দাও|bolo|বল|dekhao|দেখ)\b", low, re.I):
        if re.search(r"\b(policy|status|rules|নিয়ম|track)\b", low, re.I):
            return False
        if re.search(
            r"\b(information|info|tothyo|তথ্য|dilam|দিয়েছিলাম|cherechilam|"
            r"থেমেছিল|seta|সেটা|that|ager|age|আগে|previous|pending)\b",
            low,
            re.I,
        ) and not re.search(
            r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?(?!\d)", t
        ):
            return True
    return bool(
        re.search(r"\b(expense|খরচ|exepense)\b", low)
        and re.search(
            r"\b(back|resume|continue|asho|as[o]?|ferot|চালু|আসো|আস)\b", low, re.I
        )
    )


def format_expense_resume_message(
    workflow_state: dict[str, Any], *, user_message: str = ""
) -> str | None:
    """Intro + the exact pending expense prompt (category / route / review)."""
    resume = expense_pending_prompt(workflow_state)
    if not resume:
        return None
    lang = lang_from_block(_block(workflow_state))
    if user_message:
        lang = resolve_reply_language(user_message, lang)
    if lang == "en":
        intro = (
            "Your expense claim is **not submitted yet**. "
            "Here is where you left off:\n\n"
        )
    elif lang == "banglish":
        intro = (
            "Apnar expense claim ekhono **submit hoyni**. "
            "Jekhane cherechilen:\n\n"
        )
    else:
        intro = (
            "আপনার খরচের আবেদন এখনো **জমা হয়নি**। "
            "যেখানে থেমেছিলেন:\n\n"
        )
    return intro + resume


def _restore_menu_choices(
    wf: dict[str, Any], items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    cur_fp = items_fingerprint(items)
    return [
        s
        for s in snapshots_for_restore_menu(wf, items)
        if str(s.get("fingerprint") or "") != cur_fp
    ]


def _try_handle_total_check_turn(
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    *,
    lang: str | None,
    inc_iso: str,
) -> dict[str, Any] | None:
    from chat.services.expense.expense_total_dispute import (
        format_expense_total_check_message,
        is_expense_total_check_query,
    )
    from chat.services.expense.session_action_memory import record_expense_total_check

    if not is_expense_total_check_query(message):
        return None
    body = format_expense_total_check_message(
        wf,
        incurred_date_iso=inc_iso,
        lang=lang,
        user_message=message,
    )
    if not body:
        return None
    total = sum(float(x.get("amount") or 0) for x in items)
    wf = record_expense_total_check(
        wf,
        total=total,
        line_count=len(items),
        stage=str(block.get("stage") or "review"),
    )
    return _pack(
        wf,
        block,
        items=items,
        question=body,
        inc_iso=inc_iso,
    )


def _try_handle_restore_turn(
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    message: str,
    *,
    lang: str | None,
    inc_iso: str,
    day_logged_total: float,
    daily_cap: float,
) -> dict[str, Any] | None:
    """Snapshot restore menu + apply selected version."""
    stage = normalize_expense_stage(str(block.get("stage") or STAGE_COLLECTING))

    def _summary_pack(
        body: str,
        restored_items: list[dict[str, Any]],
        *,
        warnings: list | None = None,
        line_flags: list | None = None,
    ) -> dict[str, Any]:
        set_expense_stage(block, STAGE_REVIEW)
        tail = format_expense_summary(
            restored_items,
            incurred_date_iso=inc_iso,
            warnings=warnings or [],
            line_flags=line_flags or [],
            lang=lang,
        )
        return _pack(
            wf,
            block,
            items=restored_items,
            question=f"{body}\n\n{tail}",
            warnings=warnings or [],
            inc_iso=inc_iso,
        )

    if is_awaiting_restore_selection(block):
        choices = _restore_menu_choices(wf, items)
        pick = parse_restore_selection(
            message,
            choices,
            current_fingerprint=items_fingerprint(items),
        )
        if pick == -1:
            clear_restore_pending(block)
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
            )
            return _summary_pack(restore_cancel_notice(lang=lang), items, warnings=val.warnings, line_flags=val.line_flags)
        if pick is not None and 1 <= pick <= len(choices):
            snap = choices[pick - 1]
            restored = apply_snapshot_to_block(block, snap)
            clear_restore_pending(block)
            wf = push_expense_snapshot(
                wf,
                items=restored,
                stage=str(block.get("stage") or STAGE_REVIEW),
                action_type="after_restore",
                incurred_date_iso=inc_iso,
                lang=lang,
            )
            from chat.services.expense.session_action_memory import record_expense_corrected

            wf = record_expense_corrected(
                wf,
                items=restored,
                incurred_date_iso=inc_iso,
                stage="review",
            )
            val = validate_expense_items(
                restored,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
            )
            block["review_line_flags"] = val.line_flags
            return _summary_pack(
                restore_applied_notice(snap, lang=lang),
                restored,
                warnings=val.warnings,
                line_flags=val.line_flags,
            )
        if wants_restore_expense_version(message):
            choices = _restore_menu_choices(wf, items)
            if choices:
                block[KEY_RESTORE_PENDING] = True
                menu = format_restore_menu(choices, lang=lang, current_items=items)
                return _pack(wf, block, items=items, question=menu, inc_iso=inc_iso)
        menu = format_restore_menu(choices, lang=lang, current_items=items) if choices else ""
        hint = restore_pick_notice(lang=lang)
        q = f"{hint}\n\n{menu}" if menu else hint
        return _pack(wf, block, items=items, question=q, inc_iso=inc_iso)

    if wants_restore_expense_version(message):
        wf = push_expense_snapshot(
            wf,
            items=items,
            stage=stage,
            action_type="current_before_restore",
            incurred_date_iso=inc_iso,
            lang=lang,
        )
        choices = _restore_menu_choices(wf, items)
        if not choices:
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
            )
            body = restore_unavailable_notice(lang=lang)
            if items:
                body += "\n\n" + format_expense_summary(
                    items,
                    incurred_date_iso=inc_iso,
                    warnings=val.warnings,
                    line_flags=val.line_flags,
                    lang=lang,
                )
            return _pack(wf, block, items=items, question=body, warnings=val.warnings, inc_iso=inc_iso)
        block[KEY_RESTORE_PENDING] = True
        menu = format_restore_menu(choices, lang=lang, current_items=items)
        return _pack(wf, block, items=items, question=menu, inc_iso=inc_iso)

    return None


def wants_expense_summary(message: str) -> bool:
    """User wants to see expense review / day recap (not a new claim line)."""
    if _wants_finish_collecting(message):
        return True
    raw = message or ""
    low = raw.lower().strip()
    if re.search(
        r"(expense|খরচ|খরচের).{0,40}(summery|summary|সারাংশ|পর্যালোচনা|মোট|total|recap|"
        r"list|lists|লিস্ট|breakdown|lines)",
        low,
    ):
        return True
    if re.search(
        r"(summery|summary|সারাংশ|পর্যালোচনা|list|lists|লিস্ট|breakdown).{0,30}"
        r"(ta\s*)?(bolo|দেখ|দেখাও|বল|dekhao|dekha|daw|dao|দাও|দাও)",
        low,
    ):
        return True
    if re.search(
        r"(list|lists|লিস্ট).{0,30}(ta\s*)?(bolo|দেখ|দেখাও|বল|dekhao|dekha|daw|dao|দাও|দাও)",
        low,
    ):
        return True
    if re.search(r"(ekhon|এখন|now).{0,20}(summery|summary|সারাংশ|list|লিস্ট)", low):
        return True
    if re.search(
        r"(ajke|ajker|today|আজকে|আজকের).{0,35}(expense|খরচ).{0,35}"
        r"(list|summery|summary|লিস্ট|সারাংশ|breakdown)",
        low,
    ) or re.search(
        r"(ajke|ajker|today|আজকে|আজকের).{0,35}(expense|খরচ).{0,35}"
        r"(list|summery|summary|লিস্ট|সারাংশ|breakdown)",
        raw,
        re.I,
    ):
        return True
    if re.search(r"\b(ager|আগের|previous)\b", low) and re.search(
        r"\b(expense|খরচ)\b", low
    ) and _EXPENSE_RECAP_GIVE_RE.search(low):
        return True
    if _EXPENSE_RECAP_KIND_RE.search(raw) and _EXPENSE_RECAP_GIVE_RE.search(low):
        if re.search(r"\b(expense|খরচ|reimbursement|claim)\b", low) or re.search(
            r"(খরচ|expense)", raw, re.I
        ):
            return True
    return bool(
        re.search(
            r"\b(expense\s+summary|expense\s+list|খরচের\s+সারাংশ|খরচের\s+লিস্ট|দৈনিক\s+খরচ)\b",
            low,
        )
    )


def _category_options_text() -> str:
    return ", ".join(EXPENSE_CATEGORIES)


def _build_wizard_question(
    block: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    primary_slot: str,
    lang: str | None = None,
    pending_line: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Natural follow-up with acknowledgment of collected lines and date."""
    reply_lang = normalize_reply_lang(lang or lang_from_block(block))
    schema = get_expense_workflow_schema()
    missing = schema.missing_fields(block, items)
    mgr = ExpenseConversationManager()
    ack, ask, meta = mgr.compose_follow_up_parts(
        block,
        items,
        primary_slot=primary_slot,
        missing=missing,
        lang=reply_lang,
        pending_line=pending_line,
        incurred_date_iso=str(block.get("incurred_date_iso") or ""),
        warnings=list(block.get("warnings") or []),
    )
    if ack and ask:
        return f"{ack}{ask}", meta
    if ask:
        return ask, meta
    return collect_start_prompt(reply_lang), meta


def _ask_category_prompt(
    block: dict[str, Any],
    items: list[dict[str, Any]],
    amount: float,
    lang: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    pending = dict(block.get("pending_line") or {})
    pending["amount"] = amount
    return _build_wizard_question(
        block,
        items,
        primary_slot=SLOT_CATEGORY,
        lang=lang,
        pending_line=pending,
    )


def _ask_from_to_prompt(
    block: dict[str, Any],
    items: list[dict[str, Any]],
    category: str,
    amount: float,
    lang: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    pending = dict(block.get("pending_line") or {})
    pending["category"] = category
    pending["amount"] = amount
    return _build_wizard_question(
        block,
        items,
        primary_slot=SLOT_FROM_TO,
        lang=lang,
        pending_line=pending,
    )


def _ask_more_lines_prompt(
    block: dict[str, Any],
    items: list[dict[str, Any]],
    lang: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    return _build_wizard_question(
        block,
        items,
        primary_slot=SLOT_MORE_LINES,
        lang=lang,
    )


def _extract_lines_from_message(
    message: str,
    *,
    use_llm: bool = True,
    pipeline_result: ExpenseExtractionResult | None = None,
) -> Any:
    """Hybrid parser + optional LLM line extraction for workflow ingestion."""
    return ExpenseEntityPipeline().extract_lines(
        message,
        use_llm=use_llm,
        preloaded=pipeline_result,
    )


def _should_reset_pending_for_message(
    message: str, *, pending_step: str = ""
) -> bool:
    """New multi-line input supersedes a single pending From/To question."""
    if wants_resume_or_show_expense(message):
        return False
    if (pending_step or "").strip().lower() == "from_to" and _looks_like_route_answer(
        message
    ):
        return False
    if (pending_step or "").strip().lower() == "clarify":
        return False
    text = (message or "").strip()
    if len(_split_clauses(text)) > 1:
        return True
    if len(list(_AMOUNT_RE.finditer(text))) >= 2:
        return True
    ext = extract_expense_items(text)
    if len(ext.items) + len(ext.malformed) > 1:
        return True
    return False


def _pending_from_clause(clause: str) -> dict[str, Any] | None:
    """Build a pending line from an uncategorized clause (amount ± route)."""
    text = (clause or "").strip()
    if not text:
        return None
    amt = parse_amount_only(text)
    if amt is None:
        return None
    pair = parse_from_to_locations(text)
    return {
        "amount": float(amt),
        "category": "",
        "from_location": pair[0] if pair else "",
        "to_location": pair[1] if pair else "",
        "source_clause": text,
    }


def _resolve_pending_route(pending: dict[str, Any]) -> tuple[str, str]:
    frm = str(pending.get("from_location") or "").strip()
    to = str(pending.get("to_location") or "").strip()
    if frm and to:
        return frm, to
    src = str(pending.get("source_clause") or "").strip()
    if src:
        pair = parse_from_to_locations(src)
        if pair:
            return pair
    return "", ""


def _queue_pending_amount(block: dict[str, Any], entry: dict[str, Any]) -> None:
    if block.get("pending_line") and block["pending_line"].get("amount"):
        queue = list(block.get("pending_queue") or [])
        queue.append(entry)
        block["pending_queue"] = queue
    else:
        block["pending_line"] = entry
        block["pending_step"] = "category"
        set_expense_stage(block, STAGE_COLLECTING)


def _pending_entries_list(block: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    pending = block.get("pending_line")
    if isinstance(pending, dict) and pending.get("amount"):
        entries.append(dict(pending))
    for row in block.get("pending_queue") or []:
        if isinstance(row, dict) and row.get("amount"):
            entries.append(dict(row))
    return entries


def _store_pending_entries(block: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    if not entries:
        block.pop("pending_line", None)
        block.pop("pending_queue", None)
        return
    block["pending_line"] = dict(entries[0])
    block["pending_queue"] = [dict(x) for x in entries[1:]]


def _start_clarification_turn(
    block: dict[str, Any],
    items: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
    *,
    lang: str | None,
) -> str:
    issues = collect_clarification_issues(items, pending_entries)
    block["clarification_issues"] = serialize_clarification_issues(issues)
    block["pending_step"] = "clarify"
    _store_pending_entries(block, pending_entries)
    set_expense_stage(block, STAGE_COLLECTING)
    return format_clarification_prompt(issues, lang=lang)


def _try_clarify_before_review(
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
    *,
    inc_iso: str,
    lang: str | None,
) -> dict[str, Any] | None:
    issues = collect_clarification_issues(items, pending_entries)
    if not issues:
        return None
    question = _start_clarification_turn(
        block, items, pending_entries, lang=lang
    )
    return _pack(wf, block, items=items, question=question, inc_iso=inc_iso)


def _advance_pending_queue(
    block: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    inc_iso: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """After finishing one pending line, start the next queued amount if any."""
    lang = lang_from_block(block)
    queue = list(block.get("pending_queue") or [])
    if not queue:
        block.pop("pending_queue", None)
        return items, _ask_more_lines_prompt(block, items, lang=lang)[0]
    nxt = queue.pop(0)
    block["pending_queue"] = queue
    block["pending_line"] = nxt
    set_expense_stage(block, STAGE_COLLECTING)
    cat = str(nxt.get("category") or "").strip()
    amt = float(nxt.get("amount") or 0)
    frm = str(nxt.get("from_location") or "").strip()
    to = str(nxt.get("to_location") or "").strip()
    if cat and is_travel_category(cat) and (not frm or not to):
        block["pending_step"] = "from_to"
        return items, _ask_from_to_prompt(block, items, cat, amt, lang=lang)[0]
    block["pending_step"] = "category"
    return items, _ask_category_prompt(block, items, amt, lang=lang)[0]


def _ingest_extracted_lines(
    block: dict[str, Any],
    items: list[dict[str, Any]],
    ext: Any,
    *,
    inc_iso: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """
    Merge parsed lines; uncategorized amounts become pending category (never Other).
    Returns (items, question_if_blocked_on_pending).
    """
    out = list(items)
    lang = lang_from_block(block)
    needs_route: list[ExpenseLineItem] = []
    uncategorized: list[ExpenseLineItem] = []

    for ni in ext.items:
        d = normalize_expense_line(ni.to_dict())
        cat = str(d.get("category") or "").strip()
        if not cat:
            uncategorized.append(ni)
            continue
        if is_travel_category(d["category"]) and (
            not str(d.get("from_location") or "").strip()
            or not str(d.get("to_location") or "").strip()
        ):
            amt_key = round(float(d.get("amount") or 0), 2)
            already_routed = any(
                str(r.get("category") or "").lower() == cat.lower()
                and round(float(r.get("amount") or 0), 2) == amt_key
                and str(r.get("from_location") or "").strip()
                and str(r.get("to_location") or "").strip()
                for r in out
            )
            if not already_routed:
                needs_route.append(ni)
            continue
        key = (cat.lower(), round(float(d.get("amount") or 0), 2))
        if any(
            (str(r.get("category") or "").lower(), round(float(r.get("amount") or 0), 2))
            == key
            for r in out
        ):
            continue
        out.append(d)

    pending_entries: list[dict[str, Any]] = []
    for ni in uncategorized:
        pending_entries.append(
            {
                "amount": float(ni.amount),
                "category": "",
                "from_location": ni.from_location or "",
                "to_location": ni.to_location or "",
                "source_clause": str(getattr(ni, "notes", "") or ""),
            }
        )
    for clause in ext.malformed:
        entry = _pending_from_clause(clause)
        if entry:
            pending_entries.append(entry)

    if needs_route:
        first = needs_route[0]
        route_queue: list[dict[str, Any]] = []
        for extra in needs_route[1:]:
            route_queue.append(
                {
                    "amount": float(extra.amount),
                    "category": extra.category,
                    "from_location": extra.from_location or "",
                    "to_location": extra.to_location or "",
                    "source_clause": "",
                }
            )
        block["pending_line"] = {
            "amount": first.amount,
            "category": first.category,
            "from_location": first.from_location or "",
            "to_location": first.to_location or "",
            "source_clause": "",
        }
        block["pending_step"] = "from_to"
        set_expense_stage(block, STAGE_COLLECTING)
        block["pending_queue"] = route_queue + pending_entries
        return out, _ask_from_to_prompt(
            block, out, first.category, float(first.amount), lang=lang
        )[0]

    if pending_entries:
        clarify = _try_clarify_before_review(
            wf={"expense_request": block},
            block=block,
            items=out,
            pending_entries=[dict(x) for x in pending_entries],
            inc_iso=inc_iso,
            lang=lang,
        )
        if clarify:
            return out, clarify["question"]

        block["pending_line"] = pending_entries[0]
        block["pending_queue"] = pending_entries[1:]
        block["pending_step"] = "category"
        set_expense_stage(block, STAGE_COLLECTING)
        return out, _ask_category_prompt(
            block, out, float(pending_entries[0]["amount"]), lang=lang
        )[0]

    return dedupe_expense_items(out), None


def _finalize_pending_line(
    pending: dict[str, Any],
) -> dict[str, Any] | None:
    schema = get_expense_workflow_schema()
    cleaned = normalize_pending_line(pending)
    if not schema.can_finalize_pending(cleaned):
        return None
    line = normalize_expense_line(
        {
            "category": cleaned.get("category"),
            "amount": cleaned.get("amount"),
            "from_location": cleaned.get("from_location"),
            "to_location": cleaned.get("to_location"),
            "notes": cleaned.get("notes"),
        }
    )
    return line


def _format_line_display(row: dict[str, Any], *, inline_flags: list[str] | None = None) -> str:
    cat = str(row.get("category") or "").strip() or "Category লাগবে"
    amt = float(row.get("amount") or 0)
    frm = str(row.get("from_location") or "").strip()
    to = str(row.get("to_location") or "").strip()
    if frm and to:
        route = f"{frm} → {to}"
    elif frm or to:
        route = frm or to
    else:
        route = "—"
    line = f"- **{cat}** · {route} · **{amt:g} Tk**"
    if inline_flags:
        line += " " + " ".join(inline_flags)
    return line


def format_expense_day_summary_readonly(
    items: list[dict[str, Any]],
    *,
    incurred_date_iso: str = "",
    reference_id: str = "",
) -> str:
    """Submitted / logged expenses for a date — read-only (no wizard confirmation)."""
    if not items:
        date_hint = incurred_date_iso or "আজ"
        return (
            f"**{date_hint}** তারিখে কোনো expense জমা পাওয়া যায়নি।\n\n"
            "নতুন খরচ জমা দিতে লিখুন, যেমন: `lunch 100, bus 50 office to badda`"
        )
    total = sum(float(r.get("amount") or 0) for r in items)
    head = (
        "**দৈনিক খরচ — সারাংশ**"
        if not incurred_date_iso
        else f"**দৈনিক খরচ — সারাংশ** ({incurred_date_iso})"
    )
    body = "\n".join(_format_line_display(r) for r in items)
    lines = [head, "", body, "", f"**মোট: {total:g} Tk**"]
    if reference_id:
        lines.extend(["", f"**রেফারেন্স:** `{reference_id}`"])
    return "\n".join(lines)


def format_expense_summary(
    items: list[dict[str, Any]],
    *,
    incurred_date_iso: str = "",
    warnings: list[str] | None = None,
    line_flags: dict[int, list[str]] | None = None,
    lang: str | None = None,
) -> str:
    reply_lang = normalize_reply_lang(lang)
    total = sum(float(r.get("amount") or 0) for r in items)
    head = review_head(incurred_date_iso, reply_lang)
    flags = line_flags or {}
    body = "\n".join(
        _format_line_display(r, inline_flags=flags.get(idx))
        for idx, r in enumerate(items)
    )
    warn = ""
    if warnings:
        warn = "\n\n" + "\n".join(f"⚠ {w}" for w in warnings)
    return (
        f"{head}\n\n{body}\n\n**{total_label(reply_lang)}: {total:g} Tk**{warn}\n\n"
        + review_confirm_footer(reply_lang)
    )


def format_submit_confirm_prompt(lang: str | None = None) -> str:
    return submit_confirm_prompt(normalize_reply_lang(lang))


def format_expense_submitted_message(
    *,
    items: list[dict[str, Any]],
    reference_id: str,
    incurred_date_iso: str = "",
    lang: str | None = None,
) -> str:
    total = sum(float(r.get("amount") or 0) for r in items)
    return submitted_message(
        item_count=len(items),
        total=total,
        incurred_date_iso=incurred_date_iso,
        reference_id=reference_id,
        lang=normalize_reply_lang(lang),
    )


def _apply_corrections(
    items: list[dict[str, Any]],
    message: str,
    *,
    review_mode: bool = False,
) -> tuple[list[dict[str, Any]], bool]:
    from chat.services.expense.command_executor import apply_message_corrections

    extract = (
        None
        if review_mode
        else lambda m: _extract_lines_from_message(m, use_llm=False)
    )
    result = apply_message_corrections(items, message, extract_lines=extract)
    return result.items, result.changed


def _apply_review_corrections(
    items: list[dict[str, Any]],
    message: str,
    *,
    trace_id: str = "",
) -> tuple[list[dict[str, Any]], bool]:
    """Review-stage corrections via typed command plan (Phase 2 / 2.5)."""
    from chat.services.expense.command_executor import (
        apply_message_corrections,
    )

    result = apply_message_corrections(
        items,
        message,
        extract_lines=None,
        trace_id=trace_id,
        use_llm=True,
        review_stage=True,
    )
    return result.items, result.changed


def _pack(
    wf: dict[str, Any],
    block: dict[str, Any],
    *,
    items: list[dict[str, Any]],
    question: str | None,
    complete: bool = False,
    submitted: bool = False,
    warnings: list[str] | None = None,
    inc_iso: str = "",
    validation_blocked: bool = False,
    message_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    block["items"] = items
    wf["expense_request"] = block
    if message_facts is None and question:
        from chat.services.expense_message_facts import message_meta_from_block

        message_facts = message_meta_from_block(
            block,
            items,
            question,
            incurred_date_iso=inc_iso,
            warnings=warnings,
            validation_blocked=validation_blocked,
        )
    pack: dict[str, Any] = {
        "workflow_state": wf,
        "complete": complete,
        "submitted": submitted,
        "question": question,
        "items": items,
        "warnings": list(warnings or []),
        "incurred_date_iso": inc_iso,
        "validation_blocked": validation_blocked,
        "crm_payload": list(items),
    }
    if message_facts:
        pack["message_facts"] = message_facts
    return pack


def _has_pending_expense_line(block: dict[str, Any]) -> bool:
    return get_expense_workflow_schema().has_pending_line(block)


def _pending_finish_block_message(block: dict[str, Any], *, lang: str) -> str:
    pending = block.get("pending_line") if isinstance(block.get("pending_line"), dict) else {}
    lines: list[str] = []
    if pending.get("amount"):
        cat = str(pending.get("category") or "line").strip()
        lines.append(f"**{cat}** — **{float(pending.get('amount') or 0):g} Tk**")
    for qrow in list(block.get("pending_queue") or []):
        cat = str(qrow.get("category") or "").strip()
        try:
            amt = float(qrow.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if cat and amt > 0:
            lines.append(f"**{cat}** — **{amt:g} Tk**")
    joined = "; ".join(lines) if lines else f"**{float(pending.get('amount') or 0):g} Tk**"
    if lang == "en":
        return (
            f"Cannot submit yet — finish pending lines first: {joined}.\n"
            "Add **from/to** for travel lines (e.g. `office to badda`), then **joma daw** again."
        )
    if lang == "banglish":
        return (
            f"Ekhono submit hobe na — age pending line gulo shesh korun: {joined}.\n"
            "Travel line er **from/to** din (e.g. `office to badda`), tarpor abar **joma daw**."
        )
    return (
        f"এখনো জমা দেওয়া যাবে না — আগে pending লাইন শেষ করুন: {joined}.\n"
        "Travel খরচে **from/to** লিখুন (যেমন `office to badda`), তারপর আবার **জমা দিন**।"
    )


def _try_advance_to_review(
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    inc_iso: str,
    day_logged_total: float,
    daily_cap: float,
    message: str = "",
) -> dict[str, Any] | None:
    if _has_pending_expense_line(block):
        return None

    items = dedupe_expense_items(items)
    lang = lang_from_block(block)
    pending_entries = _pending_entries_list(block)
    if str(block.get("pending_step") or "") != "clarify":
        issues = collect_clarification_issues(items, pending_entries)
        if issues:
            clarify = _try_clarify_before_review(
                wf,
                block,
                items,
                pending_entries,
                inc_iso=inc_iso,
                lang=lang,
            )
            if clarify:
                return clarify

    val = validate_expense_items(
        items,
        incurred_date_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
        message=message,
        apply_location_fixes=False,
    )
    if not val.ok:
        set_expense_stage(block, STAGE_COLLECTING)
        block.pop("pending_line", None)
        block.pop("pending_step", None)
        return _pack(
            wf,
            block,
            items=items,
            question=val.blocking_message,
            warnings=val.warnings,
            inc_iso=inc_iso,
            validation_blocked=not bool(items),
        )
    set_expense_stage(block, STAGE_REVIEW)
    block["warnings"] = val.warnings
    block["review_line_flags"] = val.line_flags
    block.pop("pending_line", None)
    block.pop("pending_step", None)
    block.pop("clarification_issues", None)
    wf = push_expense_snapshot(
        wf,
        items=items,
        stage=STAGE_REVIEW,
        action_type="initial_review",
        incurred_date_iso=inc_iso,
        lang=lang,
    )
    return _pack(
        wf,
        block,
        items=items,
        question=format_expense_summary(
            items,
            incurred_date_iso=inc_iso,
            warnings=val.warnings,
            line_flags=val.line_flags,
            lang=lang,
        ),
        warnings=val.warnings,
        inc_iso=inc_iso,
    )


def _handle_pending_line(
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    pending: dict[str, Any],
    message: str,
    *,
    inc_iso: str,
    day_logged_total: float,
    daily_cap: float,
) -> dict[str, Any]:
    step = str(block.get("pending_step") or "category")
    amt = float(pending.get("amount") or 0)
    lang = lang_from_block(block)

    if _wants_finish_collecting(message) and _has_pending_expense_line(block):
        return _pack(
            wf,
            block,
            items=items,
            question=_pending_finish_block_message(block, lang=lang),
            inc_iso=inc_iso,
        )

    if step == "clarify":
        issues = deserialize_clarification_issues(block.get("clarification_issues"))
        pending_entries = _pending_entries_list(block)
        items, pending_entries, unresolved = apply_clarification_reply(
            message, items, issues, pending_entries
        )
        finalized: list[dict[str, Any]] = []
        remaining_pending: list[dict[str, Any]] = []
        for entry in pending_entries:
            if str(entry.get("category") or "").strip():
                row = _finalize_pending_line(entry)
                if row:
                    finalized.append(row)
                else:
                    remaining_pending.append(entry)
            else:
                remaining_pending.append(entry)
        for idx, row in enumerate(items):
            if str(row.get("category") or "").strip():
                continue
            amt = float(row.get("amount") or 0)
            if amt <= 0:
                continue
            remaining_pending.append(
                {
                    "amount": amt,
                    "category": "",
                    "from_location": row.get("from_location") or "",
                    "to_location": row.get("to_location") or "",
                    "source_clause": "",
                }
            )
        items = [row for row in items if str(row.get("category") or "").strip()]
        items.extend(finalized)
        block.pop("clarification_issues", None)
        block.pop("pending_step", None)
        _store_pending_entries(block, remaining_pending)

        if unresolved or remaining_pending:
            new_issues = collect_clarification_issues(items, remaining_pending)
            if new_issues:
                question = _start_clarification_turn(
                    block, items, remaining_pending, lang=lang
                )
                return _pack(
                    wf, block, items=items, question=question, inc_iso=inc_iso
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
            return adv
        q, facts = _ask_more_lines_prompt(block, items, lang=lang)
        return _pack(
            wf, block, items=items, question=q, inc_iso=inc_iso, message_facts=facts
        )

    if step == "category":
        if wants_resume_or_show_expense(message):
            resume_msg = format_expense_resume_message(
                {"expense_request": block}, user_message=message
            )
            if resume_msg:
                return _pack(
                    wf,
                    block,
                    items=items,
                    question=resume_msg,
                    inc_iso=inc_iso,
                )
        cat = parse_category_token(message)
        if not cat:
            return _pack(
                wf,
                block,
                items=items,
                question=(
                    "খরচের ধরন বুঝতে পারিনি।\n"
                    f"লিখুন: {_category_options_text()}"
                ),
                inc_iso=inc_iso,
            )
        pending["category"] = cat
        if is_travel_category(cat):
            frm, to = _resolve_pending_route(pending)
            if frm and to:
                pending["from_location"], pending["to_location"] = frm, to
                row = _finalize_pending_line(pending)
                if row:
                    items.append(row)
                block.pop("pending_line", None)
                block.pop("pending_step", None)
                items, q = _advance_pending_queue(block, items, inc_iso=inc_iso)
                return _pack(wf, block, items=items, question=q, inc_iso=inc_iso)
            block["pending_line"] = pending
            block["pending_step"] = "from_to"
            set_expense_stage(block, STAGE_COLLECTING)
            q, facts = _ask_from_to_prompt(block, items, cat, amt, lang=lang)
            return _pack(
                wf,
                block,
                items=items,
                question=q,
                inc_iso=inc_iso,
                message_facts=facts,
            )
        row = _finalize_pending_line(pending)
        if row:
            items.append(row)
        block.pop("pending_line", None)
        block.pop("pending_step", None)
        items, q = _advance_pending_queue(block, items, inc_iso=inc_iso)
        return _pack(
            wf,
            block,
            items=items,
            question=q,
            inc_iso=inc_iso,
        )

    if step == "from_to":
        cat = str(pending.get("category") or "Bus")
        pair = parse_from_to_locations(message)
        if not pair and is_travel_category(cat):
            pair = _route_from_clause_prefix(message, cat)
        if not pair:
            pair_raw = _resolve_pending_route(pending)
            if pair_raw[0] and pair_raw[1]:
                pair = pair_raw
        if not pair:
            q, facts = _ask_from_to_prompt(block, items, cat, amt, lang=lang)
            return _pack(
                wf,
                block,
                items=items,
                question=q,
                inc_iso=inc_iso,
                message_facts=facts,
            )
        pending["from_location"], pending["to_location"] = pair
        row = _finalize_pending_line(pending)
        if row:
            items.append(row)
        block.pop("pending_line", None)
        block.pop("pending_step", None)
        items, q = _advance_pending_queue(block, items, inc_iso=inc_iso)
        return _pack(
            wf,
            block,
            items=items,
            question=q,
            inc_iso=inc_iso,
        )

    block.pop("pending_line", None)
    block.pop("pending_step", None)
    q, facts = _ask_more_lines_prompt(block, items, lang=lang)
    return _pack(
        wf,
        block,
        items=items,
        question=q,
        inc_iso=inc_iso,
        message_facts=facts,
    )


def _unallocated_total_prompt(
    message: str, items: list[dict[str, Any]], *, lang: str | None
) -> str | None:
    declared = parse_declared_day_total(message)
    if declared is None:
        return None
    line_sum = sum(float(r.get("amount") or 0) for r in items)
    gap = round(declared - line_sum, 2)
    if gap < 0.5:
        return None
    if lang == "en":
        return (
            f"You mentioned **{declared:g} Tk** total; lines so far add up to **{line_sum:g} Tk**. "
            f"What was the other **{gap:g} Tk** for? (e.g. bus 50 office to home), or say **done**."
        )
    return (
        f"আপনি মোট **{declared:g} Tk** বলেছেন; এখন পর্যন্ত লাইনে **{line_sum:g} Tk**। "
        f"বাকি **{gap:g} Tk** কী ছিল? (যেমন: bus 50 office to home), না হলে **শেষ** লিখুন।"
    )


def _sync_reply_language(block: dict[str, Any], message: str) -> None:
    block["reply_language"] = resolve_reply_language(
        message, block.get("reply_language")
    )


def process_expense_turn(
    *,
    workflow_state: dict[str, Any],
    message: str,
    company_id: str = "",
    employee_id: str = "",
    session_id: str = "",
    trace_id: str = "",
    day_logged_total: float = 0.0,
    daily_cap: float = 300.0,
    pipeline_result: ExpenseExtractionResult | None = None,
) -> dict[str, Any]:
    """
    CRM-aligned expense wizard: collect → review → submit confirm → CRM payload.

    Travel categories (Bus, Train, …) require From/To; Lunch/Snack do not.
    """
    del company_id, employee_id, session_id
    wf = clone_workflow_state(workflow_state)
    block = wf.setdefault("expense_request", {})
    ensure_expense_block_active(block)
    _sync_reply_language(block, message)
    lang = lang_from_block(block)

    items: list[dict[str, Any]] = normalize_expense_items(
        list(block.get("items") or [])
    )
    block["items"] = items
    stage = normalize_expense_stage(str(block.get("stage") or "collecting"))
    hint_entities = dict((pipeline_result.entities if pipeline_result else {}) or {})
    inc_iso = str(
        block.get("incurred_date_iso")
        or hint_entities.get("expense_incurred_date")
        or infer_expense_incurred_date_iso(
            message=message,
            hints=hint_entities,
            today=expense_incurred_date_mod.date.today(),
        )
    )
    block["incurred_date_iso"] = inc_iso

    from chat.services.expense.session_action_memory import (
        is_vague_expense_add,
        record_expense_lines_added,
        record_vague_add_prompt,
        vague_add_clarification,
    )

    if stage == STAGE_COLLECTING and is_vague_expense_add(message):
        wf = record_vague_add_prompt(wf)
        return _pack(
            wf,
            block,
            items=items,
            question=vague_add_clarification(lang=lang),
            inc_iso=inc_iso,
        )

    restore_pack = _try_handle_restore_turn(
        wf,
        block,
        items,
        message,
        lang=lang,
        inc_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
    )
    if restore_pack:
        return restore_pack

    total_pack = _try_handle_total_check_turn(
        wf,
        block,
        items,
        message,
        lang=lang,
        inc_iso=inc_iso,
    )
    if total_pack:
        return total_pack

    if wants_resume_or_show_expense(message):
        resume_msg = format_expense_resume_message(wf, user_message=message)
        if resume_msg:
            return _pack(
                wf,
                block,
                items=items,
                question=resume_msg,
                inc_iso=inc_iso,
            )

    # --- Submit confirm (second yes) ---
    if stage == STAGE_SUBMIT_CONFIRM:
        if is_confirmation_yes(message):
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
            )
            if not val.ok:
                set_expense_stage(block, STAGE_COLLECTING)
                return _pack(
                    wf,
                    block,
                    items=items,
                    question=val.blocking_message,
                    warnings=val.warnings,
                    inc_iso=inc_iso,
                    validation_blocked=True,
                )
            date_block = expense_submit_date_block_reason(inc_iso, today=date.today())
            if date_block:
                set_expense_stage(block, STAGE_SUBMIT_CONFIRM)
                block["submit_blocked_reason"] = date_block
                return _pack(
                    wf,
                    block,
                    items=items,
                    question=(
                        f"{date_block}\n\n"
                        "এই খরচের তারিখে CRM-এ এখন জমা দেওয়া যাবে না। "
                        "আজকের খরচ হলে তারিখ ঠিক করে আবার লিখুন, অথবা ওই দিনে/পরে চেষ্টা করুন।"
                    ),
                    warnings=val.warnings,
                    inc_iso=inc_iso,
                    validation_blocked=True,
                )
            block.pop("submit_blocked_reason", None)
            wf = deactivate_expense_session(wf)
            return {
                "workflow_state": wf,
                "complete": True,
                "submitted": True,
                "question": None,
                "items": items,
                "warnings": val.warnings,
                "incurred_date_iso": inc_iso,
                "validation_blocked": False,
                "crm_payload": items,
            }
        if is_confirmation_no(message):
            set_expense_stage(block, STAGE_REVIEW)
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
            )
            return _pack(
                wf,
                block,
                items=items,
                question="ঠিক আছে — আবার দেখুন:\n\n"
                + format_expense_summary(
                    items,
                    incurred_date_iso=inc_iso,
                    warnings=val.warnings,
                    line_flags=val.line_flags,
                    lang=lang,
                ),
                warnings=val.warnings,
                inc_iso=inc_iso,
            )
        return _pack(
            wf,
            block,
            items=items,
            question=format_submit_confirm_prompt(lang),
            inc_iso=inc_iso,
        )

    # --- Data review (first yes → submit prompt) ---
    if stage == STAGE_REVIEW:
        total_pack = _try_handle_total_check_turn(
            wf,
            block,
            items,
            message,
            lang=lang,
            inc_iso=inc_iso,
        )
        if total_pack:
            return total_pack

        if is_confirmation_yes(message):
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
            )
            if not val.ok:
                set_expense_stage(block, STAGE_COLLECTING)
                return _pack(
                    wf,
                    block,
                    items=items,
                    question=val.blocking_message,
                    warnings=val.warnings,
                    inc_iso=inc_iso,
                    validation_blocked=True,
                )
            date_block = expense_submit_date_block_reason(inc_iso, today=date.today())
            if date_block:
                set_expense_stage(block, STAGE_REVIEW)
                return _pack(
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
                )
            set_expense_stage(block, STAGE_SUBMIT_CONFIRM)
            return _pack(
                wf,
                block,
                items=items,
                question=format_submit_confirm_prompt(lang),
                warnings=val.warnings,
                inc_iso=inc_iso,
            )

        review_snapshot = [dict(x) for x in items]

        if is_confirmation_no(message):
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
            return _pack(
                wf,
                block,
                items=items,
                question=q,
                warnings=val.warnings,
                inc_iso=inc_iso,
            )

        corrected = False
        from chat.services.expense.command_parser import parse_correction_plan

        correction_plan = parse_correction_plan(message)
        if correction_plan.has_any_correction() or looks_like_expense_correction(message):
            wf = push_expense_snapshot(
                wf,
                items=items,
                stage=STAGE_REVIEW,
                action_type="before_correction",
                incurred_date_iso=inc_iso,
                lang=lang,
            )
            items, corrected = _apply_review_corrections(
                items, message, trace_id=trace_id
            )

        if looks_like_duplicate_expense_reentry(message, items):
            return _pack(
                wf,
                block,
                items=items,
                question=duplicate_reentry_notice(lang),
                inc_iso=inc_iso,
            )

        if not corrected and looks_like_expense_correction(message):
            fail_note = build_correction_failure_notice(message, items, lang=lang)
            if fail_note:
                val = validate_expense_items(
                    items,
                    incurred_date_iso=inc_iso,
                    day_logged_total=day_logged_total,
                    daily_cap=daily_cap,
                    message=message,
                )
                q = (
                    fail_note
                    + "\n\n"
                    + format_expense_summary(
                        items,
                        incurred_date_iso=inc_iso,
                        warnings=val.warnings,
                        line_flags=val.line_flags,
                        lang=lang,
                    )
                )
                return _pack(
                    wf,
                    block,
                    items=items,
                    question=q,
                    warnings=val.warnings,
                    inc_iso=inc_iso,
                )

        if (
            not corrected
            and not looks_like_expense_correction(message)
            and (extract_expense_items(message).items or [])
        ):
            return _pack(
                wf,
                block,
                items=items,
                question=duplicate_reentry_notice(lang),
                inc_iso=inc_iso,
            )

        if corrected and not items and review_snapshot:
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
            return _pack(
                wf,
                block,
                items=items,
                question=q,
                warnings=val.warnings,
                inc_iso=inc_iso,
            )

        items = dedupe_expense_items(items)
        if not items and review_snapshot:
            items = review_snapshot

        if _wants_finish_collecting(message) and items:
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
                return adv

        val = validate_expense_items(
            items,
            incurred_date_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            message=message,
        )
        if not val.ok:
            set_expense_stage(block, STAGE_COLLECTING)
            return _pack(
                wf,
                block,
                items=items,
                question=val.blocking_message
                or "কোনো খরচ পাওয়া যায়নি। lunch 100, bus 50 — এভাবে লিখুন।",
                warnings=val.warnings,
                inc_iso=inc_iso,
                validation_blocked=True,
            )

        set_expense_stage(block, STAGE_REVIEW)
        block["review_line_flags"] = val.line_flags
        q = format_expense_summary(
            items,
            incurred_date_iso=inc_iso,
            warnings=val.warnings,
            line_flags=val.line_flags,
            lang=lang,
        )
        if corrected:
            q = "আপডেট করা হয়েছে।\n\n" + q
            action_type = "after_correction"
            if wants_travel_group_remove(message):
                action_type = "after_travel_remove"
                set_ingest_lock(block, reason=REASON_TRAVEL_REMOVED)
            else:
                from chat.services.expense.expense_ingest_guard import clear_ingest_lock

                clear_ingest_lock(block)
            wf = push_expense_snapshot(
                wf,
                items=items,
                stage=STAGE_REVIEW,
                action_type=action_type,
                incurred_date_iso=inc_iso,
                lang=lang,
            )
            from chat.services.expense.session_action_memory import record_expense_corrected

            wf = record_expense_corrected(
                wf,
                items=items,
                incurred_date_iso=inc_iso,
                stage="review",
            )
        return _pack(
            wf,
            block,
            items=items,
            question=q,
            warnings=val.warnings,
            inc_iso=inc_iso,
        )

    # --- Collecting ---
    if str(block.get("pending_step") or "") == "clarify":
        pending_stub = block.get("pending_line") if isinstance(block.get("pending_line"), dict) else {}
        return _handle_pending_line(
            wf,
            block,
            items,
            dict(pending_stub or {}),
            message,
            inc_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
        )

    pending = block.get("pending_line")
    if isinstance(pending, dict) and pending.get("amount"):
        if _should_reset_pending_for_message(
            message, pending_step=str(block.get("pending_step") or "")
        ):
            block.pop("pending_line", None)
            block.pop("pending_step", None)
            block.pop("pending_queue", None)
            block.pop("clarification_issues", None)
        else:
            return _handle_pending_line(
                wf,
                block,
                items,
                dict(pending),
                message,
                inc_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
            )

    if items and (
        _wants_finish_collecting(message) or is_confirmation_yes(message)
    ):
        if _has_pending_expense_line(block):
            return _pack(
                wf,
                block,
                items=items,
                question=_pending_finish_block_message(block, lang=lang),
                inc_iso=inc_iso,
            )
        # Legacy shortcut: if the user answers "yes" to the "anything else?"
        # prompt while still in collecting, treat it as "submit now".
        # This keeps older flows/tests compatible without requiring a second confirmation.
        if is_confirmation_yes(message):
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
            )
            if not val.ok:
                set_expense_stage(block, STAGE_COLLECTING)
                return _pack(
                    wf,
                    block,
                    items=items,
                    question=val.blocking_message
                    or "কিছু তথ্য মিসিং আছে — amount + category দিন (যেমন: lunch 100)।",
                    warnings=val.warnings,
                    inc_iso=inc_iso,
                    validation_blocked=True,
                )
            date_block = expense_submit_date_block_reason(inc_iso, today=date.today())
            if date_block:
                set_expense_stage(block, STAGE_COLLECTING)
                block["submit_blocked_reason"] = date_block
                return _pack(
                    wf,
                    block,
                    items=items,
                    question=(
                        f"{date_block}\n\n"
                        "এই খরচের তারিখে এখন জমা দেওয়া যাবে না। তারিখ ঠিক করে আবার চেষ্টা করুন।"
                    ),
                    warnings=val.warnings,
                    inc_iso=inc_iso,
                    validation_blocked=True,
                )
            block.pop("submit_blocked_reason", None)
            wf = deactivate_expense_session(wf)
            return {
                "workflow_state": wf,
                "complete": True,
                "submitted": True,
                "question": None,
                "items": items,
                "warnings": val.warnings,
                "incurred_date_iso": inc_iso,
                "validation_blocked": False,
                "crm_payload": items,
            }
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
            return adv

    loose_amt = parse_amount_only(message)
    if loose_amt is not None:
        block["pending_line"] = {
            "amount": loose_amt,
            "category": "",
            "from_location": "",
            "to_location": "",
        }
        block["pending_step"] = "category"
        set_expense_stage(block, STAGE_COLLECTING)
        q, facts = _ask_category_prompt(block, items, loose_amt, lang=lang)
        return _pack(
            wf,
            block,
            items=items,
            question=q,
            inc_iso=inc_iso,
            message_facts=facts,
        )

    if items and should_block_compound_reingest(block, message, items):
        val = validate_expense_items(
            items,
            incurred_date_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
            message=message,
        )
        lock_note = ""
        if block.get("ingest_lock"):
            lock_note = "\n\n" + ingest_lock_notice(block, lang=lang)
        q = (
            duplicate_reentry_notice(lang)
            + lock_note
            + "\n\n"
            + format_expense_summary(
                items,
                incurred_date_iso=inc_iso,
                warnings=val.warnings,
                line_flags=val.line_flags,
                lang=lang,
            )
        )
        return _pack(
            wf,
            block,
            items=items,
            question=q,
            warnings=val.warnings,
            inc_iso=inc_iso,
        )

    ext = _extract_lines_from_message(message, pipeline_result=pipeline_result)
    if ext.items or ext.malformed:
        before_count = len(items)
        items, blocked_q = _ingest_extracted_lines(block, items, ext, inc_iso=inc_iso)
        if len(items) > before_count:
            if block.get("ingest_lock") and not looks_like_compound_expense_claim(
                message
            ):
                from chat.services.expense.expense_ingest_guard import clear_ingest_lock

                clear_ingest_lock(block)
            wf = record_expense_lines_added(
                wf,
                new_items=items[before_count:],
                all_items=items,
                incurred_date_iso=inc_iso,
                stage=str(block.get("stage") or STAGE_COLLECTING),
            )
        if blocked_q:
            wf["expense_request"] = block
            return _pack(
                wf,
                block,
                items=items,
                question=blocked_q,
                inc_iso=inc_iso,
            )
        if items:
            gap_q = _unallocated_total_prompt(message, items, lang=lang)
            if gap_q:
                return _pack(
                    wf,
                    block,
                    items=items,
                    question=gap_q,
                    inc_iso=inc_iso,
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
                return adv
            q, facts = _ask_more_lines_prompt(block, items, lang=lang)
            return _pack(
                wf,
                block,
                items=items,
                question=q,
                inc_iso=inc_iso,
                message_facts=facts,
            )

    cat_only = parse_category_token(message)
    if cat_only and not items and not re.search(_AMOUNT_RE, message):
        return _pack(
            wf,
            block,
            items=items,
            question="কত টাকা খরচ হয়েছে? (যেমন: 100 taka)",
            inc_iso=inc_iso,
        )

    if not items:
        set_expense_stage(block, STAGE_COLLECTING)
        q = (
            "আজকের খরচ বলুন — amount দিলে পরে ধরন (lunch/bus/…) জিজ্ঞেস করব।\n"
            "অথবা একসাথে: `lunch 100, bus 50 office to badda`"
        )
        if ext.malformed:
            q = (
                "কিছু লাইন বুঝতে পারিনি। category ও amount স্পষ্ট করে লিখুন।\n"
                "উদাহরণ: lunch 100, bus 50 office to home"
            )
        return _pack(wf, block, items=[], question=q, inc_iso=inc_iso)

    items, _ = _apply_corrections(items, message)
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
        return adv
    q, facts = _ask_more_lines_prompt(block, items, lang=lang)
    return _pack(
        wf,
        block,
        items=items,
        question=q,
        inc_iso=inc_iso,
        message_facts=facts,
    )
