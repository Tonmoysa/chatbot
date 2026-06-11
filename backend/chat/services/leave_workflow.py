"""
Dynamic slot-filling leave collection. Extracts all available fields first, asks only what is missing.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from chat.services.leave_draft_utils import (
    DAY_SCOPE_FULL,
    DAY_SCOPE_HALF,
    LEAVE_PAYMENT_LWOP,
    LEAVE_PAYMENT_PAID,
    apply_leave_draft_defaults,
    normalize_end_equals_start_if_missing,
    supporting_document_needed,
    validate_dates,
)
from chat.services.leave_policies import get_company_leave_policy
from chat.services.leave_slot_extraction import (
    apply_payment_category_from_message,
    explicit_leave_type_from_message,
    extract_leave_slots,
    is_payment_only_message,
)
from chat.services.leave_confirm import (
    build_confirmation_prompt,
    process_confirmation_turn,
)
from chat.services.leave_fsm import (
    ACTIVE_FLOW_LEAVE,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    apply_leave_state,
    clear_leave_flow,
    deep_merge_draft,
    is_awaiting_leave_confirmation,
    mark_review_pending,
    normalize_workflow_state,
    read_leave_state,
)
from chat.services.leave_slots import (
    SLOT_DATES,
    SLOT_DOCUMENT,
    SLOT_HALF_PERIOD,
    SLOT_LEAVE_TYPE,
    SLOT_REASON,
    SLOT_SCOPE,
    apply_wizard_answer,
    generate_question,
    get_missing_slots,
    prefill_draft_from_extraction,
)

__all__ = [
    "LEAVE_PAYMENT_PAID",
    "LEAVE_PAYMENT_LWOP",
    "DAY_SCOPE_FULL",
    "DAY_SCOPE_HALF",
    "supporting_document_needed",
    "process_leave_turn",
    "pending_step",
    "pending_question",
    "is_leave_collecting",
    "is_leave_in_progress",
    "is_leave_paused",
    "is_awaiting_leave_confirmation",
    "pause_leave_session",
    "resume_leave_session",
    "deactivate_leave_session",
    "merge_extractor_entities",
    "build_merged_entities_for_engine",
]

_LEAVE_TYPE_PATTERNS: tuple[tuple[str, str], ...] = (
    (
        r"\b(sick(?:ness)?|ill(?:ness)?|medical|health)\b|অসুস্থ|জ্বর|ডাক্তার",
        "sick",
    ),
    (r"\b(casual)\b|ক্যাজুয়াল|নৈমিত্তিক", "casual"),
    (r"\b(famil\w*|family|wedding|funeral)\b|পরিবার", "casual"),
    (r"\b(annual|vacation|pto)\b|বার্ষিক", "annual"),
    (r"\b(maternity)\b|মাতৃত্ব", "maternity"),
    (r"\b(paternity)\b|পিতৃত্ব", "paternity"),
    (r"\b(emergency)\b|জরুরি", "emergency"),
    (r"\b(compensatory|comp\s*off)\b|কম্পেনসেটরি", "compensatory"),
)


def clone_workflow_state(state: dict[str, Any] | None) -> dict[str, Any]:
    return dict(state or {})


def is_leave_collecting(workflow_state: dict[str, Any] | None) -> bool:
    from chat.services.leave_fsm import is_leave_collecting as _fsm_collecting

    return _fsm_collecting(workflow_state)


def is_leave_paused(workflow_state: dict[str, Any] | None) -> bool:
    from chat.services.leave_fsm import is_leave_paused as _fsm_paused

    return _fsm_paused(workflow_state)


def is_leave_in_progress(workflow_state: dict[str, Any] | None) -> bool:
    from chat.services.leave_fsm import is_leave_in_progress as _fsm_in_progress

    return _fsm_in_progress(workflow_state)


def pause_leave_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = normalize_workflow_state(workflow_state)
    st = read_leave_state(wf)
    if st.get("active_flow") != ACTIVE_FLOW_LEAVE:
        return wf
    return apply_leave_state(
        wf,
        draft=dict(st.get("draft") or {}),
        step=st.get("step"),
        status=STATUS_PAUSED,
        review_pending=bool(st.get("review_pending")),
    )


def resume_leave_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = normalize_workflow_state(workflow_state)
    st = read_leave_state(wf)
    if st.get("active_flow") != ACTIVE_FLOW_LEAVE:
        return wf
    return apply_leave_state(
        wf,
        draft=dict(st.get("draft") or {}),
        step=st.get("step"),
        status=STATUS_ACTIVE,
        review_pending=bool(st.get("review_pending")),
    )


def pending_question(workflow_state: dict[str, Any] | None) -> str | None:
    if not is_leave_in_progress(workflow_state):
        return None
    st = read_leave_state(workflow_state)
    draft = dict(st.get("draft") or {})
    if st.get("review_pending") or is_awaiting_leave_confirmation(workflow_state):
        return build_confirmation_prompt(draft)
    missing = get_missing_slots(draft)
    if not missing:
        return None
    return generate_question(
        missing[0], draft, remaining=len(missing), missing=missing
    )


def pending_step(workflow_state: dict[str, Any] | None) -> str | None:
    if not is_leave_in_progress(workflow_state):
        return None
    st = read_leave_state(workflow_state)
    return st.get("step") or _first_missing_step(dict(st.get("draft") or {}))


def deactivate_leave_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    return clear_leave_flow(workflow_state)


def _infer_payment_category(
    message: str, draft: dict[str, Any], *, force: bool = False
) -> bool:
    pay = apply_payment_category_from_message(message)
    if not pay:
        return False
    if not force and draft.get("leave_payment_category"):
        return False
    draft["leave_payment_category"] = (
        LEAVE_PAYMENT_LWOP if pay == "lwop" else LEAVE_PAYMENT_PAID
    )
    return True


def _infer_leave_type(message: str, draft: dict[str, Any]) -> None:
    from chat.services.leave.normalization import parse_wizard_leave_type_answer
    from chat.services.leave_draft_utils import (
        WIZARD_LEAVE_TYPES,
        should_auto_infer_wizard_leave_type,
        sync_payment_from_leave_type,
    )

    wizard_lt = parse_wizard_leave_type_answer(message)
    if wizard_lt:
        draft["leave_type"] = wizard_lt
        sync_payment_from_leave_type(draft)
        return
    if is_payment_only_message(message):
        return
    if draft.get("leave_type") in WIZARD_LEAVE_TYPES and not explicit_leave_type_from_message(message):
        return
    explicit = explicit_leave_type_from_message(message)
    if explicit:
        draft["leave_type"] = "unpaid" if explicit == "unpaid" else explicit
        if draft["leave_type"] == "casual":
            draft["leave_type"] = "annual"
        sync_payment_from_leave_type(draft)
        return
    if not should_auto_infer_wizard_leave_type(draft):
        return
    low = message.lower()
    for pattern, code in _LEAVE_TYPE_PATTERNS:
        if re.search(pattern, low, re.I):
            draft["leave_type"] = "casual" if code == "casual" else code
            if draft["leave_type"] == "casual":
                draft["leave_type"] = "annual"
            sync_payment_from_leave_type(draft)
            return


def _infer_day_scope(message: str, draft: dict[str, Any]) -> None:
    from chat.services.leave.normalization import parse_day_scope_answer

    if draft.get("day_scope"):
        return
    scope = parse_day_scope_answer(message)
    if scope:
        draft["day_scope"] = scope


def _reason_from_message(message: str, *, edit_context: bool = False) -> str | None:
    from chat.services.leave_slot_extraction import extract_reason_from_message

    return extract_reason_from_message(message, edit_context=edit_context)


def merge_extractor_entities(
    draft: dict[str, Any],
    entities: dict[str, Any],
    *,
    overwrite: bool = False,
    message: str = "",
) -> None:
    """Patch draft from entities — by default never clobber filled slots."""
    from chat.services.leave.normalization import (
        message_explicitly_states_day_scope,
        message_explicitly_states_leave_date,
        message_explicitly_states_payment_category,
        should_suppress_inferred_leave_dates,
    )

    dt = entities.get("document_text")
    if dt and str(dt).strip():
        draft["document_text"] = str(dt).strip()
    for key in (
        "start_date",
        "end_date",
        "date",
        "days",
        "leave_type",
        "reason",
        "leave_payment_category",
        "day_scope",
        "half_day_period",
    ):
        v = entities.get(key)
        if v is None or v == "":
            continue
        if not overwrite and draft.get(key):
            continue
        if key == "leave_payment_category":
            if not message_explicitly_states_payment_category(message):
                continue
            s = str(v).strip().lower()
            draft["leave_payment_category"] = (
                LEAVE_PAYMENT_LWOP if s in {"lwop", "unpaid"} else LEAVE_PAYMENT_PAID
            )
            continue
        if key == "day_scope":
            if not message_explicitly_states_day_scope(message):
                continue
            s = str(v).strip().lower()
            draft["day_scope"] = DAY_SCOPE_HALF if s.startswith("half") else DAY_SCOPE_FULL
            continue
        if key in ("start_date", "end_date", "date"):
            if should_suppress_inferred_leave_dates(message):
                continue
            if key in ("start_date", "date") and not message_explicitly_states_leave_date(
                message
            ):
                continue
        if key == "reason":
            from chat.services.leave.reason_value import is_boilerplate_leave_reason

            if is_boilerplate_leave_reason(str(v)):
                continue
        if key == "leave_type":
            from chat.services.leave_slot_extraction import explicit_leave_type_from_message

            explicit = explicit_leave_type_from_message(message)
            if explicit:
                draft["leave_type"] = explicit
                continue
            from chat.services.leave.normalization import text_has_sick_signal

            if not (str(v).strip().lower() == "sick" and text_has_sick_signal(message)):
                continue
        if key == "date" and not draft.get("start_date"):
            draft["start_date"] = str(v).split("T")[0]
            continue
        draft[key] = v
    rs = entities.get("description")
    if rs and str(rs).strip() and (overwrite or not draft.get("reason")):
        from chat.services.leave.reason_value import is_boilerplate_leave_reason

        if not is_boilerplate_leave_reason(str(rs)):
            draft["reason"] = str(rs).strip()[:2000]


def _first_missing_step(draft: dict[str, Any]) -> str | None:
    missing = get_missing_slots(draft)
    return missing[0] if missing else None


def _is_compound_slot_message(message: str) -> bool:
    """Comma-separated or multi-field replies must use full extraction, not one-slot wizard path."""
    raw = (message or "").strip()
    if not raw:
        return False
    if re.search(r"[,;]| এবং | and ", raw, re.I):
        return True
    low = raw.lower()
    signals = 0
    if re.search(r"\b(paid|unpaid|lwop)\b", low):
        signals += 1
    if re.search(
        r"\b(sick|casual|annual|medical|emergency|maternity|paternity)\b", low
    ):
        signals += 1
    if re.search(r"\b(full|half)\b|full\s*day|half\s*day", low):
        signals += 1
    if re.search(
        r"\b(tomorrow|today|kal|agamikal|next\s+week|আগামীকাল|আজ)\b",
        low,
    ):
        signals += 1
    if re.search(
        r"\b(family|wedding|funeral|travel|program|পরিবার|অনুষ্ঠান)\b",
        low,
    ):
        signals += 1
    return signals >= 2


def _apply_slots_from_message(
    draft: dict[str, Any],
    message: str,
    entities: dict[str, Any],
    *,
    overwrite: bool = False,
) -> None:
    """Parse one or more slot values from a user message into the draft."""
    from chat.services.leave.entity_pipeline import LeaveEntityPipeline

    LeaveEntityPipeline().apply_to_draft(
        draft, message, entities, overwrite=overwrite
    )


def _force_scope_from_message(message: str, draft: dict[str, Any]) -> bool:
    """Overwrite day_scope when user clearly states half/full (review corrections)."""
    from chat.services.leave.normalization import parse_day_scope_answer

    scope = parse_day_scope_answer(message)
    if scope:
        draft["day_scope"] = scope
        return True
    return False


def _is_direct_slot_answer(message: str, pending_slot: str | None) -> bool:
    if not pending_slot or _is_compound_slot_message(message):
        return False
    t = message.strip().lower()
    if pending_slot in (SLOT_LEAVE_TYPE, "leave_type"):
        from chat.services.leave.normalization import parse_wizard_leave_type_answer

        return parse_wizard_leave_type_answer(message) is not None
    if pending_slot == "leave_payment_category":
        return t in {"paid", "unpaid", "lwop"}
    if pending_slot == SLOT_SCOPE:
        from chat.services.leave.normalization import parse_day_scope_answer

        return parse_day_scope_answer(message) is not None
    if pending_slot == SLOT_HALF_PERIOD:
        from chat.services.leave.normalization import parse_half_day_period_answer

        return parse_half_day_period_answer(message) is not None
    if pending_slot == "supporting_document":
        from chat.services.leave.document_turn_parser import is_document_slot_resolvable

        return is_document_slot_resolvable(message, use_llm=False)
    if pending_slot == SLOT_REASON:
        from chat.services.leave_draft_utils import is_reason_skip_message

        return is_reason_skip_message(message) or (
            len(t) >= 4 and t not in {"paid", "unpaid", "full", "half", "sick", "annual"}
        )
    return False


def build_merged_entities_for_engine(draft: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "start_date": draft.get("start_date"),
        "end_date": draft.get("end_date") or draft.get("start_date"),
        "date": draft.get("start_date"),
        "days": draft.get("days"),
        "leave_type": draft.get("leave_type"),
        "reason": draft.get("reason"),
        "leave_payment_category": draft.get("leave_payment_category"),
        "day_scope": draft.get("day_scope"),
        "half_day_period": draft.get("half_day_period"),
        "document_text": draft.get("document_text"),
    }
    if draft.get("supporting_document_waived"):
        out["supporting_document_waived"] = True
    return {k: v for k, v in out.items() if v is not None and v != ""}


def _pending_slot_unfilled(draft: dict[str, Any], pending_slot: str) -> bool:
    from chat.services.leave_draft_utils import WIZARD_LEAVE_TYPES, needs_half_day_period

    if pending_slot == SLOT_SCOPE:
        return not draft.get("day_scope")
    if pending_slot == SLOT_HALF_PERIOD:
        return needs_half_day_period(draft) and not draft.get("half_day_period")
    if pending_slot == SLOT_REASON:
        return not (
            (draft.get("reason") or "").strip()
            or draft.get("_reason_skipped")
            or draft.get("_reason_implied")
        )
    if pending_slot in (SLOT_LEAVE_TYPE, "leave_type"):
        return str(draft.get("leave_type") or "").lower() not in WIZARD_LEAVE_TYPES
    if pending_slot == "leave_payment_category":
        return not draft.get("leave_payment_category")
    if pending_slot == SLOT_DATES:
        return not draft.get("start_date")
    return True


def process_leave_turn(
    *,
    workflow_state: dict[str, Any],
    message: str,
    entities: dict[str, Any],
    company_id: str = "",
    trace_id: str = "",
) -> dict[str, Any]:
    wf = normalize_workflow_state(workflow_state)
    from chat.services.leave.duplicate_choice import (
        handle_duplicate_leave_choice_turn,
        mark_duplicate_leave_choice_pending,
    )
    from chat.services.leave_meta_queries import (
        _target_date_range_from_leave_message,
        build_parallel_leave_block_message,
        check_overlapping_submitted_leave,
        should_block_parallel_leave_application,
    )

    choice_pack = handle_duplicate_leave_choice_turn(wf, message)
    if choice_pack:
        wf = choice_pack.get("workflow_state") or wf
        if choice_pack.get("duplicate_choice") == "continue":
            return {
                "workflow_state": wf,
                "merged_entities": build_merged_entities_for_engine(
                    dict(read_leave_state(wf).get("draft") or {})
                ),
                "complete": False,
                "confirmed_submit": False,
                "question": choice_pack.get("question") or "",
            }
        if choice_pack.get("restart"):
            wf = choice_pack["workflow_state"]

    overlap_msg = check_overlapping_submitted_leave(wf, message)
    if overlap_msg:
        target_rng = _target_date_range_from_leave_message(message)
        if target_rng:
            wf = mark_duplicate_leave_choice_pending(
                wf, target_start=target_rng[0], target_end=target_rng[1]
            )
        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(
                dict(read_leave_state(wf).get("draft") or {})
            ),
            "complete": False,
            "confirmed_submit": False,
            "question": overlap_msg,
        }

    st = read_leave_state(wf)
    if st.get("active_flow") != ACTIVE_FLOW_LEAVE:
        seed: dict[str, Any] = {}
        if choice_pack and choice_pack.get("restart"):
            seed = dict(read_leave_state(wf).get("draft") or {})
        wf = apply_leave_state(
            wf, draft=seed, step=None, status=STATUS_ACTIVE, review_pending=False
        )
        st = read_leave_state(wf)

    if should_block_parallel_leave_application(message, wf):
        draft_preview = dict(read_leave_state(wf).get("draft") or {})
        block = build_parallel_leave_block_message()
        if is_awaiting_leave_confirmation(wf):
            from chat.services.leave_confirm import build_confirmation_prompt

            question = f"{block}\n\n{build_confirmation_prompt(draft_preview)}"
        else:
            question = block
        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(draft_preview),
            "complete": False,
            "confirmed_submit": False,
            "question": question,
        }

    draft = deep_merge_draft(dict(st.get("draft") or {}), {})
    draft["_last_user_message"] = message
    from chat.services.leave_draft_utils import persist_stated_leave_type

    persist_stated_leave_type(draft, message)
    had_scope = bool(draft.get("day_scope"))

    from chat.services.leave.date_correction import try_apply_leave_date_correction

    try_apply_leave_date_correction(
        draft, message, trace_id=trace_id, use_llm=True
    )

    from chat.services.leave_confirm import (
        SLOT_EDIT_MENU,
        _begin_edit_slot,
        _finish_edit_return_review,
        build_confirmation_prompt,
        is_edit_abort,
        parse_edit_field_choice,
        process_confirmation_turn,
        restore_leave_review_from_edit,
        try_apply_inline_edit_value,
        wants_navigate_back_to_leave_review,
        wants_resume_or_show_expense,
    )
    from chat.services.leave_fsm import KEY_EDIT_SNAPSHOT
    from chat.services.expense_workflow import (
        format_expense_resume_message,
        resume_expense_session,
    )
    from chat.services.workflow_suspend import (
        has_suspended_expense,
        suspend_leave_for_workflow_switch,
    )

    if wants_resume_or_show_expense(message) and has_suspended_expense(wf):
        wf = suspend_leave_for_workflow_switch(wf)
        wf = resume_expense_session(wf)
        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(draft),
            "complete": False,
            "confirmed_submit": False,
            "question": format_expense_resume_message(wf, user_message=message),
        }

    from chat.services.leave_confirm import is_confirmation_yes, wants_leave_submit_command
    from chat.services.leave.normalization import normalize_leave_draft

    if wants_leave_submit_command(message) and not is_awaiting_leave_confirmation(wf):
        from chat.services.expense.expense_fsm import is_expense_in_progress

        if is_expense_in_progress(wf):
            from chat.services.workflow_suspend import suspend_expense_for_workflow_switch

            wf = suspend_expense_for_workflow_switch(wf)

        policy = get_company_leave_policy(company_id or "default")
        normalize_leave_draft(draft)
        apply_leave_draft_defaults(draft, policy)
        missing = get_missing_slots(draft, policy=policy)
        if not missing:
            apply_leave_draft_defaults(draft, policy)
            wf = mark_review_pending(wf, draft)
            if is_confirmation_yes(message):
                return process_confirmation_turn(
                    workflow_state=wf,
                    message=message,
                    draft=draft,
                    entities=entities,
                    trace_id=trace_id,
                )
            from chat.services.leave_confirm import build_deferred_leave_return_prompt

            return {
                "workflow_state": wf,
                "merged_entities": build_merged_entities_for_engine(draft),
                "complete": False,
                "confirmed_submit": False,
                "question": build_deferred_leave_return_prompt(draft, message=message),
            }

    if wf.get(KEY_EDIT_SNAPSHOT) and (
        is_edit_abort(message) or wants_navigate_back_to_leave_review(message)
    ):
        wf, snap = restore_leave_review_from_edit(wf)
        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(snap),
            "complete": False,
            "confirmed_submit": False,
            "question": build_confirmation_prompt(snap),
        }

    if is_awaiting_leave_confirmation(wf) or st.get("step") == SLOT_EDIT_MENU:
        return process_confirmation_turn(
            workflow_state=wf,
            message=message,
            draft=draft,
            entities=entities,
            trace_id=trace_id,
        )

    pending_slot = st.get("step")
    policy = get_company_leave_policy(company_id or "default")
    extraction = extract_leave_slots(message, skip_leave_phrase_gate=True)

    from chat.services.leave.normalization import parse_day_scope_answer
    from chat.services.leave.reason_bucket_classifier import apply_leave_semantic_reconcile
    from chat.services.leave.reason_correction_parser import try_apply_reason_correction

    reason_corrected = try_apply_reason_correction(
        draft,
        message,
        trace_id=trace_id,
        use_llm=True,
    )

    scope_answer = parse_day_scope_answer(message)
    if scope_answer:
        draft["day_scope"] = scope_answer

    in_edit_flow = bool(wf.get(KEY_EDIT_SNAPSHOT)) or st.get("step") == SLOT_EDIT_MENU
    switch_slot = (
        parse_edit_field_choice(message)
        if in_edit_flow
        else None
    )
    if switch_slot and switch_slot != pending_slot:
        from chat.services.leave_confirm import _begin_edit_slot_with_inline_apply

        return _begin_edit_slot_with_inline_apply(
            wf, draft, switch_slot, message, entities
        )

    from chat.services.leave.normalization import message_explicitly_states_day_scope

    document_handled = False
    if pending_slot == SLOT_DOCUMENT and not _is_compound_slot_message(message):
        from chat.services.leave.reason_correction_parser import looks_like_reason_correction
        from chat.services.leave.document_turn_parser import apply_document_answer
        from chat.services.leave_draft_utils import clear_supporting_document_if_unneeded

        if reason_corrected or looks_like_reason_correction(message):
            clear_supporting_document_if_unneeded(draft)
        elif apply_document_answer(
            draft, message, trace_id=trace_id, use_llm=True
        ):
            document_handled = True
        else:
            wf = apply_leave_state(
                wf,
                draft=draft,
                step=SLOT_DOCUMENT,
                status=STATUS_ACTIVE,
                review_pending=False,
            )
            return {
                "workflow_state": wf,
                "merged_entities": build_merged_entities_for_engine(draft),
                "complete": False,
                "confirmed_submit": False,
                "question": generate_question(
                    SLOT_DOCUMENT,
                    draft,
                    remaining=1,
                    missing=[SLOT_DOCUMENT],
                ),
            }

    if (
        not document_handled
        and pending_slot
        and not _is_compound_slot_message(message)
        and try_apply_inline_edit_value(draft, pending_slot, message)
    ):
        pass
    elif (
        not document_handled
        and pending_slot == SLOT_SCOPE
        and not message_explicitly_states_day_scope(message)
        and not _is_direct_slot_answer(message, pending_slot)
    ):
        # User repeated the compound request or sent unrelated text — keep asking.
        pass
    elif (
        not document_handled
        and pending_slot
        and not _is_compound_slot_message(message)
        and _is_direct_slot_answer(message, pending_slot)
    ):
        apply_wizard_answer(draft, pending_slot=pending_slot, message=message)
    else:
        if pending_slot and _pending_slot_unfilled(draft, pending_slot):
            from chat.services.leave.collecting_turn_parser import try_resolve_collecting_slot
            from chat.services.leave.turn_apply import apply_leave_field_update

            slot_upd = try_resolve_collecting_slot(
                message,
                pending_slot=pending_slot,
                draft=draft,
                entities=entities,
                trace_id=trace_id,
            )
            if slot_upd:
                apply_leave_field_update(draft, slot_upd, message=message)
        before = dict(draft)
        _apply_slots_from_message(draft, message, entities, overwrite=False)
        draft = deep_merge_draft(before, draft)
        if (
            not message_explicitly_states_day_scope(message)
            and not (pending_slot == SLOT_SCOPE and _is_direct_slot_answer(message, SLOT_SCOPE))
            and not had_scope
        ):
            draft.pop("day_scope", None)

    from chat.services.leave_draft_utils import apply_duration_end_date

    normalize_leave_draft(draft)
    apply_leave_semantic_reconcile(
        draft,
        message=message,
        trace_id=trace_id,
        use_llm=True,
    )
    normalize_end_equals_start_if_missing(draft)
    apply_duration_end_date(draft)
    date_err: str | None = None
    if draft.get("start_date"):
        ok, code = validate_dates(draft)
        if not ok:
            date_err = code
            if code == "BAD_RANGE":
                draft.pop("end_date", None)

    missing = get_missing_slots(
        draft, policy=policy, extraction=extraction, date_error=date_err
    )

    if not missing:
        apply_leave_draft_defaults(draft, policy)
        wf = mark_review_pending(wf, draft)
        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(draft),
            "complete": False,
            "confirmed_submit": False,
            "question": build_confirmation_prompt(draft),
        }

    clarify = str(draft.get("_pending_reason_clarify") or "").strip()
    if clarify:
        wf = apply_leave_state(
            wf,
            draft=draft,
            step=pending_slot or (missing[0] if missing else None),
            status=STATUS_ACTIVE,
            review_pending=False,
        )
        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(draft),
            "complete": False,
            "confirmed_submit": False,
            "question": f"{clarify}\n\n_(ছুটি আবেদন — নিচে উত্তর দিন)_",
        }

    slot = missing[0]
    if slot == SLOT_REASON:
        from chat.services.leave.workflow_schema import mark_reason_asked

        mark_reason_asked(draft)
    wf = apply_leave_state(
        wf,
        draft=draft,
        step=slot,
        status=STATUS_ACTIVE,
        review_pending=False,
    )
    question = generate_question(
        slot,
        draft,
        remaining=len(missing),
        date_error=date_err if slot == SLOT_DATES else None,
        extraction=extraction,
        missing=missing,
    )
    return {
        "workflow_state": wf,
        "merged_entities": build_merged_entities_for_engine(draft),
        "complete": False,
        "confirmed_submit": False,
        "question": question,
    }
