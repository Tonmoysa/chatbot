"""Numbered draft summary — saved lines, pending gaps, deleted lines excluded."""

from __future__ import annotations

from typing import Any

from chat.services.expense.draft_view import ExpenseDraftView
from chat.services.expense_copy import normalize_reply_lang


def format_numbered_draft_summary(
    items: list[dict[str, Any]],
    block: dict[str, Any] | None,
    *,
    incurred_date_iso: str = "",
    lang: str | None = None,
    header: str | None = None,
    include_pending_section: bool = True,
) -> str:
    reply_lang = normalize_reply_lang(lang)
    view = ExpenseDraftView(items, block)
    date_part = incurred_date_iso or str((block or {}).get("incurred_date_iso") or "")
    if header:
        head = header
    elif reply_lang == "en":
        head = f"Saved — {date_part}" if date_part else "Saved"
    else:
        head = f"Save korechi — {date_part}" if date_part else "Save korechi"

    saved = [ln for ln in view.lines if ln.kind == "committed"]
    pending = [ln for ln in view.lines if ln.kind != "committed"]

    body_lines: list[str] = [head, ""]
    if saved:
        if reply_lang == "en":
            body_lines.append("✅ **Saved:**")
        else:
            body_lines.append("✅ **Saved:**")
        for ln in saved:
            cat = ln.category.capitalize() if ln.category else "?"
            route = ""
            if ln.from_location and ln.to_location:
                route = f" ({ln.from_location} → {ln.to_location})"
            body_lines.append(f"{ln.number}. {cat} — {ln.amount:g} Tk{route}")

    if include_pending_section and pending:
        body_lines.append("")
        if reply_lang == "en":
            body_lines.append("⏳ **Pending (not yet complete):**")
        else:
            body_lines.append("⏳ **Pending:**")
        for ln in pending:
            cat = ln.category.capitalize() if ln.category else "?"
            gap = f" — {ln.pending_gap}" if ln.pending_gap else ""
            body_lines.append(f"{ln.number}. {cat} — {ln.amount:g} Tk{gap}")

    if include_pending_section and view.pending_gap_lines():
        body_lines.append("")
        gaps = view.pending_gap_lines()
        if reply_lang == "en":
            body_lines.append("ℹ **Still needed:**")
            for ln in gaps:
                body_lines.append(f"• #{ln.number} {ln.category} — {ln.pending_gap}")
        else:
            body_lines.append("ℹ **এখনো লাগবে:**")
            for ln in gaps:
                body_lines.append(f"• #{ln.number} {ln.category} — {ln.pending_gap}")

    saved_total = view.committed_total()
    draft_total = view.draft_total()
    body_lines.append("")
    if pending and draft_total != saved_total:
        if reply_lang == "en":
            body_lines.append(
                f"**Total saved: {saved_total:g} Tk** · **Total draft: {draft_total:g} Tk**"
            )
        else:
            body_lines.append(
                f"**মোট saved: {saved_total:g} Tk** · **Total draft: {draft_total:g} Tk**"
            )
    elif reply_lang == "en":
        body_lines.append(f"**Total saved: {saved_total:g} Tk**")
    else:
        body_lines.append(f"**মোট saved: {saved_total:g} Tk**")

    return "\n".join(body_lines)


def format_submit_blocked_summary(
    items: list[dict[str, Any]],
    block: dict[str, Any] | None,
    *,
    incurred_date_iso: str = "",
    lang: str | None = None,
) -> str:
    reply_lang = normalize_reply_lang(lang)
    view = ExpenseDraftView(items, block)
    gaps = view.pending_gap_lines()
    gap_text = "; ".join(
        f"{ln.category.capitalize()} — {ln.amount:g} Tk" for ln in gaps
    )
    summary = format_numbered_draft_summary(
        items,
        block,
        incurred_date_iso=incurred_date_iso,
        lang=lang,
        header=None,
    )
    first = gaps[0] if gaps else None
    if reply_lang == "en":
        lead = (
            "Saved your lines — **cannot submit yet** because some details are still missing.\n"
            f"Pending: {gap_text}."
        )
        if first and first.pending_gap:
            lead += (
                f"\n\nFirst: **#{first.number} {first.category.capitalize()}** — "
                f"{first.pending_gap}."
            )
        lead += "\nComplete missing info, then say **joma daw** again."
    else:
        lead = (
            "লাইন save করেছি — **এখনো জমা দেওয়া যাবে না**, কিছু তথ্য বাকি।\n"
            f"Pending: {gap_text}."
        )
        if first and first.pending_gap:
            lead += (
                f"\n\nপ্রথমে: **#{first.number} {first.category.capitalize()}** — "
                f"{first.pending_gap}।"
            )
        lead += "\nবাকি তথ্য দিন, তারপর আবার **joma daw** বলুন।"
    return f"{lead}\n\n{summary}"
