"""Leave review / confirmation gate — no CRM submit until explicit user yes."""



from __future__ import annotations



import re

from typing import Any



from chat.services.leave_fsm import (
    KEY_EDIT_SNAPSHOT,
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

SLOT_EDIT_MENU = "edit_menu"



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

        "• **edit** — কোনো তথ্য বদলান (তারিখ, paid/unpaid, পুরো/হাফ দিন, কারণ)\n"

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





def parse_edit_field_choice(message: str) -> str | None:
    """Map user text to a slot id (edit menu or phrases like ``date`` / ``তারিখ``)."""
    low = (message or "").strip().lower()
    if not low:
        return None
    for pattern, slot in _FIELD_ALIASES:
        if re.search(pattern, low, re.I):
            return slot
    if re.search(r"^(tarikh|tarik|date|dates|din|day|days)$", low):
        return SLOT_DATES
    if re.search(r"^(paid|unpaid|payment|salary|select\s*leave)$", low):
        return SLOT_PAYMENT
    if re.search(r"^(reason|why|cause|kar[oa]n)$", low):
        return SLOT_REASON
    if re.search(r"^(full|half|scope|duration)$", low):
        return SLOT_SCOPE
    if re.search(r"^(type|leave\s*type|sick|casual|annual)$", low):
        return SLOT_LEAVE_TYPE
    return None


def parse_edit_slot(message: str) -> str | None:
    if not _EDIT_RE.search(message or ""):
        return None
    return parse_edit_field_choice(message)


_EDIT_ABORT_RE = re.compile(
    r"(?:"
    r"edit\s*(?:korbo\s*)?na|edit\s*chai\s*na|edit\s*nah|na\s*edit|"
    r"edit\s*bondho|edit\s*cancel|cancel\s*edit|"
    r"back\s*to\s*review|^(?:back|nevermind|never\s*mind)$|"
    r"ager\s*(?:ta|gula|summary)|আগের\s*(?:টা|তথ্য|সারাংশ)|"
    r"বদলাব\s*না|বদলাতে\s*চাই\s*না|edit\s*korbo\s*nah|"
    r"ঠিক\s*আছে.*(?:edit|বদল)|(?:edit|বদল).*ঠিক\s*আছে"
    r")",
    re.I | re.UNICODE,
)


def is_edit_abort(message: str) -> bool:
    """User leaves edit mode and wants the previous review summary back."""
    t = (message or "").strip()
    if not t:
        return False
    if is_confirmation_yes(t) or is_confirmation_cancel(t):
        return False
    return bool(_EDIT_ABORT_RE.search(t))


def wants_navigate_back_to_leave_review(message: str) -> bool:
    """Return to the leave review screen (not answering the current slot)."""
    t = (message or "").strip()
    if not t:
        return False
    if wants_resume_or_show_expense(t):
        return False
    from chat.services.workflow_suspend import wants_resume_suspended_leave

    return wants_resume_suspended_leave(t) or is_edit_abort(t)


def try_apply_inline_edit_value(
    draft: dict[str, Any], slot: str, message: str
) -> bool:
    """Apply half/full, paid/unpaid, or reason text when included with the field pick."""
    from chat.services.leave_workflow import (
        _force_scope_from_message,
        _infer_payment_category,
        _reason_from_message,
    )

    raw = (message or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if slot == SLOT_SCOPE:
        return _force_scope_from_message(raw, draft)
    if slot in (SLOT_PAYMENT, SLOT_LEAVE_TYPE):
        before = draft.get("leave_payment_category")
        _infer_payment_category(raw, draft, force=True)
        return bool(draft.get("leave_payment_category")) and (
            draft.get("leave_payment_category") != before
            or before is None
        )
    if slot == SLOT_REASON:
        if low in {"reason", "কারণ", "kar", "keno", "why", "cause"}:
            return False
        reason = _reason_from_message(raw)
        if reason and len(reason.strip()) >= 3:
            draft["reason"] = reason.strip()
            draft.pop("_reason_implied", None)
            return True
        cleaned = re.sub(
            r"^(?:reason|কারণ|kar[oa]n)\s*[:,-]?\s*",
            "",
            raw,
            flags=re.I,
        ).strip()
        if len(cleaned) >= 4:
            draft["reason"] = cleaned
            draft.pop("_reason_implied", None)
            return True
    return False


def wants_resume_or_show_expense(message: str) -> bool:
    from chat.services.expense_workflow import wants_resume_or_show_expense as _w

    return _w(message)


def _finish_edit_return_review(
    workflow_state: dict[str, Any], draft: dict[str, Any]
) -> dict[str, Any]:
    from chat.services.leave_fsm import mark_review_pending
    from chat.services.leave_workflow import build_merged_entities_for_engine

    wf = normalize_workflow_state(workflow_state)
    wf.pop(KEY_EDIT_SNAPSHOT, None)
    d = dict(draft)
    wf = mark_review_pending(wf, d)
    return {
        "workflow_state": wf,
        "merged_entities": build_merged_entities_for_engine(d),
        "complete": False,
        "confirmed_submit": False,
        "question": build_confirmation_prompt(d),
        "cancelled": False,
    }


def build_edit_field_menu_prompt(draft: dict[str, Any], *, message: str = "") -> str:
    from chat.services.translator import detect_user_language

    lang = detect_user_language(message)
    select_leave = format_select_leave_label(draft)
    scope = str(draft.get("day_scope") or "—")
    start = str(draft.get("start_date") or "—")
    end = str(draft.get("end_date") or start)
    reason = str(draft.get("reason") or "—")
    scope_label = "পুরো দিন" if scope == "full" else "হাফ দিন" if scope == "half" else scope
    date_line = start if end == start else f"{start} → {end}"

    if lang == "bn":
        return (
            "ঠিক আছে — **কোন তথ্য বদলাতে** চান? নিচের যেকোনোটা লিখুন:\n\n"
            "• **তারিখ** — `date` / `তারিখ`\n"
            "• **Select Leave (paid/unpaid)** — `payment` / `paid`\n"
            "• **পুরো বা হাফ দিন** — `scope` / `full` / `half`\n"
            "• **কারণ** — `reason` / `কারণ`\n\n"
            f"**এখন যা আছে:** তারিখ {date_line} · {select_leave} · {scope_label} · কারণ: {reason}\n\n"
            "বদলাতে চান না হলে **`back`** বা **`edit korbo na`** লিখুন — "
            "আগের পর্যালোচনায় ফিরে যাবেন।"
        )
    if lang == "banglish":
        return (
            "Thik ache — **kon field change** korte chan? Ekta likhun:\n\n"
            "• **date** / tarikh\n"
            "• **payment** / paid / unpaid\n"
            "• **scope** / full / half\n"
            "• **reason**\n\n"
            f"Ekhon: {date_line} · {select_leave} · {scope_label} · reason: {reason}\n\n"
            "Change na chaile **`back`** ba **`edit korbo na`** — review e fire jaben."
        )
    return (
        "Sure — **which field would you like to change?** Reply with one of:\n\n"
        "• **date(s)** — `date`\n"
        "• **paid / unpaid** — `payment`\n"
        "• **full or half day** — `scope`\n"
        "• **reason** — `reason`\n\n"
        f"**Current:** {date_line} · {select_leave} · {scope_label} · reason: {reason}\n\n"
        "To keep everything as-is, reply **`back`** or **`no edit`** — "
        "I'll show your review again."
    )


def restore_leave_review_from_edit(
    workflow_state: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Restore draft + review screen after edit menu or mid-slot abort."""
    return _restore_edit_snapshot(workflow_state)


def _restore_edit_snapshot(workflow_state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    wf = normalize_workflow_state(workflow_state)
    snap = dict(wf.get(KEY_EDIT_SNAPSHOT) or read_leave_state(wf).get("draft") or {})
    wf.pop(KEY_EDIT_SNAPSHOT, None)
    wf = apply_leave_state(
        wf, draft=snap, step=None, status=STATUS_ACTIVE, review_pending=True
    )
    return wf, snap


def _begin_edit_slot(
    workflow_state: dict[str, Any],
    draft: dict[str, Any],
    slot: str,
) -> dict[str, Any]:
    from chat.services.leave_workflow import build_merged_entities_for_engine

    d = dict(draft)
    clear_slot_for_edit(d, slot)
    wf = normalize_workflow_state(workflow_state)
    snap = wf.get(KEY_EDIT_SNAPSHOT)
    wf = apply_leave_state(
        wf, draft=d, step=slot, status=STATUS_ACTIVE, review_pending=False
    )
    if snap:
        wf[KEY_EDIT_SNAPSHOT] = snap
    q = generate_question(slot, d, remaining=1)
    return {
        "workflow_state": wf,
        "merged_entities": build_merged_entities_for_engine(d),
        "complete": False,
        "confirmed_submit": False,
        "question": q,
        "cancelled": False,
    }


def _process_edit_menu_turn(
    *,
    workflow_state: dict[str, Any],
    message: str,
    draft: dict[str, Any],
) -> dict[str, Any]:
    from chat.services.leave_workflow import build_merged_entities_for_engine

    wf = normalize_workflow_state(workflow_state)
    d = dict(draft)

    if is_edit_abort(message):
        wf, snap = _restore_edit_snapshot(wf)
        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(snap),
            "complete": False,
            "confirmed_submit": False,
            "question": build_confirmation_prompt(snap),
            "cancelled": False,
        }

    slot = parse_edit_field_choice(message)
    if slot:
        pack = _begin_edit_slot(wf, d, slot)
        d2 = dict(read_leave_state(pack["workflow_state"]).get("draft") or d)
        if try_apply_inline_edit_value(d2, slot, message):
            return _finish_edit_return_review(pack["workflow_state"], d2)
        if patch_draft_from_message(d2, message, None):
            return _finish_edit_return_review(pack["workflow_state"], d2)
        return pack

    return {
        "workflow_state": wf,
        "merged_entities": build_merged_entities_for_engine(d),
        "complete": False,
        "confirmed_submit": False,
        "question": build_edit_field_menu_prompt(d, message=message),
        "cancelled": False,
    }





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

    from chat.services.wizard_turn_gate import is_leave_navigation_phrase

    if is_leave_navigation_phrase(message):
        return False

    from chat.services.intent_detector import (
        _strong_expense_claim,
        _strong_expense_day_summary,
    )

    if _strong_expense_claim(message) or _strong_expense_day_summary(message):
        return False

    from chat.services.wizard_turn_gate import looks_like_leave_review_update

    if looks_like_leave_review_update(message):
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

    if st.get("step") == SLOT_EDIT_MENU:
        return _process_edit_menu_turn(
            workflow_state=wf, message=message, draft=d
        )

    if wf.get(KEY_EDIT_SNAPSHOT) and wants_navigate_back_to_leave_review(message):
        wf, snap = _restore_edit_snapshot(wf)
        from chat.services.leave_workflow import build_merged_entities_for_engine

        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(snap),
            "complete": False,
            "confirmed_submit": False,
            "question": build_confirmation_prompt(snap),
            "cancelled": False,
        }

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

    from chat.services.wizard_turn_gate import is_leave_navigation_phrase

    if is_leave_navigation_phrase(message):
        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(d),
            "complete": False,
            "confirmed_submit": False,
            "question": build_confirmation_prompt(d),
            "cancelled": False,
        }

    edit_slot = parse_edit_slot(message)

    if edit_slot:
        return _begin_edit_slot(wf, d, edit_slot)

    if not is_confirmation_yes(message) and _EDIT_RE.search(message or ""):
        snap = dict(d)
        wf = apply_leave_state(
            wf,
            draft=d,
            step=SLOT_EDIT_MENU,
            status=STATUS_ACTIVE,
            review_pending=False,
        )
        wf[KEY_EDIT_SNAPSHOT] = snap
        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(d),
            "complete": False,
            "confirmed_submit": False,
            "question": build_edit_field_menu_prompt(d, message=message),
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


