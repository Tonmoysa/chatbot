"""
Polish outbound assistant text for readable chat UI (markdown-friendly).

Fixes PDF/OCR line breaks, normalizes bullets, and formats high-traffic HR replies.
"""

from __future__ import annotations

import re
from typing import Any

from chat.constants import INTENT_HR_POLICY, INTENT_LEAVE_REQUEST
from chat.services.translator import detect_user_language
from chat.services.leave_days import compute_requested_leave_days

# Lone section numbers / bullets from extracted PDFs
_SECTION_START = re.compile(
    r"^(\d{1,2})[\.\)]\s*(.*)$",
    re.UNICODE,
)
_BULLET_START = re.compile(r"^[●•◦▪]\s*(.*)$", re.UNICODE)
_SENTENCE_END = re.compile(r"[।.!?:][\s\"')\]]*$")


def collapse_pdf_line_breaks(text: str) -> str:
    """
    Merge lines broken by PDF/OCR (often one word per line) while keeping real paragraphs.
    """
    if not text:
        return ""
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    buf: list[str] = []

    def flush_buf() -> None:
        if buf:
            out.append(" ".join(buf))
            buf.clear()

    for line in raw_lines:
        s = line.strip()
        if not s:
            flush_buf()
            if out and out[-1] != "":
                out.append("")
            continue

        if _SECTION_START.match(s) or _BULLET_START.match(s) or s.startswith(("#", "**", "- ", "* ")):
            flush_buf()
            out.append(s)
            continue

        words = s.split()
        is_fragment = (
            len(s) < 80
            and len(words) <= 6
            and not _SENTENCE_END.search(s)
        )
        if is_fragment:
            buf.append(s)
        else:
            if buf:
                out.append(" ".join(buf + [s]))
                buf.clear()
            else:
                out.append(s)

    flush_buf()
    joined = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", joined).strip()


def normalize_markdown_bullets(text: str) -> str:
    """Convert PDF bullets to markdown list markers the UI renderer understands."""
    lines: list[str] = []
    for line in text.split("\n"):
        s = line.strip()
        if s in ("●", "•", "◦", "▪"):
            continue
        m = _BULLET_START.match(s)
        if m:
            body = (m.group(1) or "").strip()
            lines.append(f"- {body}" if body else "-")
            continue
        sm = _SECTION_START.match(s)
        if sm and not sm.group(2).strip():
            lines.append(f"**{sm.group(1)}.**")
            continue
        if sm and len(sm.group(2)) < 60:
            lines.append(f"**{sm.group(1)}. {sm.group(2).strip()}**")
            continue
        lines.append(line)
    return "\n".join(lines)


