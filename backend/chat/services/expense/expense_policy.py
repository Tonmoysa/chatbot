"""
Deterministic expense policy answers (daily cap, limits) — not CRM spend summaries.
"""

from __future__ import annotations

import re
from typing import Any

from chat.constants import EXPENSE_DAY_CAP_BDT


def is_expense_daily_cap_query(message: str) -> bool:
    """
    User asks about the same-day expense reimbursement cap (e.g. 300 Tk),
    not logging spend or TA/DA allowance.
    """
    if not message:
        return False
    low = message.lower()
    raw = message or ""

    if re.search(r"\b(cost|kharcha|khoroch|expense|reimbursement)\s*limit\b", low):
        return True
    if re.search(r"\b(daily|same\s*day|protidin|ajker|ajke)\s*(?:expense|cost|kharcha)?\s*(?:limit|cap)\b", low):
        return True
    if re.search(r"\b(limit|cap|sima|সীমা)\b", low) and re.search(
        r"\b(cost|kharcha|khoroch|expense|taka|tk|টাকা|money)\b", low
    ):
        return True
    if re.search(
        r"(?:koto|কত).{0,25}(?:limit|cap|sima|সীমা)",
        low,
    ) or re.search(r"(?:limit|cap|সীমা).{0,25}(?:koto|কত)", raw, re.I):
        if re.search(r"\b(cost|kharcha|khoroch|expense|taka|tk|টাকা)\b", low) or re.search(
            r"(খরচ|টাকা|expense)", raw, re.I
        ):
            return True
    if re.search(r"\b300\s*(?:taka|tk|টাকা|bdt)\b", low) and re.search(
        r"\b(limit|cap|max|maximum)\b", low
    ):
        return True
    if re.search(r"(দৈনিক\s*খরচ\s*সীমা|expense\s*cap|daily\s*expense\s*cap)", low):
        return True
    return False


def format_expense_daily_cap_message(
    *,
    daily_cap: float = EXPENSE_DAY_CAP_BDT,
    submitted_total: float = 0.0,
    pending_total: float = 0.0,
    incurred_date_iso: str = "",
) -> str:
    date_hint = incurred_date_iso or "আজ"
    remaining = max(0.0, float(daily_cap) - float(submitted_total))
    lines = [
        f"**Same-day expense cap:** **{daily_cap:g} Tk** (company policy).",
        "",
        "এটি **auto-approve/reimbursement cap** — এক দিনে এই পরিমাণের বেশি submit করলে "
        "CRM/Finance চূড়ান্ত অনুমোদন করবে; chatbot submit block করবে না (warning দেখাতে পারে)।",
    ]
    if submitted_total > 0 or pending_total > 0:
        lines.extend(
            [
                "",
                f"**{date_hint} — আপনার track:**",
                f"- CRM/Session-এ submitted: **{submitted_total:g} Tk**",
            ]
        )
        if pending_total > 0:
            lines.append(f"- Pending draft (submit হয়নি): **{pending_total:g} Tk**")
        lines.append(f"- Cap-এর under submitted remaining: **{remaining:g} Tk**")
        if submitted_total + pending_total > daily_cap:
            lines.append(
                f"\n⚠ মোট track **{submitted_total + pending_total:g} Tk** — cap **{daily_cap:g} Tk**-এর বেশি।"
            )
    else:
        lines.extend(
            [
                "",
                f"আজ ({date_hint}) এখনো কোনো submitted/pending track নেই — "
                f"আপনি সর্বোচ্চ **{daily_cap:g} Tk** পর্যন্ত same-day cap-এর মধ্যে submit করতে পারবেন "
                "(policy অনুযায়ী)।",
            ]
        )
    lines.extend(
        [
            "",
            "TA/DA বা allowance policy জানতে চাইলে policy নাম দিয়ে জিজ্ঞাসা করুন।",
        ]
    )
    return "\n".join(lines)


def build_daily_cap_response(
    workflow_state: dict[str, Any] | None,
    *,
    crm_breakdown: dict[str, Any] | None = None,
    incurred_date_iso: str = "",
    daily_cap: float = EXPENSE_DAY_CAP_BDT,
) -> str:
    from chat.services.expense.session_ledger import build_session_expense_ledger

    ledger = build_session_expense_ledger(
        workflow_state,
        crm_breakdown=crm_breakdown or {},
        incurred_date_iso=incurred_date_iso,
        daily_cap=daily_cap,
    )
    return format_expense_daily_cap_message(
        daily_cap=float(ledger.get("daily_cap") or daily_cap),
        submitted_total=float(ledger.get("submitted_total") or 0),
        pending_total=float(ledger.get("pending_total") or 0),
        incurred_date_iso=str(ledger.get("incurred_date_iso") or incurred_date_iso),
    )
