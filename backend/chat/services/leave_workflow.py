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
    extract_leave_slots,
    merge_llm_entities_into_extraction,
)
from chat.services.leave_slots import (
    SLOT_DATES,
    apply_wizard_answer,
    generate_question,
    get_missing_slots,
    prefill_draft_from_extraction,
    summarize_captured,
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
    "is_leave_paused",
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
    (r"\b(unpaid|lwop)\b|বেতন\s*ছাড়া", "unpaid"),
    (r"\b(maternity)\b|মাতৃত্ব", "maternity"),
    (r"\b(paternity)\b|পিতৃত্ব", "paternity"),
    (r"\b(emergency)\b|জরুরি", "emergency"),
    (r"\b(compensatory|comp\s*off)\b|কম্পেনসেটরি", "compensatory"),
)


def clone_workflow_state(state: dict[str, Any] | None) -> dict[str, Any]:
    return dict(state or {})


def is_leave_collecting(workflow_state: dict[str, Any] | None) -> bool:
    lr = (workflow_state or {}).get("leave_request") or {}
    return bool(lr.get("active"))


def is_leave_paused(workflow_state: dict[str, Any] | None) -> bool:
    lr = (workflow_state or {}).get("leave_request") or {}
    return bool(lr.get("paused")) and not bool(lr.get("active"))


def pause_leave_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = clone_workflow_state(workflow_state)
    lr = wf.get("leave_request")
    if not lr:
        return wf
    lr = dict(lr)
    lr["active"] = False
    lr["paused"] = True
    wf["leave_request"] = lr
    return wf


def resume_leave_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = clone_workflow_state(workflow_state)
    lr = wf.get("leave_request")
    if not lr:
        return wf
    lr = dict(lr)
    lr["active"] = True
    lr["paused"] = False
    wf["leave_request"] = lr
    return wf


def pending_question(workflow_state: dict[str, Any] | None) -> str | None:
    if not is_leave_collecting(workflow_state):
        return None
    block = (workflow_state or {}).get("leave_request") or {}
    draft = dict(block.get("draft") or {})
    missing = get_missing_slots(draft)
    if not missing:
        return None
    return generate_question(missing[0], draft, remaining=len(missing))


def pending_step(workflow_state: dict[str, Any] | None) -> str | None:
    if not is_leave_collecting(workflow_state):
        return None
    block = (workflow_state or {}).get("leave_request") or {}
    return block.get("pending_slot") or _first_missing_step(dict(block.get("draft") or {}))


def deactivate_leave_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = clone_workflow_state(workflow_state)
    wf.pop("leave_request", None)
    return wf


def _infer_payment_category(message: str, draft: dict[str, Any]) -> None:
    low = message.lower().strip()
    if draft.get("leave_payment_category"):
        return
    if re.search(r"\b(lwop|unpaid)\b|বেতন\s*ছাড়া", low):
        draft["leave_payment_category"] = LEAVE_PAYMENT_LWOP
    elif re.search(r"\bpaid\b|বেতনসহ", low):
        draft["leave_payment_category"] = LEAVE_PAYMENT_PAID


def _infer_leave_type(message: str, draft: dict[str, Any]) -> None:
    if draft.get("leave_type"):
        return
    low = message.lower()
    for pattern, code in _LEAVE_TYPE_PATTERNS:
        if re.search(pattern, low, re.I):
            draft["leave_type"] = code
            if code == "unpaid":
                draft.setdefault("leave_payment_category", LEAVE_PAYMENT_LWOP)
            else:
                draft.setdefault("leave_payment_category", LEAVE_PAYMENT_PAID)
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
    m = message.strip()
    if len(m) < 4:
        return None
    low = m.lower().strip()
    if re.match(r"^(paid|lwop|unpaid|full|half)\b", low) and len(m.split()) <= 3:
        return None
    stripped = re.sub(
        r"^(paid|lwop|unpaid|full|half)\b[\s,:-]*", "", low, flags=re.I
    ).strip(" ,.:;-")
    return stripped[:2000] if len(stripped) >= 4 else None


