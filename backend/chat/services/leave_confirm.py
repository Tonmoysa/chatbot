"""Leave review / confirmation gate — no CRM submit until explicit user yes."""



from __future__ import annotations



import re

from typing import Any



from chat.services.leave_fsm import (

    STATUS_ACTIVE,

    apply_leave_state,

    clear_leave_flow,

    is_awaiting_leave_confirmation,

    normalize_workflow_state,

    read_leave_state,

)

from chat.services.leave_slots import (

    SLOT_DATES,

    SLOT_LEAVE_TYPE,

    SLOT_PAYMENT,

    SLOT_REASON,

    SLOT_SCOPE,

    generate_question,

)



_CONFIRM_YES_RE = re.compile(

    r"^(yes|y|yep|yeah|ok|okay|confirm|submit|done|correct|right|"

    r"হ্যাঁ|হ্যা|ঠিক\s*আছে|জমা\s*দিন|সাবমিট)([!.?\s]*)$",

    re.I,

)

_CONFIRM_CANCEL_RE = re.compile(

    r"^(no|cancel|stop|abort|quit|nope|nah|"

    r"না|বাতিল|বাদ|cancel\s*leave)([!.?\s]*)$",

    re.I,

)

_EDIT_RE = re.compile(

    r"\b(edit|change|update|fix|modify|correct)\b|"

    r"(বদল|পরিবর্তন|আপডেট|ঠিক\s*কর)",

    re.I,

)



_FIELD_ALIASES: tuple[tuple[str, str], ...] = (

    (r"\b(type|leave\s*type|category)\b|ধরন", SLOT_LEAVE_TYPE),

    (r"\b(paid|unpaid|payment|salary)\b|বেতন", SLOT_PAYMENT),

    (r"\b(half|full|scope|duration|day)\b|হাফ|পুরো", SLOT_SCOPE),

    (r"\b(date|dates|when|day)\b|তারিখ", SLOT_DATES),

    (r"\b(reason|why|cause)\b|কারণ", SLOT_REASON),

)





def build_leave_review_summary(draft: dict[str, Any]) -> str:

    lt = str(draft.get("leave_type") or "—")

    pay = str(draft.get("leave_payment_category") or "—")

    scope = str(draft.get("day_scope") or "—")

    start = str(draft.get("start_date") or "—")

    end = str(draft.get("end_date") or start)

    reason = str(draft.get("reason") or "—")

    pay_label = "বেতনসহ (paid)" if pay == "paid" else "বেতন ছাড়া (unpaid)" if pay == "lwop" else pay

    scope_label = "পুরো দিন" if scope == "full" else "হাফ দিন" if scope == "half" else scope

    lines = [

        "**ছুটি আবেদন — পর্যালোচনা**",

        f"• ধরন: {lt}",

        f"• {pay_label}",

        f"• {scope_label}",

        f"• তারিখ: {start}" + (f" → {end}" if end != start else ""),

        f"• কারণ: {reason}",

    ]

    if draft.get("document_text"):

        lines.append("• সংযুক্তি: আছে")

    return "\n".join(lines)





def build_confirmation_prompt(draft: dict[str, Any]) -> str:

    return (

        build_leave_review_summary(draft)

        + "\n\n"

        "জমা দেবেন?\n"

        "• **yes** — জমা দিন\n"

        "• **edit** — কোনো তথ্য বদলান (যেমন: edit date)\n"

        "• **cancel** — বাতিল"

    )





def is_confirmation_yes(message: str) -> bool:

    t = (message or "").strip()

    if not t:

        return False

    if _CONFIRM_YES_RE.match(t):

        return True

    return bool(re.search(r"\b(confirm|submit|ঠিক\s*আছে|হ্যাঁ)\b", t, re.I)) and not _EDIT_RE.search(t)





def is_confirmation_cancel(message: str) -> bool:

    t = (message or "").strip()

    if not t:

        return False

    if _CONFIRM_CANCEL_RE.match(t):

        return True

    return bool(re.search(r"\b(cancel|বাতিল|বাদ)\b", t, re.I)) and "edit" not in t.lower()





def parse_edit_slot(message: str) -> str | None:

    if not _EDIT_RE.search(message or ""):

        return None

    low = (message or "").lower()

    for pattern, slot in _FIELD_ALIASES:

        if re.search(pattern, low, re.I):

            return slot

    return None





