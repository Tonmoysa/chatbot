"""
Session action memory — last bot action + timeline for meta / history answers.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense.expense_fsm import read_expense_block
from chat.services.expense_extraction import _CATEGORY_TOKEN

KEY_LAST_BOT_ACTION = "last_bot_action"
KEY_BOT_ACTION_LOG = "bot_action_log"
_MAX_ACTION_LOG = 8

_POST_SUBMIT_EDIT_VERB_RE = re.compile(
    r"\b(koro|kor|kore|dao|daw|de|din|update|change|correct|fix|modify|remove|delete|baad|bad|বাদ)\b",
    re.I | re.UNICODE,
)


def has_expense_submission_lock(workflow_state: dict[str, Any] | None) -> bool:
    """True when an expense batch was submitted in this session (no in-chat edits)."""
    wf = workflow_state or {}
    last_sub = wf.get("expense_last_submission") or {}
    if str(last_sub.get("reference_id") or "").strip():
        return True
    action = read_last_bot_action(wf)
    return action.get("action_type") == "expense_submitted" and bool(
        str(action.get("reference_id") or "").strip()
    )


def _submitted_expense_categories(workflow_state: dict[str, Any] | None) -> set[str]:
    from chat.services.expense_extraction import normalize_category

    wf = workflow_state or {}
    last_sub = wf.get("expense_last_submission") or {}
    action = read_last_bot_action(wf)
    items = list(last_sub.get("items") or action.get("items") or [])
    return {
        normalize_category(str(row.get("category") or ""))
        for row in items
        if str(row.get("category") or "").strip()
    }


_POST_SUBMIT_DRAFT_ACTIONS = frozenset(
    {
        "expense_line_added",
        "expense_corrected",
        "expense_pending_discarded",
        "expense_total_check",
        "expense_vague_add_prompt",
    }
)


def is_fresh_post_submit_expense_draft(
    workflow_state: dict[str, Any] | None,
    *,
    block: dict[str, Any] | None = None,
) -> bool:
    """
    True when the active expense_request is a new claim after CRM submit.

    N56 must only purge stale remnants (same lines as the last submission), not
    intentional post-submit drafts — otherwise day summaries hide pending lines.
    """
    wf = workflow_state or {}
    if not has_expense_submission_lock(wf):
        return False
    block = block if block is not None else read_expense_block(wf)
    from chat.services.expense.session_ledger import draft_line_rows_for_block

    rows = draft_line_rows_for_block(block)
    if not rows:
        return False
    if block.get("post_submit_draft"):
        return True

    action = read_last_bot_action(wf)
    if str(action.get("action_type") or "") in _POST_SUBMIT_DRAFT_ACTIONS:
        return True

    last_sub = wf.get("expense_last_submission") or {}
    sub_items = list(last_sub.get("items") or [])
    if not sub_items:
        return True

    from chat.services.expense.expense_draft_snapshots import items_fingerprint

    return items_fingerprint(rows) != items_fingerprint(sub_items)


def purge_stale_expense_draft_after_submit(
    workflow_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """Drop a leftover active draft once CRM submit is recorded."""
    from chat.services.expense.expense_fsm import deactivate_expense_session

    wf_in = workflow_state or {}
    if not has_expense_submission_lock(wf_in):
        return wf_in
    block = read_expense_block(wf_in)
    if not block.get("active") and not list(block.get("items") or []):
        return wf_in
    if is_fresh_post_submit_expense_draft(wf_in, block=block):
        return wf_in
    return deactivate_expense_session(wf_in)


_NEW_EXPENSE_CLAIM_MARKER_RE = re.compile(
    r"(new\s+expense|notun\s+expense|notun\s+kore|notun\s+ekta|aro\s+(?:ekta\s+)?expense|"
    r"arekta\s+expense|ar\s+ekta\s+expense|another\s+expense|notun\s+kharo?ch|নতুন\s*expense|নতুন\s*খরচ)",
    re.I | re.UNICODE,
)

# Fresh "incurred" verbs — reporting new spending, not editing an existing line.
_NEW_EXPENSE_CLAIM_VERB_RE = re.compile(
    r"\b(hoyeche|hoeche|hoise|hoyche|hoiche|holo|korechi|korlam|diyechi|legeche|legechilo|"
    r"cost\s+hoyeche|kharo?ch\s+hoyeche|হয়েছে|হইছে)\b",
    re.I | re.UNICODE,
)

# Explicit "add this as a new expense" intent (add koro / jog koro / expense e add).
_NEW_EXPENSE_ADD_RE = re.compile(
    r"(add\s+kor\w*|\bexpense\s*(?:e|te)\s*add\b|\bjog\s+kor\w*|যোগ\s*কর\w*)",
    re.I | re.UNICODE,
)


def looks_like_new_expense_claim_after_submit(message: str) -> bool:
    """
    True when a post-submit message opens a brand-new expense claim rather than
    editing the already-submitted batch — e.g.

      - ``amar ajke new expense hoyeche bus 100 taka``
      - ``bus 70 mirpur to motijheel then lunch 100 ... eta expense e add koro``

    Such claims must start a fresh workflow (duplicates of an already-submitted
    batch are allowed — a re-submit creates a new claim, not an in-chat edit).
    """
    from chat.services.expense.expense_confirm import looks_like_expense_correction
    from chat.services.expense_extraction import extract_expense_items

    raw = (message or "").strip()
    if not raw:
        return False
    # A targeted correction ("lunch ta 200 hobe", "bus baad dao") is an edit.
    if looks_like_expense_correction(raw):
        return False
    items = extract_expense_items(raw).items
    if not items:
        return False
    low = raw.lower()
    # A compound batch (2+ extracted lines) is a new claim, never a single edit.
    if len(items) >= 2:
        return True
    if _NEW_EXPENSE_CLAIM_MARKER_RE.search(low):
        return True
    if _NEW_EXPENSE_ADD_RE.search(low):
        return True
    if _NEW_EXPENSE_CLAIM_VERB_RE.search(low) and not _POST_SUBMIT_EDIT_VERB_RE.search(low):
        return True
    return False


def looks_like_post_submit_expense_modification(
    workflow_state: dict[str, Any] | None,
    message: str,
) -> bool:
    """Detect edits to a submitted batch (including ``lunch 200 taka koro``)."""
    from chat.services.expense.expense_confirm import looks_like_expense_correction
    from chat.services.expense_extraction import (
        _looks_like_route_answer,
        extract_expense_items,
        normalize_category,
    )

    raw = (message or "").strip()
    if not raw or not has_expense_submission_lock(workflow_state):
        return False
    if wants_post_submit_edit_question(raw):
        return False
    # A fresh "new expense" claim after submit is NOT a modification — it should
    # open a new expense workflow (the submitted batch stays locked/un-editable).
    if looks_like_new_expense_claim_after_submit(raw):
        return False

    wf = workflow_state or {}
    block = read_expense_block(wf)

    # In-progress new claim after CRM submit — route fills, review edits, submit.
    if block.get("active") and is_fresh_post_submit_expense_draft(wf, block=block):
        return False

    pending_step = str(block.get("pending_step") or "").strip()
    if block.get("active") and block.get("pending_line"):
        if pending_step == "from_to" and _looks_like_route_answer(raw):
            return False
        if is_fresh_post_submit_expense_draft(wf, block=block):
            return False

    if looks_like_expense_correction(raw):
        return True

    submitted_cats = _submitted_expense_categories(wf)
    if not submitted_cats:
        return False

    low = raw.lower()
    if _POST_SUBMIT_EDIT_VERB_RE.search(low):
        for cat in submitted_cats:
            if cat and re.search(rf"\b{re.escape(cat)}\b", low, re.I):
                return True

    ext = extract_expense_items(raw)
    for item in ext.items:
        cat = normalize_category(str(item.category or ""))
        if cat in submitted_cats:
            try:
                if float(item.amount or 0) > 0:
                    return True
            except (TypeError, ValueError):
                return True
    return False


def looks_like_submitted_expense_correction_attempt(
    workflow_state: dict[str, Any] | None,
    message: str,
) -> bool:
    """User tries to edit a submitted expense batch in chat."""
    if not has_expense_submission_lock(workflow_state):
        return False
    return looks_like_post_submit_expense_modification(workflow_state, message)


def format_submitted_expense_cancel_blocked_answer(
    workflow_state: dict[str, Any] | None,
    *,
    lang: str | None = None,
) -> str:
    """Explain that a submitted expense cannot be cancelled in chat."""
    wf = workflow_state or {}
    action = read_last_bot_action(wf)
    last_sub = wf.get("expense_last_submission") or {}
    ref = str(last_sub.get("reference_id") or action.get("reference_id") or "")
    if lang == "en":
        if ref:
            return (
                f"Expense `{ref}` is already **submitted** — it cannot be **cancelled** "
                "or edited in this chat.\n\n"
                "Please contact **CRM/Finance** for post-submit changes.\n"
                "To start a **new** claim, describe your expenses again."
            )
        return (
            "Your expense is already **submitted** — it cannot be **cancelled** "
            "or edited in this chat.\n\n"
            "Contact **CRM/Finance** for changes after submit."
        )
    if ref:
        return (
            f"Expense `{ref}` **ইতিমধ্যে জমা** হয়েছে — এই চ্যাট থেকে **বাতিল বা edit** "
            "করা যাবে না।\n\n"
            "পরিবর্তন চাইলে **CRM/Finance**-এর সাথে যোগাযোগ করুন।\n"
            "নতুন claim শুরু করতে আবার খরচের বিবরণ লিখুন।"
        )
    return (
        "Expense **ইতিমধ্যে জমা** হয়েছে — chat থেকে **বাতিল বা edit** করা যাবে না।\n"
        "পরিবর্তন চাইলে CRM/Finance-এর সাথে যোগাযোগ করুন।"
    )


def format_submitted_expense_edit_blocked_answer(
    workflow_state: dict[str, Any] | None,
    *,
    lang: str | None = None,
) -> str:
    """Explain that submitted expenses cannot be edited in chat."""
    wf = workflow_state or {}
    action = read_last_bot_action(wf)
    last_sub = wf.get("expense_last_submission") or {}
    ref = str(last_sub.get("reference_id") or action.get("reference_id") or "")
    if lang == "en":
        if ref:
            return (
                f"Expense `{ref}` is already **submitted** — it cannot be edited in chat.\n\n"
                "Contact **CRM/Finance** for post-submit changes."
            )
        return (
            "Your expense is already **submitted** — it cannot be edited in chat.\n\n"
            "Contact **CRM/Finance** for post-submit changes."
        )
    if ref:
        return (
            f"Expense `{ref}` **submit** হয়ে গেছে — chat-এ আর **edit করা যায় না**।\n\n"
            "পরিবর্তন চাইলে **CRM/Finance**-এর সাথে যোগাযোগ করুন।"
        )
    return (
        "Expense **submit** হয়ে গেছে — chat-এ আর **edit করা যায় না**।\n"
        "পরিবর্তন চাইলে CRM/Finance-এর সাথে যোগাযোগ করুন।"
    )


_SUBMIT_TIMING_QUESTION_RE = re.compile(
    r"(?:"
    r"\b(?:can|could|may)\s+(?:i|we)\b.{0,35}\bsubmit\b"
    r"|"
    r"\b(?:do|should)\s+(?:i|we)\s+(?:need\s+to|have\s+to)\s+submit\b"
    r"|"
    r"\b(?:must|need|have\s+to|required)\b.{0,20}\bsubmit\b"
    r"|"
    r"(?:pore|por|later|letter|tomorrow|ekhon|akhon|now).{0,25}(?:submit|জমা|joma)"
    r"|"
    r"(?:submit|জমা|joma).{0,25}(?:pore|por|later|letter|tomorrow|ekhon|akhon|now|পরে|পর)"
    r")",
    re.I | re.UNICODE,
)


def wants_expense_submit_timing_question(message: str) -> bool:
    """User asks whether/when they must submit (draft still open — not a status lookup)."""
    raw = (message or "").strip()
    if not raw:
        return False
    from chat.services.expense.wizard_commands import wants_expense_submit_command

    if wants_expense_submit_command(raw) and re.search(
        r"\b(koro|kor|daw|dao|de|debo|din|den|kore\s*daw|kore\s*de)\b",
        raw,
        re.I | re.UNICODE,
    ):
        return False
    if wants_post_submit_edit_question(raw):
        return False
    low = raw.lower()
    if not re.search(r"\bsubmit\b", low) and not re.search(r"(জমা|joma)", raw, re.I | re.UNICODE):
        return False
    if _SUBMIT_TIMING_QUESTION_RE.search(raw):
        return True
    if re.search(r"\b(?:can|could|may)\b", low) and re.search(
        r"\b(?:later|letter|now|pore|por|tomorrow|must|need)\b", low
    ):
        return True
    return bool(
        re.search(r"\bsubmit\b\s*\?", low)
        and re.search(r"\b(?:can|could|later|letter|now|pore|when)\b", low)
    )


def wants_expense_draft_persistence_question(message: str) -> bool:
    """User asks if the in-session draft will be kept for later."""
    raw = (message or "").strip()
    if not raw:
        return False
    low = raw.lower()
    return bool(
        re.search(r"\b(?:save|saved|keep|kept|retain|persist|thakbe|thakbe)\b", low)
        and re.search(r"\b(?:draft|session|later|pore|tomorrow|chat)\b", low)
    ) or bool(
        re.search(r"(draft|খসড়া|ড্রাফট).{0,25}(save|thakbe|থাকবে|রাখবে)", raw, re.I | re.UNICODE)
    )


def wants_post_submit_edit_question(message: str) -> bool:
    """User asks whether they can edit after CRM submit (policy/meta, not a draft edit)."""
    raw = (message or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if not (
        re.search(r"\b(edit|change|update|correct|fix|modify)\b", low)
        or re.search(r"(এডিট|সংশোধন|পরিবর্তন|ঠিক\s*কর)", raw, re.I | re.UNICODE)
    ):
        return False
    if re.search(r"\b(?:before|age|prothom|first)\b", low) and re.search(
        r"\bsubmit\b", low
    ):
        return False
    return bool(
        re.search(r"\bafter\s+submit\b", low)
        or re.search(r"\bsubmitted\b", low)
        or re.search(r"submit\s*(?:korar|korer|er)\s*por", low)
        or re.search(r"submit\s*(?:korar|korer|er)\s*pare", low)
        or re.search(r"জমা.{0,20}(?:পর|পরে)", raw, re.I | re.UNICODE)
        or re.search(r"\bsubmit\b", low)
    )


def wants_expense_pre_submit_review(message: str) -> bool:
    """User wants to see expense draft/review before submitting."""
    raw = (message or "").strip()
    if not raw:
        return False
    if not re.search(r"\b(expense|খরচ|report)\b", raw, re.I) and "খরচ" not in raw:
        return False
    return bool(
        re.search(
            r"(?:submit|জমা|joma|report).{0,40}(?:age|আগে|before|prior)",
            raw,
            re.I | re.UNICODE,
        )
        or re.search(
            r"(?:age|আগে|before).{0,40}(?:submit|জমা|report|দেখাও|dekhao|দেখ)",
            raw,
            re.I | re.UNICODE,
        )
        or re.search(
            r"(?:expense|খরচ|report).{0,30}(?:আরেকবার|abar|again).{0,25}"
            r"(?:দেখাও|dekhao|দেখ|review)",
            raw,
            re.I | re.UNICODE,
        )
    )


_EXPENSE_SUBMIT_STATUS_RE = re.compile(
    r"(?:"
    r"(?:ki|kono|any).{0,35}(?:expense|খরচ|kharcha|khoroch).{0,35}"
    r"(?:submit|joma|জমা).{0,25}(?:korechi|korchi|kor[eo]chi|hoyeche|hoise|done|হয়েছে|হয়েছে)"
    r"|"
    r"(?:expense|খরচ|kharcha|khoroch).{0,35}"
    r"(?:submit|joma|জমা).{0,25}(?:korechi|korchi|kor[eo]chi|hoyeche|hoise|done|হয়েছে|হয়েছে)"
    r"|"
    r"(?:submit|joma|জমা).{0,20}(?:hoyeche|hoise|হয়েছে|হয়েছে).{0,25}(?:expense|খরচ)"
    r"|"
    r"(?:amar|my).{0,20}(?:expense|খরচ|kharcha).{0,25}(?:submit|joma|জমা).{0,15}(?:hoyeche|hoise|হয়েছে)"
    r")",
    re.I | re.UNICODE,
)


def wants_expense_submission_status(message: str) -> bool:
    """User asks whether expense was already submitted in this session."""
    raw = (message or "").strip()
    if not raw:
        return False
    if re.search(r"\b(leave|chuti|chhuti|ছুটি)\b", raw, re.I | re.UNICODE):
        return False
    if wants_expense_pre_submit_review(message):
        return False
    return bool(_EXPENSE_SUBMIT_STATUS_RE.search(raw))


def wants_expense_meta_question(message: str) -> bool:
    """User asks about the bot's last expense action (not a new claim line)."""
    raw = (message or "").strip()
    if not raw:
        return False
    if wants_expense_submission_status(message):
        return True
    if wants_expense_pre_submit_review(message):
        return True
    from chat.services.expense.expense_total_dispute import is_expense_total_check_query

    if is_expense_total_check_query(message):
        return False
    if wants_post_submit_edit_question(message):
        return True
    if wants_expense_submit_timing_question(message):
        return True
    if wants_expense_draft_persistence_question(message):
        return True
    from chat.services.hr_signal import message_looks_like_expense_status_query

    if message_looks_like_expense_status_query(raw):
        return True
    low = raw.lower()
    if wants_expense_history_query(message):
        return False
    if re.search(
        r"(?:"
        r"ki\s+add\s+kor(?:cho|ci|bo|ben)|"
        r"what\s+(?:did\s+you|are\s+you)\s+add|"
        r"what\s+did\s+you\s+do|"
        r"ki\s+kor(?:cho|ci|teso|tes|bo)|"
        r"what\s+are\s+you\s+doing|"
        r"submit\s+(?:hoise|hoyeche|kora\s+hoise|done)\s*ki|"
        r"submit\s+kora\s+hoise\s*ki|"
        r"submitted\s+yet|"
        r"ki\s+add\s+kor(?:la|le|silam)|"
        r"tumi\s+ki\s+add\s+kor|"
        r"apni\s+ki\s+add\s+kor"
        r")",
        low,
    ):
        return True
    if re.search(r"(কি\s+add\s+কর|কি\s+যোগ\s+কর|কি\s+কর\s*ছ)", raw):
        return True
    if re.search(
        r"(কত|মোট).{0,20}(কস্ট|খরচ|expense).{0,25}(এড|add|যোগ|করছি|korchi)",
        raw,
        re.I | re.UNICODE,
    ):
        return True
    if re.search(
        r"\b(expense|kharcha|khoroch|reimbursement|reimburse|claim|summary|summery|draft|pending)\b",
        low,
    ) or re.search(r"(খরচ|expense)", raw, re.I):
        if re.search(
            r"\b(keno|why|kothai|kothay|kothao|koi|where|missing|shudhu|only|just|incomplete)\b",
            low,
        ) or re.search(r"\bbaki\s+(expense|line|gula|kharcha)\b", low):
            return True
    if re.search(r"\b(ki|what)\b", low) and re.search(
        r"\b(add|adding|added|jog|correction|correct|change|update)\b", low
    ):
        if re.search(r"\b(expense|kharcha|khoroch|draft|line)\b", low) or re.search(
            r"(খরচ|expense)", raw, re.I
        ):
            return True
    return False


