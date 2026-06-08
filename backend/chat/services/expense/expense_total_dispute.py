"""
Deterministic expense total check / dispute — never LLM chit-chat for recounts.
"""

from __future__ import annotations

import re
from typing import Any

from chat.constants import EXPENSE_DAY_CAP_BDT
from chat.services.expense.expense_fsm import read_expense_block
from chat.services.expense.session_ledger import (
    build_session_expense_ledger,
    format_ledger_footnotes,
)
from chat.services.expense_workflow import _format_line_display, format_expense_summary


def parse_user_stated_total(message: str) -> float | None:
    """Amount the user claims the total should be (if any)."""
    raw = message or ""
    low = raw.lower()
    patterns = [
        r"(?:total|mot|moot|money|mony|sum|hisab|হিসাব|মোট)\s*(?:hoy|hoise|hobe|is|are|ta)?\s*"
        r"(\d+(?:[.,]\d{1,2})?)\s*(?:tk|taka|bdt)?",
        r"(\d+(?:[.,]\d{1,2})?)\s*(?:tk|taka|bdt)\s*(?:vul|wrong|ভুল|thik\s*nai)",
        r"(?:should\s+be|hobe|হবে)\s*(\d+(?:[.,]\d{1,2})?)",
    ]
    for pat in patterns:
        m = re.search(pat, low)
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except (TypeError, ValueError):
                continue
    return None


def is_expense_total_dispute_query(message: str) -> bool:
    """User thinks the total is wrong or wants a explicit recount."""
    raw = (message or "").strip()
    if not raw or _is_expense_limit_policy_query(message):
        return False
    low = raw.lower()
    if not _has_total_domain(raw, low):
        return False
    return bool(
        re.search(
            r"\b(wrong|vul|ভুল|mistake|incorrect|thik\s*nai|ঠিক\s*নয়|"
            r"mismatch|problem|issue|somossa|সমস্যা)\b",
            low,
        )
        or re.search(r"(ভুল|ঠিক\s*নয়)", raw)
    )


def is_expense_total_verify_query(message: str) -> bool:
    """User asks to verify / confirm the total (not necessarily claiming it's wrong)."""
    raw = (message or "").strip()
    if not raw or _is_expense_limit_policy_query(message):
        return False
    low = raw.lower()
    if not _has_total_domain(raw, low):
        return False
    if is_expense_total_dispute_query(message):
        return True
    return bool(
        re.search(
            r"\b(check|verify|confirm|recount|recalculate|double\s*check|"
            r"thik\s*ache|sothik|correct|match)\b",
            low,
        )
        or re.search(r"(চেক|যাচাই|ঠিক\s*আছে|গণনা)", raw)
        or re.search(r"\b(total|mot|moot)\s*(koto|ki|ta)\b", low)
        or re.search(r"\b(koto|ki)\s*(hobe|hoy|ache|ase)\b", low)
    )


def is_expense_total_check_query(message: str) -> bool:
    return is_expense_total_dispute_query(message) or is_expense_total_verify_query(
        message
    )


