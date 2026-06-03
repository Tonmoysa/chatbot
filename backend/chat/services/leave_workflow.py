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
    merge_llm_entities_into_extraction,
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
    if is_awaiting_leave_confirmation(workflow_state):
        return build_confirmation_prompt(draft)
    missing = get_missing_slots(draft)
    if not missing:
        return None
    return generate_question(missing[0], draft, remaining=len(missing))


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
    if is_payment_only_message(message):
        return
    if draft.get("leave_type") and not explicit_leave_type_from_message(message):
        return
    low = message.lower()
    for pattern, code in _LEAVE_TYPE_PATTERNS:
        if re.search(pattern, low, re.I):
            draft["leave_type"] = code
            return


def _infer_day_scope(message: str, draft: dict[str, Any]) -> None:
    if draft.get("day_scope"):
        return
    low = message.lower()
    if re.search(r"\bhalf\b|হাফ|অর্ধ", low):
        draft["day_scope"] = DAY_SCOPE_HALF
    elif re.search(r"\bfull\b|পুরো", low):
        draft["day_scope"] = DAY_SCOPE_FULL


def _reason_from_message(message: str) -> str | None:
    from chat.services.leave_slot_extraction import extract_reason_from_message

    return extract_reason_from_message(message)


def merge_extractor_entities(
    draft: dict[str, Any],
    entities: dict[str, Any],
    *,
    overwrite: bool = False,
) -> None:
    """Patch draft from entities — by default never clobber filled slots."""
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
    ):
        v = entities.get(key)
        if v is None or v == "":
            continue
        if not overwrite and draft.get(key):
            continue
        if key == "leave_payment_category":
            s = str(v).strip().lower()
            draft["leave_payment_category"] = (
                LEAVE_PAYMENT_LWOP if s in {"lwop", "unpaid"} else LEAVE_PAYMENT_PAID
            )
            continue
        if key == "day_scope":
            s = str(v).strip().lower()
            draft["day_scope"] = DAY_SCOPE_HALF if s.startswith("half") else DAY_SCOPE_FULL
            continue
        if key == "date" and not draft.get("start_date"):
            draft["start_date"] = str(v).split("T")[0]
            continue
        draft[key] = v
    rs = entities.get("description")
    if rs and str(rs).strip() and (overwrite or not draft.get("reason")):
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
    if is_payment_only_message(message):
        _infer_payment_category(message, draft, force=True)
        _infer_day_scope(message, draft)
        return

    parts = [p.strip() for p in re.split(r"[,;]+", message) if p.strip()]
    if not parts:
        parts = [message]
    seen: set[str] = set()
    for part in parts:
        if part in seen:
            continue
        seen.add(part)
        ex = extract_leave_slots(part, skip_leave_phrase_gate=True)
        prefill_draft_from_extraction(
            draft, ex, external_entities=None, overwrite=overwrite
        )
        _infer_leave_type(part, draft)
        _infer_payment_category(part, draft)
        _infer_day_scope(part, draft)
    ext = dict(entities)
    ex_whole = extract_leave_slots(message, skip_leave_phrase_gate=True)
    if ex_whole.leave_payment_category.confidence != "high":
        ext.pop("leave_payment_category", None)
    if ex_whole.day_scope.confidence != "high":
        ext.pop("day_scope", None)
    if ex_whole.reason.confidence != "high":
        ext.pop("reason", None)
        ext.pop("description", None)
    merge_llm_entities_into_extraction(ex_whole, ext)
    slot_overwrite = overwrite or _is_compound_slot_message(message)
    prefill_draft_from_extraction(
        draft, ex_whole, external_entities=ext, overwrite=slot_overwrite
    )
    explicit_lt = explicit_leave_type_from_message(message)
    if explicit_lt:
        draft["leave_type"] = explicit_lt
    _infer_leave_type(message, draft)
    _infer_payment_category(message, draft)
    _infer_day_scope(message, draft)
    from chat.services.leave_slot_extraction import extract_reason_from_message

    reason = extract_reason_from_message(message)
    if reason and (overwrite or not draft.get("reason")):
        draft["reason"] = reason
        draft.pop("_reason_implied", None)
    if overwrite:
        _force_scope_from_message(message, draft)