def clear_slot_for_edit(draft: dict[str, Any], slot: str) -> None:

    if slot == SLOT_LEAVE_TYPE:

        draft.pop("leave_type", None)

    elif slot == SLOT_PAYMENT:

        draft.pop("leave_payment_category", None)

    elif slot == SLOT_SCOPE:

        draft.pop("day_scope", None)

    elif slot == SLOT_DATES:

        draft.pop("start_date", None)

        draft.pop("end_date", None)

        draft.pop("days", None)

    elif slot == SLOT_REASON:

        draft.pop("reason", None)

        draft.pop("_reason_implied", None)





def patch_draft_from_message(

    draft: dict[str, Any],

    message: str,

    entities: dict[str, Any] | None = None,

) -> bool:

    from chat.services.leave_workflow import _apply_slots_from_message



    before = (

        draft.get("leave_type"),

        draft.get("leave_payment_category"),

        draft.get("day_scope"),

        draft.get("start_date"),

        draft.get("reason"),

    )

    _apply_slots_from_message(draft, message, entities or {}, overwrite=True)

    after = (

        draft.get("leave_type"),

        draft.get("leave_payment_category"),

        draft.get("day_scope"),

        draft.get("start_date"),

        draft.get("reason"),

    )

    return before != after





def _looks_like_slot_correction(message: str) -> bool:

    low = (message or "").lower()

    if is_confirmation_yes(message) or is_confirmation_cancel(message):

        return False

    if parse_edit_slot(message):

        return False

    return bool(

        re.search(

            r"\b(paid|unpaid|lwop|sick|casual|annual|full|half|medical)\b|"

            r"full\s*day|half\s*day|হাফ|পুরো|বেতন",

            low,

            re.I,

        )

        or re.search(r"\bhobe\b|হবে", low)

    )





def process_confirmation_turn(

    *,

    workflow_state: dict[str, Any],

    message: str,

    draft: dict[str, Any] | None = None,

    entities: dict[str, Any] | None = None,

) -> dict[str, Any]:

    """Handle yes / edit / cancel at review. Terminal lock happens only after CRM submit."""

    from chat.services.leave_workflow import build_merged_entities_for_engine



    wf = normalize_workflow_state(workflow_state)

    st = read_leave_state(wf)

    d = dict(draft if draft is not None else st.get("draft") or {})



    if is_confirmation_cancel(message):

        wf = clear_leave_flow(wf)

        return {

            "workflow_state": wf,

            "merged_entities": build_merged_entities_for_engine(d),

            "complete": False,

            "confirmed_submit": False,

            "question": None,

            "cancelled": True,

        }



    edit_slot = parse_edit_slot(message)

    if edit_slot or (not is_confirmation_yes(message) and _EDIT_RE.search(message or "")):

        slot = edit_slot or SLOT_DATES

        clear_slot_for_edit(d, slot)

        wf = apply_leave_state(

            wf, draft=d, step=slot, status=STATUS_ACTIVE, review_pending=False

        )

        q = generate_question(slot, d, remaining=1)

        return {

            "workflow_state": wf,

            "merged_entities": build_merged_entities_for_engine(d),

            "complete": False,

            "confirmed_submit": False,

            "question": q,

            "cancelled": False,

        }



    if is_confirmation_yes(message):

        return {

            "workflow_state": wf,

            "merged_entities": build_merged_entities_for_engine(d),

            "complete": True,

            "confirmed_submit": True,

            "question": None,

            "cancelled": False,

        }



    if _looks_like_slot_correction(message):

        patch_draft_from_message(d, message, entities)

        wf = apply_leave_state(

            wf, draft=d, step=None, status=STATUS_ACTIVE, review_pending=True

        )

        return {

            "workflow_state": wf,

            "merged_entities": build_merged_entities_for_engine(d),

            "complete": False,

            "confirmed_submit": False,

            "question": build_confirmation_prompt(d),

            "cancelled": False,

            "draft_patched": True,

        }



    wf = apply_leave_state(

        wf, draft=d, step=None, status=STATUS_ACTIVE, review_pending=True

    )

    return {

        "workflow_state": wf,

        "merged_entities": build_merged_entities_for_engine(d),

        "complete": False,

        "confirmed_submit": False,

        "question": build_confirmation_prompt(d),

        "cancelled": False,

    }


