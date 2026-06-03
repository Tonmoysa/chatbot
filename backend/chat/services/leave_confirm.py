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

from chat.services.leave_draft_utils import format_select_leave_label
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

_DEFER_EXPENSE_SUBMIT_RE = re.compile(
    r"(?:expense|খরচ|kharcha).{0,35}(?:age|আগে|first|prothom|before).{0,25}"
    r"(?:submit|joma|জমা|kor[eo]?|কর[ো]?)|"
    r"(?:age|আগে|first|prothom|before).{0,25}(?:submit|joma|জমা).{0,35}(?:expense|খরচ)|"
    r"expense\s+ta\s+age",
    re.I | re.UNICODE,
)

_DEFER_LEAVE_SUBMIT_RE = re.compile(
    r"(?:leave|ছুটি|chuti|chhuti).{0,35}(?:request|application|abedon|আবেদন)?"
    r".{0,20}(?:age|আগে|first|prothom|before).{0,25}"
    r"(?:submit|joma|জমা|kor[eo]?|কর[ো]?)|"
    r"(?:age|আগে|first|prothom|before).{0,25}(?:submit|joma|জমা).{0,35}"
    r"(?:leave|ছুটি|chuti|chhuti|request)|"
    r"leave\s+request\s+ta\s+age",
    re.I | re.UNICODE,
)



_FIELD_ALIASES: tuple[tuple[str, str], ...] = (

    (r"\b(type|leave\s*type|category)\b|ধরন", SLOT_LEAVE_TYPE),

    (r"\b(paid|unpaid|payment|salary)\b|বেতন", SLOT_PAYMENT),

    (r"\b(half|full|scope|duration|day)\b|হাফ|পুরো", SLOT_SCOPE),

    (r"\b(date|dates|when|day)\b|তারিখ", SLOT_DATES),

    (r"\b(reason|why|cause)\b|কারণ", SLOT_REASON),

)