def _force_scope_from_message(message: str, draft: dict[str, Any]) -> bool:
    """Overwrite day_scope when user clearly states half/full (review corrections)."""
    low = (message or "").lower()
    if re.search(r"\bhalf\b|হাফ|অর্ধ|half\s*day", low):
        draft["day_scope"] = DAY_SCOPE_HALF
        return True
    if re.search(r"\bfull\b|পুরো|full\s*day", low) and not re.search(
        r"\bhalf\b|হাফ", low
    ):
        draft["day_scope"] = DAY_SCOPE_FULL
        return True
    return False


def _is_direct_slot_answer(message: str, pending_slot: str | None) -> bool:
    if not pending_slot or _is_compound_slot_message(message):
        return False
    t = message.strip().lower()
    if pending_slot == "leave_type":
        return t in {"paid", "unpaid", "lwop"}
    if pending_slot == "leave_payment_category":
        return t in {"paid", "unpaid", "lwop"}
    if pending_slot == "day_scope":
        return t in {"full", "half"} or "full" in t or "half" in t
    if pending_slot == "supporting_document":
        return t == "skip" or len(t) > 8
    if pending_slot == "reason":
        return len(t) >= 4 and t not in {"paid", "unpaid", "full", "half"}
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
        "document_text": draft.get("document_text"),
    }
    if draft.get("supporting_document_waived"):
        out["supporting_document_waived"] = True
    return {k: v for k, v in out.items() if v is not None and v != ""}


def process_leave_turn(
    *,
    workflow_state: dict[str, Any],
    message: str,
    entities: dict[str, Any],
    company_id: str = "",
) -> dict[str, Any]:
    wf = normalize_workflow_state(workflow_state)
    st = read_leave_state(wf)
    if st.get("active_flow") != ACTIVE_FLOW_LEAVE:
        wf = apply_leave_state(
            wf, draft={}, step=None, status=STATUS_ACTIVE, review_pending=False
        )
        st = read_leave_state(wf)

    draft = deep_merge_draft(dict(st.get("draft") or {}), {})
    draft["_last_user_message"] = message

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
        )

    pending_slot = st.get("step")
    policy = get_company_leave_policy(company_id or "default")
    extraction = extract_leave_slots(message, skip_leave_phrase_gate=True)

    in_edit_flow = bool(wf.get(KEY_EDIT_SNAPSHOT)) or st.get("step") == SLOT_EDIT_MENU
    switch_slot = (
        parse_edit_field_choice(message)
        if in_edit_flow
        else None
    )
    if switch_slot and switch_slot != pending_slot:
        pack = _begin_edit_slot(wf, draft, switch_slot)
        d2 = dict(read_leave_state(pack["workflow_state"]).get("draft") or draft)
        if try_apply_inline_edit_value(d2, switch_slot, message):
            return _finish_edit_return_review(pack["workflow_state"], d2)
        return pack

    if pending_slot and try_apply_inline_edit_value(draft, pending_slot, message):
        pass
    elif pending_slot and _is_direct_slot_answer(message, pending_slot):
        apply_wizard_answer(draft, pending_slot=pending_slot, message=message)
    else:
        before = dict(draft)
        _apply_slots_from_message(draft, message, entities, overwrite=False)
        draft = deep_merge_draft(before, draft)

    normalize_end_equals_start_if_missing(draft)
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

    slot = missing[0]
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
    )
    return {
        "workflow_state": wf,
        "merged_entities": build_merged_entities_for_engine(draft),
        "complete": False,
        "confirmed_submit": False,
        "question": question,
    }
