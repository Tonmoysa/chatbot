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
    ExpenseLineItem,
    _CATEGORY_TOKEN,
    extract_expense_items,
    merge_items,
    normalize_category,
)
from chat.services.expense_incurred_date import infer_expense_incurred_date_iso
from chat.services.expense_validation import validate_expense_items

__all__ = [
    "process_expense_turn",
    "is_expense_collecting",
    "deactivate_expense_session",
    "format_expense_summary",
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

_REMOVE_RE = re.compile(
    rf"(?P<cat>{_CATEGORY_TOKEN})\s+"
    r"(?:remove|delete|বাদ|বাদ\s*দাও|বাদ\s*দিন|remove\s*koro|bad\s*daw)",
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
    return bool(block.get("active"))


def deactivate_expense_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = clone_workflow_state(workflow_state)
    wf.pop("expense_request", None)
    return wf


def _block(workflow_state: dict[str, Any]) -> dict[str, Any]:
    return (workflow_state or {}).get("expense_request") or {}


def format_expense_summary(
    items: list[dict[str, Any]],
    *,
    incurred_date_iso: str = "",
    warnings: list[str] | None = None,
) -> str:
    total = sum(float(r.get("amount") or 0) for r in items)
    lines = [f"• {r.get('category')} — {float(r.get('amount') or 0):g} Tk" for r in items]
    head = "আজকের expense summary:" if not incurred_date_iso else f"{incurred_date_iso} expense summary:"
    body = "\n".join(lines)
    warn = ""
    if warnings:
        warn = "\n\n" + "\n".join(f"⚠ {w}" for w in warnings)
    return (
        f"{head}\n\n{body}\n\nমোট: {total:g} Tk{warn}\n\n"
        "সব তথ্য কি ঠিক আছে?\n(হ্যাঁ / না)"
    )


def build_confirmation_question() -> str:
    return "সব তথ্য কি ঠিক আছে? (হ্যাঁ / না)"


def _is_confirmation_yes(message: str) -> bool:
    t = (message or "").strip()
    if _CONFIRM_RE.match(t):
        return True
    return bool(re.search(r"\b(confirm|submit|ঠিক\s*আছে|হ্যাঁ)\b", t, re.I))


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
        for row in out:
            if str(row.get("category") or "").lower() == cat.lower():
                row["amount"] = new_amt
                changed = True
                break

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
    ext = extract_expense_items(message)
    if ext.items and not changed:
        for ni in ext.items:
            cat = ni.category
            replaced = False
            for row in out:
                if str(row.get("category") or "").lower() == cat.lower():
                    row["amount"] = ni.amount
                    if ni.from_location:
                        row["from_location"] = ni.from_location
                    if ni.to_location:
                        row["to_location"] = ni.to_location
                    replaced = True
                    changed = True
                    break
            if not replaced:
                out.append(ni.to_dict())
                changed = True

    return out, changed


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
    One expense workflow turn.

    Returns:
      workflow_state, complete, submitted, question, items, warnings,
      incurred_date_iso, validation_blocked
    """
    wf = clone_workflow_state(workflow_state)
    block = wf.setdefault("expense_request", {})
    block["active"] = True
    block["workflow_type"] = "expense_request"

    items: list[dict[str, Any]] = list(block.get("items") or [])
    stage = str(block.get("stage") or "collecting")
    inc_iso = str(
        block.get("incurred_date_iso")
        or infer_expense_incurred_date_iso(message=message, hints={}, today=date.today())
    )
    block["incurred_date_iso"] = inc_iso

    if stage == "confirming":
        if _is_confirmation_yes(message):
            val = validate_expense_items(
                items,
                incurred_date_iso=inc_iso,
                day_logged_total=day_logged_total,
                daily_cap=daily_cap,
            )
            if not val.ok:
                block["stage"] = "collecting"
                block["items"] = items
                wf["expense_request"] = block
                return {
                    "workflow_state": wf,
                    "complete": False,
                    "submitted": False,
                    "question": val.blocking_message,
                    "items": items,
                    "warnings": val.warnings,
                    "incurred_date_iso": inc_iso,
                    "validation_blocked": True,
                }
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
            }

        # Correction path or follow-up items before confirm
        items, _ = _apply_corrections(items, message)
        if not items:
            ext = extract_expense_items(message)
            if ext.items:
                items = merge_items([], ext.items)

        val = validate_expense_items(
            items,
            incurred_date_iso=inc_iso,
            day_logged_total=day_logged_total,
            daily_cap=daily_cap,
        )
        if not val.ok:
            block["stage"] = "collecting"
            block["items"] = items
            wf["expense_request"] = block
            return {
                "workflow_state": wf,
                "complete": False,
                "submitted": False,
                "question": val.blocking_message or (
                    "কোনো খরচ পাওয়া যায়নি। lunch 100, bus 50 — এভাবে লিখুন।"
                ),
                "items": items,
                "warnings": val.warnings,
                "incurred_date_iso": inc_iso,
                "validation_blocked": True,
            }

        block["stage"] = "confirming"
        block["items"] = items
        block["warnings"] = val.warnings
        wf["expense_request"] = block
        q = format_expense_summary(
            items, incurred_date_iso=inc_iso, warnings=val.warnings
        )
        if _is_confirmation_no(message):
            q = "আপডেট করা হয়েছে।\n\n" + q
        return {
            "workflow_state": wf,
            "complete": False,
            "submitted": False,
            "question": q,
            "items": items,
            "warnings": val.warnings,
            "incurred_date_iso": inc_iso,
            "validation_blocked": False,
        }

    # collecting stage
    ext = extract_expense_items(message)
    if ext.items:
        items = merge_items(items, ext.items, replace_same_category=bool(items))

    if not items:
        block["stage"] = "collecting"
        block["items"] = []
        wf["expense_request"] = block
        q = (
            "আজকের খরচগুলো লিখুন — একসাথে একাধিক লাইন দিতে পারবেন।\n"
            "উদাহরণ: lunch 100, bus 50, rickshaw 20"
        )
        if ext.malformed:
            q = (
                "কিছু লাইন বুঝতে পারিনি। category ও amount স্পষ্ট করে লিখুন।\n"
                "উদাহরণ: lunch 100, bus 50"
            )
        return {
            "workflow_state": wf,
            "complete": False,
            "submitted": False,
            "question": q,
            "items": [],
            "warnings": [],
            "incurred_date_iso": inc_iso,
            "validation_blocked": False,
        }

    val = validate_expense_items(
        items,
        incurred_date_iso=inc_iso,
        day_logged_total=day_logged_total,
        daily_cap=daily_cap,
    )
    if not val.ok:
        block["stage"] = "collecting"
        block["items"] = items
        wf["expense_request"] = block
        return {
            "workflow_state": wf,
            "complete": False,
            "submitted": False,
            "question": val.blocking_message,
            "items": items,
            "warnings": val.warnings,
            "incurred_date_iso": inc_iso,
            "validation_blocked": True,
        }

    block["stage"] = "confirming"
    block["items"] = items
    block["warnings"] = val.warnings
    wf["expense_request"] = block
    extra = ""
    if items and len(ext.items) < len(items):
        extra = "আর কোনো expense আছে? না থাকলে summary দেখুন।\n\n"
    q = extra + format_expense_summary(
        items, incurred_date_iso=inc_iso, warnings=val.warnings
    )
    return {
        "workflow_state": wf,
        "complete": False,
        "submitted": False,
        "question": q,
        "items": items,
        "warnings": val.warnings,
        "incurred_date_iso": inc_iso,
        "validation_blocked": False,
    }
