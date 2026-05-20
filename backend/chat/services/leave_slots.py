"""
Dynamic slot-filling: missing-slot detection and natural question generation.
"""

from __future__ import annotations

from typing import Any

from chat.services.leave_draft_utils import supporting_document_needed
from chat.services.leave_policies import CompanyLeavePolicy
from chat.services.leave_slot_extraction import LeaveSlotExtraction

# Priority order for asking (only missing slots are asked)
SLOT_LEAVE_TYPE = "leave_type"
SLOT_PAYMENT = "leave_payment_category"
SLOT_SCOPE = "day_scope"
SLOT_DATES = "leave_dates"
SLOT_REASON = "reason"
SLOT_DOCUMENT = "supporting_document"
SLOT_DATE_CLARIFY = "date_clarification"

_SLOT_ASK_ORDER = (
    SLOT_DATE_CLARIFY,
    SLOT_DATES,
    SLOT_LEAVE_TYPE,
    SLOT_PAYMENT,
    SLOT_SCOPE,
    SLOT_REASON,
    SLOT_DOCUMENT,
)

_WIZ_MARKER = "_(ছুটি আবেদন — নিচে উত্তর দিন)_"


def prefill_draft_from_extraction(
    draft: dict[str, Any],
    extraction: LeaveSlotExtraction,
    *,
    external_entities: dict[str, Any] | None = None,
) -> None:
    """Apply high-confidence slots into workflow draft."""
    if external_entities:
        from chat.services.leave_workflow import merge_extractor_entities

        merge_extractor_entities(draft, external_entities)

    for field, slot_name in (
        ("leave_type", "leave_type"),
        ("start_date", "start_date"),
        ("end_date", "end_date"),
        ("days", "days"),
        ("leave_payment_category", "leave_payment_category"),
        ("day_scope", "day_scope"),
        ("reason", "reason"),
    ):
        sv = getattr(extraction, slot_name)
        if sv.confidence != "high" or sv.value is None:
            continue
        val = sv.value
        if field == "leave_payment_category":
            draft[field] = "paid" if val == "paid" else "lwop"
        elif field == "day_scope":
            draft[field] = "half" if val == "half" else "full"
        else:
            draft[field] = val
        if field == "reason" and sv.source.startswith("implied"):
            draft["_reason_implied"] = True

    if extraction.start_date.confidence == "high" and not draft.get("end_date"):
        draft["end_date"] = draft.get("start_date")

    if extraction.days.confidence == "high" and extraction.days.value:
        draft["days"] = extraction.days.value


def apply_wizard_answer(
    draft: dict[str, Any],
    *,
    pending_slot: str,
    message: str,
) -> None:
    """Parse a direct answer to the last asked slot (short replies like paid / full)."""
    from chat.services.leave_workflow import (
        _infer_day_scope,
        _infer_leave_type,
        _infer_payment_category,
        _reason_from_message,
    )

    msg = (message or "").strip()
    if pending_slot == SLOT_LEAVE_TYPE:
        _infer_leave_type(msg, draft)
    elif pending_slot == SLOT_PAYMENT:
        _infer_payment_category(msg, draft)
    elif pending_slot == SLOT_SCOPE:
        _infer_day_scope(msg, draft)
    elif pending_slot == SLOT_DATE_CLARIFY:
        from chat.services.leave_slot_extraction import extract_leave_slots

        ex = extract_leave_slots(msg, skip_leave_phrase_gate=True)
        prefill_draft_from_extraction(draft, ex, external_entities=None)
    elif pending_slot == SLOT_DATES:
        from chat.services.leave_slot_extraction import extract_leave_slots

        ex = extract_leave_slots(msg, skip_leave_phrase_gate=True)
        prefill_draft_from_extraction(draft, ex)
    elif pending_slot == SLOT_REASON:
        r = _reason_from_message(msg)
        if r:
            draft["reason"] = r
            draft.pop("_reason_implied", None)
    elif pending_slot == SLOT_DOCUMENT:
        if msg.lower() == "skip":
            draft["supporting_document_waived"] = True
        else:
            draft["document_text"] = msg[:2000]
    else:
        _infer_payment_category(msg, draft)
        _infer_day_scope(msg, draft)


def get_missing_slots(
    draft: dict[str, Any],
    *,
    policy: CompanyLeavePolicy | None = None,
    extraction: LeaveSlotExtraction | None = None,
    date_error: str | None = None,
) -> list[str]:
    """Return ordered list of slots still needed (dynamic, not fixed 1..5)."""
    missing: list[str] = []

    if extraction and extraction.vague_date and not draft.get("start_date"):
        missing.append(SLOT_DATE_CLARIFY)

    if date_error:
        missing.append(SLOT_DATES)
        return _order(missing)

    if not (draft.get("leave_type") or "").strip():
        missing.append(SLOT_LEAVE_TYPE)
    if not draft.get("leave_payment_category"):
        missing.append(SLOT_PAYMENT)
    if not draft.get("day_scope"):
        missing.append(SLOT_SCOPE)
    if not draft.get("start_date"):
        missing.append(SLOT_DATES)

    from chat.services.leave_draft_utils import (
        normalize_end_equals_start_if_missing,
        validate_dates,
    )

    normalize_end_equals_start_if_missing(draft)
    if draft.get("start_date"):
        ok, err = validate_dates(draft)
        if not ok and err:
            if SLOT_DATES not in missing:
                missing.append(SLOT_DATES)

    if not _reason_satisfied(draft):
        missing.append(SLOT_REASON)

    if supporting_document_needed(draft):
        if not draft.get("supporting_document_waived"):
            doc = str(draft.get("document_text") or "").strip()
            if not doc:
                missing.append(SLOT_DOCUMENT)

    return _order(missing)


