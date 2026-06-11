"""Localized copy for the leave wizard (P2 — expense parity)."""

from __future__ import annotations

from typing import Any

from chat.services.expense_copy import ReplyLang, normalize_reply_lang


def lang_from_draft(draft: dict[str, Any] | None) -> ReplyLang:
    return normalize_reply_lang((draft or {}).get("reply_language"))


def review_header(lang: ReplyLang) -> str:
    if lang == "en":
        return "**Leave request — review**"
    if lang == "banglish":
        return "**Leave request — review**"
    return "**ছুটি আবেদন — পর্যালোচনা**"


def scope_label(scope: str, lang: ReplyLang) -> str:
    if scope == "full":
        return "Full day" if lang == "en" else "পুরো দিন"
    if scope == "half":
        return "Half day" if lang == "en" else "হাফ দিন"
    return scope


def confirmation_footer(lang: ReplyLang) -> str:
    if lang == "en":
        return (
            "Submit this leave request?\n"
            "• **yes** — submit\n"
            "• **edit** — change a field (dates, paid/unpaid, full/half day, reason)\n"
            "• **cancel** — discard"
        )
    if lang == "banglish":
        return (
            "Leave request submit korben?\n"
            "• **yes** — submit\n"
            "• **edit** — field change (date, paid/unpaid, full/half, reason)\n"
            "• **cancel** — cancel"
        )
    return (
        "নিচের তথ্য একবার **চেক** করুন — সব **ঠিক** থাকলে **yes** লিখে জমা দিন।\n"
        "• **yes** — জমা দিন\n"
        "• **edit** — কোনো তথ্য বদলান (তারিখ, paid/unpaid, পুরো/হাফ দিন, কারণ)\n"
        "• **cancel** — বাতিল"
    )


_BN_MONTH_NAMES = {
    1: "জানুয়ারি",
    2: "ফেব্রুয়ারি",
    3: "মার্চ",
    4: "এপ্রিল",
    5: "মে",
    6: "জুন",
    7: "জুলাই",
    8: "আগস্ট",
    9: "সেপ্টেম্বর",
    10: "অক্টোবর",
    11: "নভেম্বর",
    12: "ডিসেম্বর",
}


def _format_leave_date_label(iso: str, *, lang: ReplyLang) -> str:
    if not iso or iso == "—":
        return iso
    if lang == "bn":
        try:
            import datetime as _dt

            d = _dt.date.fromisoformat(iso)
            month = _BN_MONTH_NAMES.get(d.month)
            if month:
                return f"{d.day} {month} ({iso})"
        except ValueError:
            pass
    return iso


def build_review_summary_body(
    draft: dict[str, Any],
    *,
    lang: ReplyLang | None = None,
    select_leave_label: str,
) -> str:
    reply = normalize_reply_lang(lang or lang_from_draft(draft))
    scope = str(draft.get("day_scope") or "—")
    start = _format_leave_date_label(str(draft.get("start_date") or "—"), lang=reply)
    end_raw = str(draft.get("end_date") or draft.get("start_date") or "—")
    end = _format_leave_date_label(end_raw, lang=reply)
    reason = str(draft.get("reason") or "—")
    scope_txt = scope_label(scope, reply)

    if reply == "en":
        lines = [
            review_header(reply),
            f"• Select Leave: {select_leave_label}",
            f"• Duration: {scope_txt}",
            f"• Dates: {start}" + (f" → {end}" if end != start else ""),
            f"• Reason: {reason}",
        ]
    elif reply == "banglish":
        lines = [
            review_header(reply),
            f"• Select Leave: {select_leave_label}",
            f"• Duration: {scope_txt}",
            f"• Date: {start}" + (f" → {end}" if end != start else ""),
            f"• Reason: {reason}",
        ]
    else:
        lines = [
            review_header(reply),
            f"• Select Leave: {select_leave_label}",
            f"• পুরো/হাফ দিন: {scope_txt}",
            f"• তারিখ: {start}" + (f" → {end}" if end != start else ""),
            f"• কারণ: {reason}",
        ]

    from chat.services.leave_draft_utils import (
        has_real_supporting_document,
        supporting_document_needed,
    )

    if supporting_document_needed(draft):
        if has_real_supporting_document(draft):
            doc_line = "• Attachment: yes" if reply == "en" else "• সংযুক্তি: আছে"
        elif draft.get("supporting_document_waived"):
            if reply == "en":
                doc_line = "• Attachment: none — manager will review"
            else:
                doc_line = "• সংযুক্তি: এখন নেই — ম্যানেজার রিভিউ নেবেন"
        else:
            doc_line = ""
        if doc_line:
            lines.append(doc_line)

    return "\n".join(lines)


def build_confirmation_prompt_body(
    draft: dict[str, Any],
    *,
    lang: ReplyLang | None = None,
    select_leave_label: str,
) -> str:
    body = build_review_summary_body(
        draft, lang=lang, select_leave_label=select_leave_label
    )
    reply = normalize_reply_lang(lang or lang_from_draft(draft))
    return body + "\n\n" + confirmation_footer(reply)
