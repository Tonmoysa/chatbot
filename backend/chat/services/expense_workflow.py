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

_CONFIRM_RE = re.compile(
    r"^(?:"
    r"yes|yep|yeah|ok|okay|confirm|submit|done|correct|right|"
    r"হ্যাঁ|হ্যা|ঠিক\s*আছে|ঠিক|জমা\s*দাও|জমা\s*দিন|"
    r"thik\s*ache|thik|hmm?\s*yes|submit\s*koro"
    r")\s*\.?$",
    re.I,
)

_DENY_RE = re.compile(
    r"^(?:"
    r"no|nope|wrong|incorrect|not\s+right|cancel|"
    r"না|ভুল|ঠিক\s*নয়|ভুল\s*আছে"
    r")\s*\.?$",
    re.I,
)

_UPDATE_AMOUNT_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+"
    r"(?:(?:\d+)\s*(?:টাকা|taka|tk)?\s*)?"
    r"(?:না|na|no|not)\s+"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk|হবে|hobe)?",
    re.I,
)

_SET_AMOUNT_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?\s*(?:হবে|hobe|হয়|hoy)?",
    re.I,
)

_REMOVE_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+"
    r"(?:remove|delete|বাদ|বাদ\s*দাও|বাদ\s*দিন|remove\s*koro|bad\s*daw)",
    re.I,
)

_REMOVE_ONE_RE = re.compile(
    r"(?:ekta|একটা|one|ek)\s+"
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+"
    r"(?:baad|বাদ|bad)\s*(?:jabe|daw|debo|kor|koro|হবে|হবে)?",
    re.I,
)

_REMOVE_LOOSE_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+.*?"
    r"(?:baad|বাদ|bad)\s*(?:jabe|daw|debo|kor|koro|হবে)?",
    re.I,
)

_ADD_RE = re.compile(
    r"(?:"
    r"(?:আরও|add|plus|new|extra)\s+)?"
    r"(?P<amt>\d+(?:[.,]\d{1,2})?)\s*(?:টাকা|taka|tk)?\s*"
    rf"(?P<cat>{_CATEGORY_TOKEN})"
    r"|"
    rf"(?P<cat2>{_CATEGORY_TOKEN})\s+"
    r"(?:add|যোগ|jog)\s+"
    r"(?P<amt2>\d+(?:[.,]\d{1,2})?)",
    re.I,
)

def clone_workflow_state(state: dict[str, Any] | None) -> dict[str, Any]:
    return dict(state or {})


def is_expense_collecting(workflow_state: dict[str, Any] | None) -> bool:
    block = (workflow_state or {}).get("expense_request") or {}
    return bool(block.get("active")) and not bool(block.get("paused"))


def is_expense_paused(workflow_state: dict[str, Any] | None) -> bool:
    block = (workflow_state or {}).get("expense_request") or {}
    return bool(block.get("active")) and bool(block.get("paused"))


def is_expense_in_progress(workflow_state: dict[str, Any] | None) -> bool:
    block = (workflow_state or {}).get("expense_request") or {}
    return bool(block.get("active"))


def pause_expense_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = clone_workflow_state(workflow_state)
    block = wf.setdefault("expense_request", {})
    block["active"] = True
    block["paused"] = True
    return wf


def resume_expense_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = clone_workflow_state(workflow_state)
    block = wf.get("expense_request") or {}
    if not block.get("active"):
        return wf
    block.pop("paused", None)
    block["active"] = True
    wf["expense_request"] = block
    return wf


def save_expense_last_submission(
    workflow_state: dict[str, Any],
    *,
    reference_id: str,
    items: list[dict[str, Any]],
    incurred_date_iso: str = "",
) -> dict[str, Any]:
    wf = clone_workflow_state(workflow_state)
    wf["expense_last_submission"] = {
        "reference_id": str(reference_id or "").strip(),
        "items": [dict(x) for x in items],
        "incurred_date_iso": str(incurred_date_iso or "").strip(),
        "submitted_at": date.today().isoformat(),
    }
    return wf


def deactivate_expense_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = clone_workflow_state(workflow_state)
    wf.pop("expense_request", None)
    return wf