def build_leave_review_summary(draft: dict[str, Any]) -> str:

    select_leave = format_select_leave_label(draft)

    scope = str(draft.get("day_scope") or "—")

    start = str(draft.get("start_date") or "—")

    end = str(draft.get("end_date") or start)

    reason = str(draft.get("reason") or "—")

    scope_label = "পুরো দিন" if scope == "full" else "হাফ দিন" if scope == "half" else scope

    lines = [

        "**ছুটি আবেদন — পর্যালোচনা**",

        f"• Select Leave: {select_leave}",

        f"• Leave Type: {scope_label}",

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


def build_deferred_leave_return_prompt(
    draft: dict[str, Any], *, message: str = ""
) -> str:
    """Professional review when user returns to a parked leave before submitting."""
    from chat.services.translator import detect_user_language

    lang = detect_user_language(message)
    body = build_confirmation_prompt(draft)
    if lang == "bn":
        intro = (
            "আপনার ছুটির আবেদন এখনো **জমা হয়নি**। নিচের তথ্যগুলো যাচাই করুন — "
            "সব ঠিক থাকলে **yes** লিখে জমা দিন; বদলাতে **edit** লিখুন।\n\n"
        )
    elif lang == "banglish":
        intro = (
            "Apnar leave request ekhono submit hoyni. Nicher info check korun — "
            "thik thakle **yes** diye submit korun; change korte **edit**.\n\n"
        )
    else:
        intro = (
            "Your leave request is **not submitted yet**. Please review the details below — "
            "reply **yes** to submit, or **edit** to change a field.\n\n"
        )
    return intro + body





def wants_defer_leave_for_expense_submit(message: str) -> bool:
    """User wants to finish a parked expense before confirming leave (not leave yes)."""
    t = (message or "").strip()
    if not t:
        return False
    if _DEFER_LEAVE_SUBMIT_RE.search(t):
        return False
    if _DEFER_EXPENSE_SUBMIT_RE.search(t):
        return True
    low = t.lower()
    return bool(
        re.search(r"\b(expense|খরচ)\b", low, re.I)
        and re.search(r"\b(submit|joma|জমা)\b", low, re.I)
        and re.search(r"\b(age|আগে|first|prothom|before|on)\s*(koro|kor|কর)\b", low, re.I)
        and not re.search(r"\b(leave|chuti|chhuti|ছুটি|request)\b", low, re.I)
    )


def wants_defer_expense_for_leave_submit(message: str) -> bool:
    """User wants to finish a parked leave (often submit) before continuing expense."""
    t = (message or "").strip()
    if not t:
        return False
    if _DEFER_EXPENSE_SUBMIT_RE.search(t):
        return False
    if _DEFER_LEAVE_SUBMIT_RE.search(t):
        return True
    low = t.lower()
    return bool(
        re.search(r"\b(leave|chuti|chhuti|request|ছুটি)\b", low, re.I)
        and re.search(r"\b(submit|joma|জমা)\b", low, re.I)
        and re.search(r"\b(age|আগে|first|prothom|before|on)\s*(koro|kor|কর)\b", low, re.I)
        and not re.search(r"\b(expense|খরচ|kharcha)\b", low, re.I)
    )


def is_confirmation_yes(message: str) -> bool:

    t = (message or "").strip()

    if not t:

        return False

    if wants_defer_leave_for_expense_submit(t):

        return False

    if wants_defer_expense_for_leave_submit(t):

        return False

    if _CONFIRM_YES_RE.match(t):

        return True

    if re.search(r"\b(expense|খরচ)\b", t, re.I) and re.search(
        r"\b(submit|joma|জমা)\b", t, re.I
    ):
        return False

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

    if wants_defer_expense_for_leave_submit(message):

        return False

    from chat.services.intent_detector import (
        _strong_expense_claim,
        _strong_expense_day_summary,
    )

    if _strong_expense_claim(message) or _strong_expense_day_summary(message):
        return False

    # Free-form reason updates often arrive at the confirmation screen
    # (users add context instead of typing "yes"). Treat long non-question text
    # as a slot correction so we can patch the draft (typically `reason`).
    t = (message or "").strip()
    if t and len(t) >= 12 and not t.endswith("?") and not re.search(
        r"^(can\s+i|what|why|how|when|where|which)\b", t, re.I
    ):
        return True

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

    if wants_defer_expense_for_leave_submit(message) and not is_confirmation_yes(message):

        wf = apply_leave_state(

            wf, draft=d, step=None, status=STATUS_ACTIVE, review_pending=True

        )

        return {

            "workflow_state": wf,

            "merged_entities": build_merged_entities_for_engine(d),

            "complete": False,

            "confirmed_submit": False,

            "question": build_deferred_leave_return_prompt(d, message=message),

            "cancelled": False,

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

        changed = patch_draft_from_message(d, message, entities)

        wf = apply_leave_state(

            wf, draft=d, step=None, status=STATUS_ACTIVE, review_pending=True

        )

        t = (message or "").strip()
        # Only auto-submit on "reason-like" corrections that strongly look like a leave
        # reason (e.g. travel/ceremony). Otherwise keep the explicit "yes" confirm step.
        reasonish = bool(
            re.search(
                r"\b(travel|village|ceremon|funeral|wedding)\b|"
                r"(অনুষ্ঠান|গ্রাম|বিয়ে|জানাজা)",
                t,
                re.I,
            )
        )
        if changed and reasonish and len(t) >= 12 and not t.endswith("?"):
            # Treat free-form corrections (typically adding a reason) as an implicit
            # confirmation to submit, matching legacy wizard behavior.
            return {
                "workflow_state": wf,
                "merged_entities": build_merged_entities_for_engine(d),
                "complete": True,
                "confirmed_submit": True,
                "question": None,
                "cancelled": False,
                "draft_patched": True,
            }

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