def merge_extractor_entities(draft: dict[str, Any], entities: dict[str, Any]) -> None:
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
    if rs and str(rs).strip() and not draft.get("reason"):
        draft["reason"] = str(rs).strip()[:2000]


def _first_missing_step(draft: dict[str, Any]) -> str | None:
    missing = get_missing_slots(draft)
    return missing[0] if missing else None


def _is_direct_slot_answer(message: str, pending_slot: str | None) -> bool:
    if not pending_slot:
        return False
    t = message.strip().lower()
    if pending_slot == "leave_type":
        if t in {"paid", "unpaid", "lwop", "full", "half"}:
            return False
        return len(t) >= 2
    if pending_slot == "leave_payment_category":
        return t in {"paid", "unpaid", "lwop"} or bool(re.match(r"^(paid|বেতন)", t))
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
    wf = clone_workflow_state(workflow_state)
    block = wf.setdefault("leave_request", {})
    block["active"] = True
    draft = dict(block.get("draft") or {})
    draft["_last_user_message"] = message
    pending_slot = block.get("pending_slot")
    policy = get_company_leave_policy(company_id or "default")
    extraction = extract_leave_slots(message, skip_leave_phrase_gate=True)

    if pending_slot and _is_direct_slot_answer(message, pending_slot):
        apply_wizard_answer(draft, pending_slot=pending_slot, message=message)
    else:
        ext = dict(entities)
        if extraction.leave_payment_category.confidence != "high":
            ext.pop("leave_payment_category", None)
        if extraction.day_scope.confidence != "high":
            ext.pop("day_scope", None)
        if extraction.reason.confidence != "high":
            ext.pop("reason", None)
            ext.pop("description", None)
        # Must merge the same sanitized overlay as prefill uses — merging full
        # `entities` let LLM hallucinated reason/description bypass slot gating
        # and auto-complete the wizard (duplicate flows then lost active state).
        merge_llm_entities_into_extraction(extraction, ext)
        prefill_draft_from_extraction(draft, extraction, external_entities=ext)
        _infer_leave_type(message, draft)
        _infer_payment_category(message, draft)
        _infer_day_scope(message, draft)

    normalize_end_equals_start_if_missing(draft)
    date_err: str | None = None
    if draft.get("start_date"):
        ok, code = validate_dates(draft)
        if not ok:
            date_err = code
            if code == "BAD_RANGE":
                draft.pop("end_date", None)
            # Keep start/end on IN_PAST so the user sees what was parsed; slot layer
            # still asks for a corrected date via date_error + SLOT_DATES.

    missing = get_missing_slots(
        draft, policy=policy, extraction=extraction, date_error=date_err
    )

    if not missing:
        # Apply tenant/policy defaults only once every required slot is explicit.
        apply_leave_draft_defaults(draft, policy)
        block.pop("pending_slot", None)
        block["draft"] = draft
        wf["leave_request"] = block
        wf["last_completed_leave_workflow"] = {
            "workflow_type": "leave_request",
            "completed": True,
            "slots_snapshot": {
                k: draft.get(k)
                for k in (
                    "start_date",
                    "end_date",
                    "leave_type",
                    "leave_payment_category",
                    "day_scope",
                    "reason",
                )
            },
        }
        wf = deactivate_leave_session(wf)
        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(draft),
            "complete": True,
            "question": None,
        }

    slot = missing[0]
    block["pending_slot"] = slot
    block["draft"] = draft
    block["workflow_type"] = "leave_request"
    block["missing_slots"] = list(missing)
    block["completed"] = False
    wf["leave_request"] = block
    question = generate_question(
        slot,
        draft,
        remaining=len(missing),
        date_error=date_err if slot == SLOT_DATES else None,
        extraction=extraction,
    )
    ack = summarize_captured(draft)
    if ack and slot not in (SLOT_DATES, "supporting_document"):
        question = ack + "\n\n" + question

    return {
        "workflow_state": wf,
        "merged_entities": build_merged_entities_for_engine(draft),
        "complete": False,
        "question": question,
    }