def expense_pending_prompt(workflow_state: dict[str, Any] | None) -> str | None:
    """Current wizard question for resume-after-interrupt (e.g. policy lookup)."""
    if not is_expense_in_progress(workflow_state):
        return None
    block = _block(workflow_state)
    stage = _normalize_stage(str(block.get("stage") or "collecting"))
    inc_iso = str(block.get("incurred_date_iso") or "")
    items = list(block.get("items") or [])
    warnings = list(block.get("warnings") or [])

    lang = lang_from_block(block)

    if stage == "submit_confirm":
        return submit_confirm_prompt(lang)
    if stage == "review":
        return format_expense_summary(
            items, incurred_date_iso=inc_iso, warnings=warnings, lang=lang
        )

    pending = block.get("pending_line")
    if isinstance(pending, dict) and pending.get("amount"):
        step = str(block.get("pending_step") or "category")
        amt = float(pending.get("amount") or 0)
        if step == "from_to":
            cat = str(pending.get("category") or "Bus")
            return ask_from_to_prompt(cat, amt, lang)
        return ask_category_prompt(amt, lang)

    if items:
        return ask_more_lines_prompt(lang)
    return collect_start_prompt(lang)


def _block(workflow_state: dict[str, Any]) -> dict[str, Any]:
    return (workflow_state or {}).get("expense_request") or {}


def _normalize_stage(stage: str) -> str:
    s = (stage or "collecting").strip().lower()
    if s == "confirming":
        return "review"
    return s or "collecting"


_FINISH_COLLECT_RE = re.compile(
    r"^(?:"
    r"no\s+more|nothing\s+more|that'?s\s+all|done|finish|শেষ|আর\s*নাই|"
    r"আর\s*কিছু\s*নাই|না\s*আর|bas|শুধু\s*এটুকু"
    r")\s*\.?$",
    re.I,
)


def _wants_finish_collecting(message: str) -> bool:
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


def _ask_category_prompt(amount: float, lang: str | None = None) -> str:
    return ask_category_prompt(amount, normalize_reply_lang(lang))


def _ask_from_to_prompt(category: str, amount: float, lang: str | None = None) -> str:
    return ask_from_to_prompt(category, amount, normalize_reply_lang(lang))


def _ask_more_lines_prompt(lang: str | None = None) -> str:
    return ask_more_lines_prompt(normalize_reply_lang(lang))


def _should_reset_pending_for_message(
    message: str, *, pending_step: str = ""
) -> bool:
    """New multi-line input supersedes a single pending From/To question."""
    if (pending_step or "").strip().lower() == "from_to" and _looks_like_route_answer(
        message
    ):
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
        block["stage"] = "collecting"


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
        return items, _ask_more_lines_prompt(lang)
    nxt = queue.pop(0)
    block["pending_queue"] = queue
    block["pending_line"] = nxt
    block["stage"] = "collecting"
    cat = str(nxt.get("category") or "").strip()
    amt = float(nxt.get("amount") or 0)
    frm = str(nxt.get("from_location") or "").strip()
    to = str(nxt.get("to_location") or "").strip()
    if cat and is_travel_category(cat) and (not frm or not to):
        block["pending_step"] = "from_to"
        return items, _ask_from_to_prompt(cat, amt, lang)
    block["pending_step"] = "category"
    return items, _ask_category_prompt(amt, lang)


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
        d = ni.to_dict()
        cat = str(d.get("category") or "").strip()
        if not cat:
            uncategorized.append(ni)
            continue
        if str(d.get("category") or "") == "Other":
            continue
        if is_travel_category(d["category"]) and (
            not str(d.get("from_location") or "").strip()
            or not str(d.get("to_location") or "").strip()
        ):
            needs_route.append(ni)
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
                "source_clause": "",
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
        block["stage"] = "collecting"
        block["pending_queue"] = route_queue + pending_entries
        return out, _ask_from_to_prompt(first.category, float(first.amount), lang)

    if pending_entries:
        block["pending_line"] = pending_entries[0]
        block["pending_queue"] = pending_entries[1:]
        block["pending_step"] = "category"
        block["stage"] = "collecting"
        return out, _ask_category_prompt(float(pending_entries[0]["amount"]), lang)

    return out, None