def _is_expense_limit_policy_query(message: str) -> bool:
    """Daily cap / allowance questions — not draft total recount."""
    raw = (message or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if re.search(r"\b(limit|cap|budget|allowance|policy|entitlement)\b", low) or re.search(
        r"(সীমা|বাজেট|ভাতা)", raw
    ):
        if not re.search(
            r"\b(check|verify|recount|vul|wrong|mistake|thik\s*nai|ঠিক\s*নয়)\b",
            low,
        ) and not re.search(r"(চেক|ভুল|যাচাই)", raw):
            return True
    try:
        from chat.services.policy_intent_helpers import is_expense_entitlement_query

        if is_expense_entitlement_query(message):
            return True
    except Exception:
        pass
    return False


def _has_total_domain(raw: str, low: str) -> bool:
    return bool(
        re.search(
            r"\b(total|mot|moot|amount|money|mony|taka|tk|sum|hisab|combined|"
            r"subtotal|মোট|টাকা|হিসাব)\b",
            low,
        )
        or re.search(r"(মোট|টাকা|হিসাব)", raw)
    )


def _format_arithmetic_breakdown(
    items: list[dict[str, Any]],
    *,
    lang: str | None = None,
) -> str:
    if not items:
        return ""
    parts = [f"+ {float(x.get('amount') or 0):g}" for x in items]
    labels = [str(x.get("category") or "Other") for x in items]
    total = sum(float(x.get("amount") or 0) for x in items)
    lines: list[str] = []
    if lang == "en":
        lines.append("**Line-by-line count:**")
    else:
        lines.append("**লাইন ধরে হিসাব:**")
    for label, part in zip(labels, parts):
        lines.append(f"  · {label} {part.replace('+ ', '')} Tk")
    if lang == "en":
        lines.append(f"  **= {total:g} Tk**")
    else:
        lines.append(f"  **= {total:g} Tk**")
    return "\n".join(lines)


def _format_stated_total_comparison(
    stated: float,
    actual: float,
    *,
    lang: str | None = None,
) -> str:
    diff = round(actual - stated, 2)
    if abs(diff) < 0.01:
        if lang == "en":
            return f"✅ Your figure **{stated:g} Tk** matches the draft total."
        return f"✅ আপনার **{stated:g} Tk** — draft total-এর সাথে **মিলেছে**।"
    if lang == "en":
        return (
            f"⚠️ You mentioned **{stated:g} Tk**; draft adds up to **{actual:g} Tk** "
            f"(difference **{abs(diff):g} Tk**)."
        )
    return (
        f"⚠️ আপনি **{stated:g} Tk** বলেছেন; draft যোগফল **{actual:g} Tk** "
        f"(পার্থক্য **{abs(diff):g} Tk**)।"
    )


def format_expense_total_check_message(
    workflow_state: dict[str, Any] | None,
    *,
    crm_breakdown: dict[str, Any] | None = None,
    incurred_date_iso: str = "",
    lang: str | None = None,
    user_message: str = "",
) -> str | None:
    """Recount draft + session ledger totals deterministically."""
    wf = workflow_state or {}
    block = read_expense_block(wf)
    from chat.services.expense.session_ledger import draft_line_rows_for_block

    items = draft_line_rows_for_block(block)
    if not items and not (
        wf.get("expense_submissions_history") or wf.get("expense_last_submission")
    ):
        return None

    inc = str(incurred_date_iso or block.get("incurred_date_iso") or "").strip()
    ledger = build_session_expense_ledger(
        wf,
        crm_breakdown=crm_breakdown or {},
        incurred_date_iso=inc,
        daily_cap=float(EXPENSE_DAY_CAP_BDT),
    )
    pending_total = float(ledger.get("pending_total") or 0)
    submitted_total = float(ledger.get("submitted_total") or 0)
    combined = float(ledger.get("combined_total") or 0)
    cap = float(ledger.get("daily_cap") or EXPENSE_DAY_CAP_BDT)
    remaining = float(ledger.get("remaining_under_cap") or 0)
    draft_sum = sum(float(x.get("amount") or 0) for x in items)
    stage = str(block.get("stage") or "")
    stated = parse_user_stated_total(user_message) if user_message else None

    if lang == "en":
        head = "**Expense total check**"
        sub = "_Deterministic recount from your session draft — not a guess._"
    elif lang == "banglish":
        head = "**Expense total check**"
        sub = "_Apnar session draft theke exact গণনা — guess নয়._"
    else:
        head = "**Expense total check**"
        sub = "_আপনার session draft থেকে সঠিক গণনা — অনুমান নয়।_"

    lines = [head, sub, ""]

    if items:
        if stage in ("review", "submit_confirm"):
            summary = format_expense_summary(
                items,
                incurred_date_iso=inc,
                lang=lang,
            )
            lines.append(summary)
        else:
            lines.append(_format_arithmetic_breakdown(items, lang=lang))
            for row in items:
                lines.append(f"  {_format_line_display(row).lstrip('- ')}")
        lines.append("")
        lines.append(_format_arithmetic_breakdown(items, lang=lang))
        lines.append("")
        if abs(draft_sum - pending_total) < 0.01:
            if lang == "en":
                lines.append(
                    f"✅ Draft sum **{draft_sum:g} Tk** matches pending total."
                )
            else:
                lines.append(
                    f"✅ Draft যোগফল **{draft_sum:g} Tk** — pending total-এর সাথে মিলেছে।"
                )
        else:
            if lang == "en":
                lines.append(
                    f"⚠️ Draft sum **{draft_sum:g} Tk** · ledger pending **{pending_total:g} Tk**."
                )
            else:
                lines.append(
                    f"⚠️ Draft **{draft_sum:g} Tk** · ledger pending **{pending_total:g} Tk**।"
                )
        if stated is not None:
            lines.append(_format_stated_total_comparison(stated, draft_sum, lang=lang))
    else:
        if lang == "en":
            lines.append("No active expense draft lines — only submitted history below.")
        else:
            lines.append("Active draft line নেই — নিচে submitted history।")

    lines.append("")
    if lang == "en":
        lines.append("📊 **Session track (today)**")
        lines.append(f"   - Submitted: **{submitted_total:g} Tk**")
        if pending_total > 0:
            lines.append(f"   - Pending draft: **{pending_total:g} Tk**")
        lines.append(f"   - Combined track: **{combined:g} Tk**")
        lines.append(f"   - Cap remaining (submitted): **{remaining:g} Tk** / **{cap:g} Tk**")
    else:
        lines.append("📊 **Session track (আজ)**")
        lines.append(f"   - জমা হয়েছে: **{submitted_total:g} Tk**")
        if pending_total > 0:
            lines.append(f"   - Pending draft: **{pending_total:g} Tk**")
        lines.append(f"   - মোট track: **{combined:g} Tk**")
        lines.append(f"   - Cap বাকি (submitted): **{remaining:g} Tk** / **{cap:g} Tk**")

    foot = format_ledger_footnotes(ledger, lang=lang)
    if foot:
        lines.append(foot)

    lines.append("")
    if lang == "en":
        lines.append("**Next steps**")
        lines.append("- Fix a line: `bus 70 hobe` · `lunch baad daw`")
        lines.append("- Roll back: `ager thik chilo restore koro`")
        lines.append("- Happy with total: `yes`")
    elif lang == "banglish":
        lines.append("**Next steps**")
        lines.append("- Fix: `bus 70 hobe` · `lunch baad daw`")
        lines.append("- Roll back: `ager thik chilo restore koro`")
        lines.append("- OK hole: `yes`")
    else:
        lines.append("**পরবর্তী ধাপ**")
        lines.append("- ঠিক করতে: `bus 70 hobe` · `lunch baad daw`")
        lines.append("- আগের version: `ager thik chilo restore koro`")
        lines.append("- ঠিক থাকলে: `yes`")

    return "\n".join(lines)