def _order(slots: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for s in _SLOT_ASK_ORDER:
        if s in slots and s not in seen:
            out.append(s)
            seen.add(s)
    return out


def _reason_satisfied(draft: dict[str, Any]) -> bool:
    if len(str(draft.get("reason") or "").strip()) >= 4:
        return True
    if draft.get("_reason_implied"):
        return True
    lt = str(draft.get("leave_type") or "").lower()
    if lt in ("sick", "medical") and draft.get("start_date"):
        draft.setdefault("reason", "অসুস্থতা / sick leave")
        draft["_reason_implied"] = True
        return True
    return False


def generate_question(
    slot: str,
    draft: dict[str, Any],
    *,
    remaining: int,
    date_error: str | None = None,
    extraction: LeaveSlotExtraction | None = None,
) -> str:
    """Natural, contextual prompts — no rigid Question X/Y numbering."""
    lt = str(draft.get("leave_type") or "").strip()
    lt_bn = {
        "sick": "অসুস্থতার",
        "casual": "ক্যাজুয়াল",
        "annual": "বার্ষিক",
    }.get(lt, "")

    if slot == SLOT_DATE_CLARIFY and extraction and extraction.clarification_needed:
        return extraction.clarification_needed + _WIZ_MARKER

    if slot == SLOT_LEAVE_TYPE:
        return (
            "ছুটির **ধরন** কী?\n"
            "উদাহরণ: sick / casual / annual / emergency "
            "(বাংলা বা English — এক লাইনে)"
            + _WIZ_MARKER
        )

    if slot == SLOT_PAYMENT:
        head = f"আপনার {lt_bn} ছুটির জন্য — " if lt_bn else ""
        if draft.get("start_date"):
            head = f"**{draft['start_date']}** তারিখের ছুটির জন্য — "
        return (
            f"{head}এটা **বেতনসহ** নাকি **বেতন ছাড়া** চান?\n"
            "• বেতনসহ / paid\n"
            "• বেতন ছাড়া / unpaid"
            + _WIZ_MARKER
        )

    if slot == SLOT_SCOPE:
        return (
            "প্রতিদিন **পুরো দিন** নাকি **হাফ দিন** ছুটি?\n"
            "(full / half লিখলেও চলবে)"
            + _WIZ_MARKER
        )

    if slot == SLOT_DATES:
        if date_error == "IN_PAST":
            return (
                "আজকের আগের তারিখে ছুটি দেওয়া যাবে না। আজ বা পরের দিন দিন।"
                + _WIZ_MARKER
            )
        if date_error == "BAD_RANGE":
            return (
                "শেষ তারিখ যেন প্রথম তারিখের আগে না হয় — আবার লিখুন।"
                + _WIZ_MARKER
            )
        return (
            "**কোন তারিখ(গুলো)** ছুটি চান?\n"
            "• এক দিন: কাল / আগামীকাল / ২০২৬-০৫-১৫\n"
            "• একাধিক: ২০২৬-০৫-১২ থেকে ২০২৬-০৫-১৪"
            + _WIZ_MARKER
        )

    if slot == SLOT_REASON:
        return (
            "Reason টা এক লাইনে লিখুন।\n"
            "(যেমন: sick, family কাজ, travel — বাংলা/English যেকোনো)"
            + _WIZ_MARKER
        )

    if slot == SLOT_DOCUMENT:
        return (
            "এই ছুটির জন্য **ডাক্তারের চিট** বা কাগজ দিতে পারেন?\n"
            "আপলোড/পেস্ট করুন, অথবা এখন না হলে **skip** লিখুন — ম্যানেজার দেখবেন।"
            + _WIZ_MARKER
        )

    return "আর একটু তথ্য দরকার — নিচে লিখে পাঠান।" + _WIZ_MARKER


def summarize_captured(draft: dict[str, Any]) -> str:
    """Optional acknowledgment of what we already understood (for multi-slot turns)."""
    parts: list[str] = []
    if draft.get("leave_type"):
        parts.append(str(draft["leave_type"]))
    if draft.get("start_date"):
        parts.append(str(draft["start_date"]))
    if draft.get("leave_payment_category"):
        parts.append(str(draft["leave_payment_category"]))
    if not parts:
        return ""
    return " _(বুঝেছি: " + ", ".join(parts) + ")_"