def _finalize_pending_line(
    pending: dict[str, Any],
) -> dict[str, Any] | None:
    cat = str(pending.get("category") or "").strip()
    try:
        amt = float(pending.get("amount") or 0)
    except (TypeError, ValueError):
        return None
    if not cat or amt <= 0:
        return None
    if is_travel_category(cat):
        frm = str(pending.get("from_location") or "").strip()
        to = str(pending.get("to_location") or "").strip()
        if not frm or not to:
            return None
    return {
        "category": normalize_category(cat),
        "amount": amt,
        "from_location": str(pending.get("from_location") or "").strip(),
        "to_location": str(pending.get("to_location") or "").strip(),
        "notes": str(pending.get("notes") or "").strip(),
    }


def _format_line_display(row: dict[str, Any]) -> str:
    cat = str(row.get("category") or "Other")
    amt = float(row.get("amount") or 0)
    frm = str(row.get("from_location") or "").strip()
    to = str(row.get("to_location") or "").strip()
    if frm and to:
        route = f"{frm} → {to}"
    elif frm or to:
        route = frm or to
    else:
        route = "—"
    return f"- **{cat}** · {route} · **{amt:g} Tk**"


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
    lang: str | None = None,
) -> str:
    reply_lang = normalize_reply_lang(lang)
    total = sum(float(r.get("amount") or 0) for r in items)
    head = review_head(incurred_date_iso, reply_lang)
    body = "\n".join(_format_line_display(r) for r in items)
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


def build_confirmation_question() -> str:
    return "সব তথ্য কি ঠিক আছে? (হ্যাঁ / না)"


def _is_confirmation_yes(message: str) -> bool:
    t = (message or "").strip()
    if _CONFIRM_RE.match(t):
        return True
    return bool(re.search(r"\b(confirm|submit|ঠিক\s*আছে|হ্যাঁ)\b", t, re.I))