def wants_expense_history_query(message: str) -> bool:
    """Explicit session expense history (not just today's total)."""
    raw = message or ""
    low = raw.lower()
    if not re.search(r"\b(expense|reimbursement|claim|kharcha|khoroch)\b", low) and not re.search(
        r"(খরচ|expense)", raw, re.I
    ):
        return False
    return bool(
        re.search(r"\b(history|histori|record|timeline|log)\b", low)
        or re.search(r"(ইতিহাস|হিস্টোরি|history|record)", raw, re.I)
        or re.search(r"(ajker|ajke|today).{0,20}expense.{0,20}history", low)
    )


def is_vague_expense_add(message: str) -> bool:
    """Bare 'add koro' with no amount or category."""
    raw = (message or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if not (
        re.search(r"\b(add|adding|jog|যোগ)\b", low)
        or re.search(r"add\s*koro", low)
        or re.search(r"aro\s+add", low)
        or re.search(r"more\s+add", low)
    ):
        return False
    if re.search(r"\d", raw):
        return False
    if re.search(rf"\b({_CATEGORY_TOKEN})\b", raw, re.I):
        return False
    words = re.findall(r"\S+", raw)
    return len(words) <= 6


def vague_add_clarification(*, lang: str | None = None) -> str:
    if lang == "en":
        return (
            "What should I add? Please include **category and amount**, e.g. "
            "**lunch 200**, **bus 50 mirpur to motijheel**."
        )
    if lang == "banglish":
        return (
            "Ki add korbo? **Category + amount** din, e.g. **lunch 200**, "
            "**bus 50 mirpur to motijheel**."
        )
    return (
        "কি add করব? **Category + amount** লিখুন, যেমন: **lunch 200**, "
        "**bus 50 mirpur to motijheel**।"
    )


def _action_log(wf: dict[str, Any]) -> list[dict[str, Any]]:
    log = wf.get(KEY_BOT_ACTION_LOG)
    return list(log) if isinstance(log, list) else []


def record_bot_action(
    workflow_state: dict[str, Any],
    *,
    action_type: str,
    summary: str,
    items: list[dict[str, Any]] | None = None,
    reference_id: str = "",
    total: float | None = None,
    stage: str = "",
    incurred_date_iso: str = "",
) -> dict[str, Any]:
    wf = dict(workflow_state or {})
    entry: dict[str, Any] = {
        "action_type": str(action_type or "").strip(),
        "summary": str(summary or "").strip(),
    }
    if items:
        entry["items"] = [dict(x) for x in items]
    if reference_id:
        entry["reference_id"] = reference_id
    if total is not None:
        entry["total"] = float(total)
    if stage:
        entry["stage"] = stage
    if incurred_date_iso:
        entry["incurred_date_iso"] = incurred_date_iso
    wf[KEY_LAST_BOT_ACTION] = entry
    log = _action_log(wf)
    log.append(dict(entry))
    wf[KEY_BOT_ACTION_LOG] = log[-_MAX_ACTION_LOG:]
    return wf


def record_expense_submitted(
    workflow_state: dict[str, Any],
    *,
    items: list[dict[str, Any]],
    reference_id: str,
    incurred_date_iso: str = "",
) -> dict[str, Any]:
    total = sum(float(x.get("amount") or 0) for x in items)
    n = len(items)
    summary = (
        f"Submitted expense {reference_id}: {n} line(s), {total:g} Tk "
        f"for {incurred_date_iso or 'today'}."
    )
    return record_bot_action(
        workflow_state,
        action_type="expense_submitted",
        summary=summary,
        items=items,
        reference_id=reference_id,
        total=total,
        stage="submitted",
        incurred_date_iso=incurred_date_iso,
    )


def record_expense_corrected(
    workflow_state: dict[str, Any],
    *,
    items: list[dict[str, Any]],
    incurred_date_iso: str = "",
    stage: str = "review",
) -> dict[str, Any]:
    total = sum(float(x.get("amount") or 0) for x in items)
    cats = ", ".join(str(x.get("category") or "") for x in items[:4])
    summary = f"Updated expense draft: {cats} — total {total:g} Tk (not submitted yet)."
    return record_bot_action(
        workflow_state,
        action_type="expense_corrected",
        summary=summary,
        items=items,
        total=total,
        stage=stage,
        incurred_date_iso=incurred_date_iso,
    )


def record_pending_expense_discarded(
    workflow_state: dict[str, Any],
    *,
    entry: dict[str, Any],
    items: list[dict[str, Any]],
    incurred_date_iso: str = "",
    stage: str = "collecting",
) -> dict[str, Any]:
    amt = float(entry.get("amount") or 0)
    cat = str(entry.get("category") or "").strip()
    detail = f"{cat} " if cat else ""
    summary = f"Removed incomplete pending line: {detail}{amt:g} Tk (user confirmed)."
    total = sum(float(x.get("amount") or 0) for x in items)
    return record_bot_action(
        workflow_state,
        action_type="expense_pending_discarded",
        summary=summary,
        items=[dict(entry)],
        total=total,
        stage=stage,
        incurred_date_iso=incurred_date_iso,
    )


def record_expense_lines_added(
    workflow_state: dict[str, Any],
    *,
    new_items: list[dict[str, Any]],
    all_items: list[dict[str, Any]],
    incurred_date_iso: str = "",
    stage: str = "collecting",
) -> dict[str, Any]:
    parts = []
    for row in new_items:
        cat = str(row.get("category") or "Other")
        amt = float(row.get("amount") or 0)
        parts.append(f"{cat} {amt:g} Tk")
    summary = f"Added to draft: {', '.join(parts)}."
    total = sum(float(x.get("amount") or 0) for x in all_items)
    return record_bot_action(
        workflow_state,
        action_type="expense_line_added",
        summary=summary,
        items=list(new_items),
        total=total,
        stage=stage,
        incurred_date_iso=incurred_date_iso,
    )


def record_expense_total_check(
    workflow_state: dict[str, Any],
    *,
    total: float,
    line_count: int,
    stage: str = "review",
) -> dict[str, Any]:
    summary = (
        f"Recounted expense draft: {line_count} line(s), total {total:g} Tk "
        f"(stage {stage or 'review'})."
    )
    return record_bot_action(
        workflow_state,
        action_type="expense_total_check",
        summary=summary,
        total=total,
        stage=stage,
    )


def record_vague_add_prompt(workflow_state: dict[str, Any]) -> dict[str, Any]:
    return record_bot_action(
        workflow_state,
        action_type="expense_vague_add_prompt",
        summary="Asked user to specify category and amount for a vague 'add' request.",
        stage=str((read_expense_block(workflow_state) or {}).get("stage") or "collecting"),
    )


def read_last_bot_action(workflow_state: dict[str, Any] | None) -> dict[str, Any]:
    action = (workflow_state or {}).get(KEY_LAST_BOT_ACTION)
    return dict(action) if isinstance(action, dict) else {}


def _format_submit_timing_answer(
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    stage: str,
    *,
    lang: str | None = None,
) -> str:
    """Explain that draft submit can wait until the user finishes the wizard."""
    pending = block.get("pending_line") if isinstance(block.get("pending_line"), dict) else {}
    pending_amt = float(pending.get("amount") or 0)
    pending_step = str(block.get("pending_step") or "").strip().lower()
    last_sub = wf.get("expense_last_submission") or {}
    ref = str(last_sub.get("reference_id") or "")

    if not block.get("active"):
        if ref:
            if lang == "en":
                return (
                    f"Your last expense (`{ref}`) is already **submitted**. "
                    "You can start a **new** expense anytime and submit when ready."
                )
            return (
                f"আপনার শেষ expense (`{ref}`) **submit** হয়ে গেছে। "
                "নতুন খরচ যেকোনো সময় শুরু করে পরে **joma daw** করতে পারবেন।"
            )
        if lang == "en":
            return (
                "Yes — you can log expenses anytime and **submit later** in this chat session. "
                "Example: **lunch 200**, then **done** or **joma daw** when ready."
            )
        return (
            "হ্যাঁ — আপনি যেকোনো সময় expense লিখে **পরে submit** করতে পারবেন (এই session-এ)।\n"
            "যেমন: **lunch 200**, তারপর প্রস্তুত হলে **done** বা **joma daw**।"
        )

    if pending_step == "category" and pending_amt > 0:
        if lang == "en":
            return (
                f"Yes — you can **submit later**. I saved **{pending_amt:g} Tk** in this session.\n"
                "First reply with a **category** (e.g. lunch, snack, bus), "
                "then add any other lines and say **done** or **joma daw** when ready."
            )
        if lang == "banglish":
            return (
                f"হ্যাঁ — **পরে submit** করতে পারবেন। **{pending_amt:g} Tk** এই session-এ save আছে।\n"
                "আগে **category** বলুন (যেমন lunch, snack, bus), "
                "তারপর **done** / **joma daw** দিয়ে submit করুন।"
            )
        return (
            f"হ্যাঁ — **পরে submit** করতে পারবেন। **{pending_amt:g} Tk** এই session-এ সংরক্ষিত আছে।\n"
            "আগে **category** বলুন (যেমন lunch, snack, bus), "
            "তারপর **done** বা **joma daw** দিয়ে জমা দিন।"
        )

    if stage == "submit_confirm":
        if lang == "en":
            return (
                "You're at the **final submit** step now. Reply **yes** / **joma daw** to send to CRM, "
                "or **no** to go back and review. If you leave mid-session, the draft stays saved here."
            )
        return (
            "এখন **চূড়ান্ত submit** ধাপে আছেন। CRM-এ পাঠাতে **yes** / **joma daw** দিন, "
            "আবার দেখতে **no** বলুন। Session ছাড়লেও draft এখানে থাকবে।"
        )

    if stage == "review":
        if lang == "en":
            return (
                "Yes — submit when you're ready. Review the summary, then say **yes** or **joma daw**. "
                "No rush; the draft stays in this session."
            )
        return (
            "হ্যাঁ — প্রস্তুত হলে submit করুন। Summary দেখে **yes** বা **joma daw** বলুন। "
            "তাড়াহুড়ো নেই — draft এই session-এ থাকবে।"
        )

    total = sum(float(x.get("amount") or 0) for x in items) + (
        pending_amt if pending_amt and not items else 0
    )
    if lang == "en":
        return (
            f"Yes — you can **submit later**. Draft is saved in this session"
            f"{f' (**{total:g} Tk**)' if total else ''}.\n"
            "Finish your lines (category, routes if needed), then **done** or **joma daw**."
        )
    return (
        f"হ্যাঁ — **পরে submit** করতে পারবেন। Draft এই session-এ save আছে"
        f"{f' (**{total:g} Tk**)' if total else ''}।\n"
        "লাইন শেষ করে **done** বা **joma daw** বলুন।"
    )


def _format_draft_persistence_answer(
    block: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    lang: str | None = None,
) -> str:
    if not block.get("active"):
        if lang == "en":
            return (
                "There's no open expense draft right now. "
                "When you add lines, they stay in this chat session until you submit or cancel."
            )
        return (
            "এখন কোনো open expense draft নেই। "
            "লাইন add করলে submit বা cancel না করা পর্যন্ত এই session-এ থাকবে।"
        )
    if lang == "en":
        return (
            "Yes — your expense **draft is saved** in this chat session until you submit or cancel. "
            f"Current stage: **{block.get('stage') or 'collecting'}**."
        )
    return (
        "হ্যাঁ — expense **draft এই session-এ save** থাকবে, যতক্ষণ না submit বা cancel করেন।\n"
        f"বর্তমান stage: **{block.get('stage') or 'collecting'}**।"
    )


def _format_items_brief(items: list[dict[str, Any]]) -> str:
    parts = []
    for row in items[:6]:
        cat = str(row.get("category") or "Other")
        amt = float(row.get("amount") or 0)
        parts.append(f"{cat} {amt:g} Tk")
    return ", ".join(parts) if parts else "—"


def format_meta_question_answer(
    workflow_state: dict[str, Any] | None,
    message: str,
    *,
    lang: str | None = None,
) -> str | None:
    """Answer meta/clarify questions from session action memory + draft state."""
    from chat.services.expense.session_ledger import draft_line_rows_for_block

    wf = workflow_state or {}
    action = read_last_bot_action(wf)
    block = read_expense_block(wf)
    items = draft_line_rows_for_block(block)
    stage = str(block.get("stage") or "")
    low = (message or "").lower()

    if wants_expense_pre_submit_review(message):
        if not items:
            return "এখনো কোনো expense line নেই — আগে খরচ যোগ করুন।"
        lines_out = []
        for i, row in enumerate(items, 1):
            cat = str(row.get("category") or "Other")
            amt = float(row.get("amount") or 0)
            lines_out.append(f"{i}. **{cat}** — **{amt:g} Tk**")
        total = sum(float(x.get("amount") or 0) for x in items)
        return (
            "**Submit করার আগে পর্যালোচনা:**\n"
            + "\n".join(lines_out)
            + f"\n\nমোট: **{total:g} Tk** · stage **{stage or 'collecting'}**"
        )

    if wants_expense_submit_timing_question(message):
        return _format_submit_timing_answer(wf, block, items, stage, lang=lang)

    if wants_expense_draft_persistence_question(message):
        return _format_draft_persistence_answer(block, items, lang=lang)

    raw = (message or "").strip()
    if re.search(r"\b(kothai|where|koi|kothay|kothao|missing)\b", low) or re.search(
        r"(age\s+toh|ager\s+to|আগে\s+তো|আগের)", raw, re.I | re.UNICODE
    ):
        from chat.services.expense.session_ledger import line_incompleteness_notes

        all_rows = items
        if all_rows:
            lines_out: list[str] = []
            for row in all_rows:
                cat = str(row.get("category") or "").strip() or "—"
                amt = float(row.get("amount") or 0)
                notes = line_incompleteness_notes(row)
                note_txt = f" — {'; '.join(notes)}" if notes else ""
                lines_out.append(f"- {cat} · **{amt:g} Tk**{note_txt}")
            total = sum(float(r.get("amount") or 0) for r in all_rows)
            if lang == "en":
                return (
                    "Nothing was removed — your **full draft** in this session:\n"
                    + "\n".join(lines_out)
                    + f"\n\nDraft total: **{total:g} Tk** · stage **{stage or 'collecting'}**."
                )
            return (
                "কিছু **মুছে যায়নি** — session-এ আপনার **পুরো draft**:\n"
                + "\n".join(lines_out)
                + f"\n\nমোট draft: **{total:g} Tk** · stage **{stage or 'collecting'}**।"
            )

    if wants_post_submit_edit_question(message):
        last_sub = wf.get("expense_last_submission") or {}
        ref = str(last_sub.get("reference_id") or action.get("reference_id") or "")
        if lang == "en":
            if ref:
                return (
                    f"No — once expense `{ref}` is **submitted**, it cannot be edited in chat. "
                    "Contact CRM/Finance for any post-submit changes."
                )
            return (
                "No — **submitted** expenses cannot be edited in chat. "
                "Contact CRM/Finance for post-submit changes."
            )
        if ref:
            return (
                f"না — expense `{ref}` একবার **submit** হয়ে গেলে chat-এ আর **edit করা যায় না**।\n"
                "Submit-এর পর পরিবর্তন চাইলে CRM/Finance-এর সাথে যোগাযোগ করুন।"
            )
        return (
            "না — expense **submit** হয়ে গেলে chat-এ আর **edit করা যায় না**।\n"
            "পরিবর্তন চাইলে CRM/Finance-এর সাথে যোগাযোগ করুন।"
        )

    submit_probe = bool(
        re.search(r"submit", low) and re.search(r"\b(ki|hoise|hoyeche|done|yet)\b", low)
    )

    if submit_probe:
        last_sub = wf.get("expense_last_submission") or {}
        ref = str(last_sub.get("reference_id") or action.get("reference_id") or "")
        if ref and action.get("action_type") == "expense_submitted":
            total = float(action.get("total") or 0)
            if lang == "en":
                return (
                    f"Yes — your last expense was **submitted**.\n"
                    f"- Reference: `{ref}`\n"
                    f"- Total: **{total:g} Tk**"
                )
            return (
                f"হ্যাঁ — আপনার শেষ expense **submit হয়েছে**।\n"
                f"- রেফারেন্স: `{ref}`\n"
                f"- মোট: **{total:g} Tk**"
            )
        if items and block.get("active"):
            total = sum(float(x.get("amount") or 0) for x in items)
            if lang == "en":
                return (
                    f"No — not submitted yet. Draft is at **{stage or 'collecting'}** stage.\n"
                    f"- Lines: {len(items)} · Total: **{total:g} Tk**\n"
                    f"- Latest: {_format_items_brief(items)}"
                )
            return (
                f"না — **এখনো submit হয়নি**। Draft **{stage or 'collecting'}** stage-এ আছে।\n"
                f"- লাইন: {len(items)} টি · মোট: **{total:g} Tk**\n"
                f"- সর্বশেষ: {_format_items_brief(items)}"
            )
        if ref:
            total = sum(float(x.get("amount") or 0) for x in list(last_sub.get("items") or []))
            return (
                f"হ্যাঁ — session-এ expense submit হয়েছে (`{ref}`, **{total:g} Tk**). "
                f"এখন active draft নেই।"
            )
        if lang == "en":
            return "No expense has been submitted in this session yet."
        return "এই session-এ এখনো কোনো expense submit হয়নি।"

    if not action and not items:
        if lang == "en":
            return "I have not recorded a recent expense action in this session yet."
        return "এই session-এ এখনো কোনো সাম্প্রতিক expense action নেই।"

    atype = str(action.get("action_type") or "")
    summary = str(action.get("summary") or "")
    action_items = list(action.get("items") or items)

    if atype == "expense_total_check":
        total = float(action.get("total") or sum(float(x.get("amount") or 0) for x in items))
        if lang == "en":
            return (
                f"I **recounted** your expense draft: **{total:g} Tk** "
                f"({len(items)} line(s), stage **{stage or action.get('stage') or 'review'}**)."
            )
        return (
            f"আমি expense draft-এর **হিসাব আবার করেছি**: **{total:g} Tk** "
            f"({len(items)} লাইন, stage **{stage or action.get('stage') or 'review'}**)।"
        )

    if atype == "expense_vague_add_prompt":
        if lang == "en":
            return (
                "I asked you to clarify what to add — I did **not** add any line yet. "
                "Say e.g. **lunch 200** or **bus 50 office to home**."
            )
        return (
            "আমি **কি add করব** জানতে চেয়েছিলাম — এখনো কোনো line add করিনি। "
            "লিখুন, যেমন: **lunch 200** বা **bus 50 office to home**।"
        )

    if atype == "expense_submitted":
        ref = str(action.get("reference_id") or "")
        total = float(action.get("total") or 0)
        if lang == "en":
            head = f"I **submitted** expense `{ref}` — **{total:g} Tk**."
        else:
            head = f"আমি expense **submit** করেছি `{ref}` — **{total:g} Tk**।"
        if action_items:
            head += f"\n- Lines: {_format_items_brief(action_items)}"
        if items and block.get("active"):
            pending = sum(float(x.get("amount") or 0) for x in items)
            if lang == "en":
                head += f"\n\nYou also have a **new draft** (not submitted): {pending:g} Tk."
            else:
                head += f"\n\nআপনার **নতুন draft** (submit হয়নি): **{pending:g} Tk**।"
        return head

    if atype == "expense_pending_discarded":
        amt = float(action.get("total") or 0)
        discarded = list(action_items)
        if discarded:
            row = discarded[0]
            amt = float(row.get("amount") or amt)
        stage_hint = stage or str(action.get("stage") or "")
        if lang == "en":
            return (
                f"I **removed** an incomplete pending line (**{amt:g} Tk**) from your draft "
                f"(you confirmed). Stage: **{stage_hint}**."
            )
        return (
            f"আপনার confirm-এ incomplete pending line (**{amt:g} Tk**) draft থেকে **সরিয়ে** দিয়েছি।\n"
            f"Stage: **{stage_hint}**।"
        )

    if atype in ("expense_corrected", "expense_line_added") or action_items:
        total = float(action.get("total") or sum(float(x.get("amount") or 0) for x in action_items))
        if lang == "en":
            head = "Last action on your expense draft:"
        else:
            head = "আপনার expense draft-এ শেষ action:"
        if atype == "expense_line_added":
            if lang == "en":
                head = "I **added** these lines to your draft (not submitted yet):"
            else:
                head = "আমি draft-এ **যোগ** করেছি (এখনো submit হয়নি):"
        elif atype == "expense_corrected":
            if lang == "en":
                head = "I **updated** your expense review:"
            else:
                head = "আমি expense review **আপডেট** করেছি:"
        lines = _format_items_brief(action_items)
        stage_hint = stage or str(action.get("stage") or "")
        if lang == "en":
            return f"{head}\n- {lines}\n- Total: **{total:g} Tk** · Stage: **{stage_hint}**"
        return f"{head}\n- {lines}\n- মোট: **{total:g} Tk** · Stage: **{stage_hint}**"

    if summary:
        return summary
    return None


def format_expense_history_message(
    ledger: dict[str, Any],
    workflow_state: dict[str, Any] | None,
    *,
    lang: str | None = None,
) -> str:
    """Rich session history: ledger + recent action timeline."""
    from chat.services.expense.session_ledger import format_session_expense_ledger_message

    date_iso = str(ledger.get("incurred_date_iso") or "").strip() or "আজ"
    if lang == "en":
        head = f"**Expense history — session** ({date_iso})"
    else:
        head = f"**Expense history — session** ({date_iso})"

    body = format_session_expense_ledger_message(ledger)
    # Replace default header with history header
    body = re.sub(
        r"^\*\*দৈনিক খরচ — সারাংশ\*\*.*?\n\n",
        "",
        body,
        count=1,
    )
    lines = [head, "", body]

    log = _action_log(workflow_state or {})
    if log:
        lines.append("")
        if lang == "en":
            lines.append("📝 **Recent session actions**")
        else:
            lines.append("📝 **Session timeline (সাম্প্রতিক)**")
        for entry in log[-5:]:
            summary = str(entry.get("summary") or "").strip()
            if summary:
                lines.append(f"- {summary}")
    return "\n".join(lines)
