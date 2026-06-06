"""
Expense draft validation — warnings and soft checks, not finance approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from chat.constants import EXPENSE_DAY_CAP_BDT
from chat.services.expense_locations import (
    detect_travel_location_typos,
    location_context_from_rows,
)
from chat.services.expense.normalization import expense_category_needs_clarification
from chat.services.expense_extraction import (
    EXPENSE_CATEGORIES,
    is_travel_category,
    normalize_category,
)


@dataclass
class ExpenseValidationResult:
    ok: bool
    warnings: list[str] = field(default_factory=list)
    blocking_message: str | None = None
    line_flags: dict[int, list[str]] = field(default_factory=dict)


def validate_expense_items(
    items: list[dict[str, Any]],
    *,
    incurred_date_iso: str = "",
    day_logged_total: float = 0.0,
    daily_cap: float = EXPENSE_DAY_CAP_BDT,
    message: str = "",
    apply_location_fixes: bool = False,
) -> ExpenseValidationResult:
    if not items:
        return ExpenseValidationResult(
            ok=False,
            blocking_message=(
                "কোনো খরচ শনাক্ত হয়নি। উদাহরণ: lunch 100, bus 50, rickshaw 20।"
            ),
        )

    warnings: list[str] = []
    line_flags: dict[int, list[str]] = {}
    seen: set[tuple[str, float]] = set()
    total = 0.0
    loc_ctx = location_context_from_rows(items)

    for idx, row in enumerate(items):
        raw_cat = str(row.get("category") or "").strip()
        if expense_category_needs_clarification(raw_cat, message=message):
            try:
                amt = float(row.get("amount") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            return ExpenseValidationResult(
                ok=False,
                blocking_message=(
                    f"**{amt:g} Tk** এর category জানা নেই — review এর আগে বলুন "
                    "(যেমন: lunch, bus, snack, rickshaw)।"
                ),
            )

        cat = normalize_category(raw_cat)
        if cat not in EXPENSE_CATEGORIES:
            warnings.append(f"অজানা ক্যাটাগরি '{cat}' — Other হিসেবে রাখা হয়েছে।")
            row["category"] = "Other"
        else:
            row["category"] = cat

        try:
            amt = float(row.get("amount") or 0)
        except (TypeError, ValueError):
            return ExpenseValidationResult(
                ok=False,
                blocking_message="এক বা একাধিক খরচের পরিমাণ পড়া যায়নি — amount আবার লিখুন।",
            )
        if amt <= 0:
            return ExpenseValidationResult(
                ok=False,
                blocking_message="প্রতিটি খরচের amount ০ এর বেশি হতে হবে।",
            )

        key = (cat.lower(), round(amt, 2))
        if key in seen:
            warnings.append(f"ডুপ্লিকেট লাইন: {cat} — {amt:g} Tk (একই মেসেজে দুবার)।")
        seen.add(key)
        total += amt

        if is_travel_category(cat):
            frm = str(row.get("from_location") or "").strip()
            to = str(row.get("to_location") or "").strip()
            if not frm or not to:
                return ExpenseValidationResult(
                    ok=False,
                    blocking_message=(
                        f"**{cat}** খরচের জন্য **From** ও **To** লোকেশন দুটোই লাগবে "
                        "(যেমন: office theke badda, অথবা from office to motijheel)।"
                    ),
                )
            for typo in detect_travel_location_typos(row, context=loc_ctx):
                role = "To" if typo["field"] == "to_location" else "From"
                note = (
                    f"**{cat}** · {role}: **{typo['original']}** — "
                    f"আপনি কি **{typo['suggestion']}** বোঝাচ্ছেন?"
                )
                if apply_location_fixes:
                    row[typo["field"]] = typo["suggestion"]
                    warnings.append(note)
                else:
                    line_flags.setdefault(idx, []).append(
                        f"⚠️ ({typo['suggestion']}?)"
                    )

    projected = float(day_logged_total) + total
    if projected > float(daily_cap) + 1e-9:
        warnings.append(
            f"সতর্কতা: {incurred_date_iso or 'আজ'} মোট {projected:g} Tk — "
            f"দৈনিক সীমা {daily_cap:g} Tk এর কাছাকাছি/উর্ধ্বে। জমা দেওয়া যাবে; "
            "চূড়ান্ত অনুমোদন CRM/Finance করবে।"
        )

    return ExpenseValidationResult(
        ok=True, warnings=warnings, line_flags=line_flags
    )