def polish_policy_answer(text: str) -> str:
    """Readable policy/RAG answer: fix broken lines, bullets, spacing."""
    t = collapse_pdf_line_breaks(text or "")
    t = normalize_markdown_bullets(t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    # Ensure list blocks have a blank line before them for markdown parser
    t = re.sub(r"([^\n])\n(- )", r"\1\n\n\2", t)
    return t.strip()


def format_leave_submitted_message(
    *,
    entities: dict[str, Any],
    decision: dict[str, Any],
    reference_id: str,
    deduped: bool,
    lang: str,
) -> str:
    """Single cohesive leave submit card (no duplicate EN+BN blocks)."""
    start = str(entities.get("start_date") or "—")
    end = str(entities.get("end_date") or start)
    lt = str(entities.get("leave_type") or "—")
    pay = str(entities.get("leave_payment_category") or "")
    scope = str(entities.get("day_scope") or "full")
    scope_l = "হাফ দিন" if "half" in scope.lower() else "পুরো দিন"
    pay_l = (
        "বেতনসহ"
        if pay == "paid"
        else "বেতন ছাড়া"
        if pay in ("lwop", "unpaid")
        else pay or "—"
    )
    ledger = decision.get("requested_ledger_days")
    if ledger is None:
        ledger = compute_requested_leave_days(entities)
    bal = decision.get("balance_days")
    rem = decision.get("remaining_balance_days")
    ref = (reference_id or "").strip()

    if deduped:
        if lang == "bn":
            msg = "**এই ছুটির আবেদন আগেই জমা হয়েছে** — নতুন আবেদন তৈরি হয়নি।"
        else:
            msg = "**This leave request was already submitted.** No new request was created."
        if ref:
            msg += f"\n\n**রেফারেন্স / Reference:** `{ref}`"
        return msg

    date_line = f"**{start}**" if start == end else f"**{start}** → **{end}**"
    if lang == "bn":
        lines = [
            "**ছুটি আবেদন জমা হয়েছে**",
            "",
            f"- **তারিখ:** {date_line}",
            f"- **ধরন:** {lt} · {pay_l} · {scope_l}",
            f"- **আবেদনকৃত দিন:** {float(ledger):g}",
        ]
        if bal is not None:
            tail = f"- **বর্তমান ব্যালান্স:** {float(bal):g} দিন"
            if rem is not None:
                tail += f" (অনুমোদন হলে আনুমানিক **{float(rem):g}** দিন থাকবে)"
            lines.append(tail)
        if ref:
            lines.append(f"- **রেফারেন্স:** `{ref}`")
        lines.extend(
            [
                "",
                "চূড়ান্ত অনুমোদন আপনার কোম্পানির HR / CRM সিস্টেমে হবে — এই চ্যাট শুধু আবেদন জমা নেয়।",
            ]
        )
        return "\n".join(lines)

    lines = [
        "**Leave request submitted**",
        "",
        f"- **Dates:** {date_line}",
        f"- **Type:** {lt} · {pay_l} · {scope_l}",
        f"- **Days requested:** {float(ledger):g}",
    ]
    if bal is not None:
        extra = f"- **Balance:** {float(bal):g} day(s)"
        if rem is not None:
            extra += f" (~{float(rem):g} remaining if approved)"
        lines.append(extra)
    if ref:
        lines.append(f"- **Reference:** `{ref}`")
    lines.extend(
        [
            "",
            "Final approval happens in your company's HR system — this chat only submits the request.",
        ]
    )
    return "\n".join(lines)


def polish_clarification_message(text: str) -> str:
    """Light cleanup for wizard questions and NEEDS_CLARIFICATION replies."""
    t = collapse_pdf_line_breaks(text or "")
    t = normalize_markdown_bullets(t)
    return t.strip()


def polish_outbound_message(
    message: str,
    *,
    intent: str,
    outcome: str,
    user_message: str,
    entities: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    crm_payload: dict[str, Any] | None = None,
) -> str:
    """
    Final pass before persisting assistant text — intent-aware formatting.
    """
    msg = (message or "").strip()
    if not msg:
        return msg

    lang = detect_user_language(user_message)
    decision = decision or {}
    entities = entities or {}
    crm_payload = crm_payload or {}

    if intent == INTENT_HR_POLICY and outcome == "INFORMATIONAL":
        return polish_policy_answer(msg)

    if (
        intent == INTENT_LEAVE_REQUEST
        and outcome == "SUBMITTED"
        and not crm_payload.get("_skip_leave_format")
    ):
        ref = str(crm_payload.get("request_id") or "").strip()
        sub = crm_payload.get("leave_submission") or {}
        if not ref:
            ref = str(sub.get("reference_id") or sub.get("request_id") or "")
        return format_leave_submitted_message(
            entities=entities,
            decision=decision,
            reference_id=ref,
            deduped=bool(crm_payload.get("_deduped")),
            lang=lang,
        )

    if outcome == "NEEDS_CLARIFICATION":
        return polish_clarification_message(msg)

    if intent == INTENT_LEAVE_REQUEST:
        return polish_clarification_message(msg)

    return collapse_pdf_line_breaks(msg)