def _dedupe_expense_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop accidental duplicate lines (same category + amount)."""
    seen: set[tuple[str, float]] = set()
    out: list[dict[str, Any]] = []
    for row in items:
        cat = str(row.get("category") or "").lower()
        try:
            amt = round(float(row.get("amount") or 0), 2)
        except (TypeError, ValueError):
            out.append(dict(row))
            continue
        key = (cat, amt)
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(row))
    return out


def _set_category_amount(
    out: list[dict[str, Any]], cat: str, new_amt: float
) -> bool:
    """Update amount; collapse multiple rows of the same category to one."""
    cat_l = cat.lower()
    idxs = [
        i
        for i, row in enumerate(out)
        if str(row.get("category") or "").lower() == cat_l
    ]
    if not idxs:
        return False
    out[idxs[0]]["amount"] = new_amt
    for i in reversed(idxs[1:]):
        del out[i]
    return True


def _is_confirmation_no(message: str) -> bool:
    t = (message or "").strip()
    if _DENY_RE.match(t):
        return True
    return bool(re.search(r"\b(না|ভুল|wrong|not\s+right)\b", t, re.I))


def _apply_corrections(
    items: list[dict[str, Any]],
    message: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Return updated items and whether any correction was applied."""
    changed = False
    out = [dict(x) for x in items]
    low = message or ""

    for m in _REMOVE_ONE_RE.finditer(low):
        cat = normalize_category(m.group("cat"))
        for i, row in enumerate(out):
            if str(row.get("category") or "").lower() == cat.lower():
                del out[i]
                changed = True
                break

    for m in _REMOVE_LOOSE_RE.finditer(low):
        if _REMOVE_ONE_RE.search(low):
            continue
        cat = normalize_category(m.group("cat"))
        for i, row in enumerate(out):
            if str(row.get("category") or "").lower() == cat.lower():
                del out[i]
                changed = True
                break

    for m in _REMOVE_RE.finditer(low):
        cat = normalize_category(m.group("cat"))
        before = len(out)
        out = [r for r in out if str(r.get("category") or "").lower() != cat.lower()]
        if len(out) < before:
            changed = True

    for m in _UPDATE_AMOUNT_RE.finditer(low):
        cat = normalize_category(m.group("cat"))
        raw_amt = m.group("amt").replace(",", ".")
        try:
            new_amt = float(raw_amt)
        except ValueError:
            continue
        if _set_category_amount(out, cat, new_amt):
            changed = True

    for m in _SET_AMOUNT_RE.finditer(low):
        if _UPDATE_AMOUNT_RE.search(low):
            continue
        cat = normalize_category(m.group("cat"))
        try:
            new_amt = float(m.group("amt").replace(",", "."))
        except ValueError:
            continue
        if _set_category_amount(out, cat, new_amt):
            changed = True

    for m in _ADD_RE.finditer(low):
        cat_g = m.group("cat") or m.group("cat2")
        amt_g = m.group("amt") or m.group("amt2")
        if not cat_g or not amt_g:
            continue
        try:
            new_amt = float(amt_g.replace(",", "."))
        except ValueError:
            continue
        cat = normalize_category(cat_g)
        found = False
        for row in out:
            if str(row.get("category") or "").lower() == cat.lower():
                row["amount"] = float(row.get("amount") or 0) + new_amt
                found = True
                changed = True
                break
        if not found:
            out.append(
                ExpenseLineItem(category=cat, amount=new_amt).to_dict()
            )
            changed = True

    # Re-extract only when no structured correction matched (e.g. fresh "bus 70").
    if not changed:
        ext = extract_expense_items(message)
        for ni in ext.items:
            cat = ni.category
            if _set_category_amount(out, cat, float(ni.amount)):
                row = next(
                    r
                    for r in out
                    if str(r.get("category") or "").lower() == cat.lower()
                )
                if ni.from_location:
                    row["from_location"] = ni.from_location
                if ni.to_location:
                    row["to_location"] = ni.to_location
                changed = True
            else:
                out.append(ni.to_dict())
                changed = True

    if changed:
        out = _dedupe_expense_items(out)
    return out, changed


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
) -> dict[str, Any]:
    block["items"] = items
    wf["expense_request"] = block
    return {
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


def _try_advance_to_review(
    wf: dict[str, Any],
    block: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    inc_iso: str,
    day_logged_total: float,
    daily_cap: float,
) -> dict[str, Any] | None:
    val = validate_expense_items(
        items,
        incurred_date_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
    )
    if not val.ok:
        block["stage"] = "collecting"
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
    block["stage"] = "review"
    block["warnings"] = val.warnings
    block.pop("pending_line", None)
    block.pop("pending_step", None)
    lang = lang_from_block(block)
    return _pack(
        wf,
        block,
        items=items,
        question=format_expense_summary(
            items, incurred_date_iso=inc_iso, warnings=val.warnings, lang=lang
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

    if step == "category":
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
            block["stage"] = "collecting"
            return _pack(
                wf,
                block,
                items=items,
                question=_ask_from_to_prompt(cat, amt, lang),
                inc_iso=inc_iso,
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
            return _pack(
                wf,
                block,
                items=items,
                question=_ask_from_to_prompt(cat, amt, lang),
                inc_iso=inc_iso,
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
    return _pack(
        wf,
        block,
        items=items,
        question=_ask_more_lines_prompt(lang),
        inc_iso=inc_iso,
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
    day_logged_total: float = 0.0,
    daily_cap: float = 300.0,
) -> dict[str, Any]:
    """
    CRM-aligned expense wizard: collect → review → submit confirm → CRM payload.

    Travel categories (Bus, Train, …) require From/To; Lunch/Snack do not.
    """
    del company_id, employee_id, session_id
    wf = clone_workflow_state(workflow_state)
    block = wf.setdefault("expense_request", {})
    block["active"] = True
    block["workflow_type"] = "expense_request"
    _sync_reply_language(block, message)
    lang = lang_from_block(block)

    items: list[dict[str, Any]] = list(block.get("items") or [])
    stage = _normalize_stage(str(block.get("stage") or "collecting"))
    inc_iso = str(
        block.get("incurred_date_iso")
        or infer_expense_incurred_date_iso(message=message, hints={}, today=date.today())
    )
    block["incurred_date_iso"] = inc_iso

    # --- Submit confirm (second yes) ---
    if stage == "submit_confirm":
        if _is_confirmation_yes(message):
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
            )
            if not val.ok:
                block["stage"] = "collecting"
                return _pack(
                    wf,
                    block,
                    items=items,
                    question=val.blocking_message,
                    warnings=val.warnings,
                    inc_iso=inc_iso,
                    validation_blocked=True,
                )
            date_block = expense_submit_date_block_reason(inc_iso)
            if date_block:
                block["stage"] = "submit_confirm"
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
        if _is_confirmation_no(message):
            block["stage"] = "review"
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
                    items, incurred_date_iso=inc_iso, warnings=val.warnings, lang=lang
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
    if stage == "review":
        if _is_confirmation_yes(message):
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
            )
            if not val.ok:
                block["stage"] = "collecting"
                return _pack(
                    wf,
                    block,
                    items=items,
                    question=val.blocking_message,
                    warnings=val.warnings,
                    inc_iso=inc_iso,
                    validation_blocked=True,
                )
            date_block = expense_submit_date_block_reason(inc_iso)
            if date_block:
                block["stage"] = "review"
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
            block["stage"] = "submit_confirm"
            return _pack(
                wf,
                block,
                items=items,
                question=format_submit_confirm_prompt(lang),
                warnings=val.warnings,
                inc_iso=inc_iso,
            )

        items, corrected = _apply_corrections(items, message)
        if not corrected:
            ext_fix = extract_expense_items(message)
            if ext_fix.items or ext_fix.malformed:
                block_fix = dict(block)
                items, blocked_q = _ingest_extracted_lines(
                    block_fix, items, ext_fix, inc_iso=inc_iso
                )
                block.update(block_fix)
                if blocked_q:
                    wf["expense_request"] = block
                    return _pack(
                        wf,
                        block,
                        items=items,
                        question=blocked_q,
                        inc_iso=inc_iso,
                    )
                corrected = True
        items = _dedupe_expense_items(items)
        if not items:
            ext = extract_expense_items(message)
            if ext.items:
                items = merge_items([], ext.items)

        if _wants_finish_collecting(message) and items:
            adv = _try_advance_to_review(
                wf,
                block,
                items,
                inc_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
            )
            if adv:
                return adv

        val = validate_expense_items(
            items,
            incurred_date_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
        )
        if not val.ok:
            block["stage"] = "collecting"
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

        block["stage"] = "review"
        q = format_expense_summary(
            items, incurred_date_iso=inc_iso, warnings=val.warnings, lang=lang
        )
        if corrected or _is_confirmation_no(message):
            q = "আপডেট করা হয়েছে।\n\n" + q
        return _pack(
            wf,
            block,
            items=items,
            question=q,
            warnings=val.warnings,
            inc_iso=inc_iso,
        )

    # --- Collecting ---
    pending = block.get("pending_line")
    if isinstance(pending, dict) and pending.get("amount"):
        if _should_reset_pending_for_message(
            message, pending_step=str(block.get("pending_step") or "")
        ):
            block.pop("pending_line", None)
            block.pop("pending_step", None)
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
        _wants_finish_collecting(message) or _is_confirmation_yes(message)
    ):
        adv = _try_advance_to_review(
            wf,
            block,
            items,
            inc_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
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
        block["stage"] = "collecting"
        return _pack(
            wf,
            block,
            items=items,
            question=_ask_category_prompt(loose_amt, lang),
            inc_iso=inc_iso,
        )

    ext = extract_expense_items(message)
    if ext.items or ext.malformed:
        items, blocked_q = _ingest_extracted_lines(block, items, ext, inc_iso=inc_iso)
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
            )
            if adv and len(ext.items) >= 2 and not block.get("pending_line"):
                return adv
            return _pack(
                wf,
                block,
                items=items,
                question=_ask_more_lines_prompt(lang),
                inc_iso=inc_iso,
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
        block["stage"] = "collecting"
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
    )
    if adv:
        return adv
    return _pack(
        wf,
        block,
        items=items,
        question=_ask_more_lines_prompt(lang),
        inc_iso=inc_iso,
    )
