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


def wants_expense_meta_question(message: str) -> bool:
    """User asks about the bot's last expense action (not a new claim line)."""
    raw = (message or "").strip()
    if not raw:
        return False
    from chat.services.expense.expense_total_dispute import is_expense_total_check_query

    if is_expense_total_check_query(message):
        return False
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
    if re.search(r"\b(expense|kharcha|khoroch|summary|summery|draft|pending)\b", low) or re.search(
        r"(খরচ|expense)", raw, re.I
    ):
        if re.search(
            r"\b(keno|why|kothai|where|missing|shudhu|only|just|incomplete)\b", low
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
