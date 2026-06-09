"""
Polish outbound assistant text for readable chat UI (markdown-friendly).

Fixes PDF/OCR line breaks, normalizes bullets, and formats high-traffic HR replies.
"""

from __future__ import annotations

import re
from typing import Any

from chat.constants import (
    EXPENSE_DAY_CAP_BDT,
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_HR_POLICY,
    INTENT_LEAVE_REQUEST,
)
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
    pending_bullet = False
    for line in text.split("\n"):
        s = line.strip()
        if s in ("●", "•", "◦", "▪"):
            pending_bullet = True
            continue
        m = _BULLET_START.match(s)
        if m:
            body = (m.group(1) or "").strip()
            lines.append(f"- {body}" if body else "-")
            pending_bullet = False
            continue
        sm = _SECTION_START.match(s)
        if sm and not sm.group(2).strip():
            lines.append(f"**{sm.group(1)}.**")
            pending_bullet = False
            continue
        if sm and len(sm.group(2)) < 60:
            lines.append(f"**{sm.group(1)}. {sm.group(2).strip()}**")
            pending_bullet = False
            continue
        if pending_bullet and s:
            lines.append(f"- {s}")
            pending_bullet = False
        else:
            lines.append(line)
            pending_bullet = False
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


_EXPENSE_WIZARD_POLISH_RULES = frozenset(
    {
        "EXPENSE_WORKFLOW_COLLECTING",
        "EXPENSE_WORKFLOW_REVIEW",
        "EXPENSE_WORKFLOW_SUBMIT_CONFIRM",
    }
)
_LEAVE_WIZARD_POLISH_RULES = frozenset(
    {"LEAVE_WORKFLOW_COLLECTING", "LEAVE_WORKFLOW_AWAITING_CONFIRMATION"}
)
_EXPENSE_WIZARD_SKIP_POLISH_RULES = frozenset(
    {
        "EXPENSE_WORKFLOW_SUBMITTED",
        "EXPENSE_WORKFLOW_EMPTY",
    }
)


def _should_llm_polish_expense_wizard(rules_applied: list[str] | None) -> bool:
    rules = {str(r or "") for r in (rules_applied or [])}
    if rules & _EXPENSE_WIZARD_SKIP_POLISH_RULES:
        return False
    return bool(rules & _EXPENSE_WIZARD_POLISH_RULES)


def _should_llm_polish_leave_wizard(rules_applied: list[str] | None) -> bool:
    rules = {str(r or "") for r in (rules_applied or [])}
    return bool(rules & _LEAVE_WIZARD_POLISH_RULES)


def polish_outbound_message(
    message: str,
    *,
    intent: str,
    outcome: str,
    user_message: str,
    entities: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    crm_payload: dict[str, Any] | None = None,
    trace_id: str | None = None,
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
        card = format_leave_submitted_message(
            entities=entities,
            decision=decision,
            reference_id=ref,
            deduped=bool(crm_payload.get("_deduped")),
            lang=lang,
        )
        if trace_id and not crm_payload.get("_deduped"):
            from chat.services.message_polish_llm import (
                leave_facts_preserved,
                polish_template_message,
            )

            polished = polish_template_message(
                card,
                user_message=user_message,
                message_type="leave_submitted",
                trace_id=trace_id,
                user_lang=lang,
                min_length=30,
            )
            if leave_facts_preserved(card, polished):
                return polished
        return card

    if (
        intent == INTENT_EXPENSE_CLAIM
        and outcome == "SUBMITTED"
        and trace_id
    ):
        from chat.services.expense_message_facts import build_submit_success_envelope
        from chat.services.message_polish_llm import polish_expense_message_with_envelope

        items = list(entities.get("expense_items") or [])
        total = sum(float(r.get("amount") or 0) for r in items)
        ref = str(crm_payload.get("request_id") or "").strip()
        sub = crm_payload.get("expense_submission") or {}
        if not ref:
            ref = str(sub.get("reference_id") or sub.get("request_id") or "")
        inc = str(entities.get("expense_incurred_date") or "")
        reply_lang = str(
            ((crm_payload.get("expense_wizard") or {}).get("reply_language"))
            or lang
        )
        if reply_lang not in ("bn", "en", "banglish"):
            reply_lang = lang
        envelope = build_submit_success_envelope(
            item_count=len(items),
            total=total,
            incurred_date_iso=inc,
            reference_id=ref,
            lang=reply_lang if reply_lang in ("bn", "en", "banglish") else "bn",  # type: ignore[arg-type]
            template=msg,
        )
        polished = polish_expense_message_with_envelope(
            envelope,
            user_message=user_message,
            trace_id=trace_id,
        )
        if polished and polished.strip():
            return collapse_pdf_line_breaks(polished)

    if outcome == "NEEDS_CLARIFICATION":
        rules = list(decision.get("rules_applied") or [])
        if intent == INTENT_LEAVE_REQUEST and trace_id:
            review = (
                "LEAVE_WORKFLOW_AWAITING_CONFIRMATION" in rules
                or "জমা দেবেন" in msg
                or "জমা হয়নি" in msg
                or "submit hoyni" in msg.lower()
            )
            if _should_llm_polish_leave_wizard(rules) or review:
                from chat.services.message_polish_llm import polish_leave_wizard_message

                polished = polish_leave_wizard_message(
                    msg,
                    user_message=user_message,
                    trace_id=trace_id,
                    review=review,
                )
                return polish_clarification_message(polished)
        if (
            intent == INTENT_EXPENSE_CLAIM
            and trace_id
            and _should_llm_polish_expense_wizard(rules)
        ):
            expense_facts = entities.get("expense_message_facts")
            if isinstance(expense_facts, dict) and (
                expense_facts.get("polishable_part")
                or expense_facts.get("ask_envelope")
                or expense_facts.get("message_type")
                in (
                    "expense_validation_block",
                    "expense_clarify",
                    "expense_clarify_praise_review",
                    "expense_review_praise",
                    "expense_submit_confirm",
                )
            ):
                from chat.services.message_polish_llm import (
                    polish_expense_message_with_envelope,
                )

                polished = polish_expense_message_with_envelope(
                    expense_facts,
                    user_message=user_message,
                    trace_id=trace_id,
                )
                if polished:
                    return polish_clarification_message(polished)
            from chat.services.message_polish_llm import polish_template_message

            polished = polish_template_message(
                msg,
                user_message=user_message,
                message_type="expense_wizard",
                trace_id=trace_id,
                min_length=30,
            )
            return polish_clarification_message(polished)
        return polish_clarification_message(msg)

    if intent == INTENT_EXPENSE_DAY_SUMMARY and trace_id:
        from chat.services.expense_message_facts import build_session_ledger_envelope
        from chat.services.message_polish_llm import polish_expense_day_recap_message

        ledger = crm_payload.get("session_expense_ledger")
        ledger_dict = ledger if isinstance(ledger, dict) else {}
        recap_lang = detect_user_language(user_message or msg)
        envelope = build_session_ledger_envelope(
            ledger_dict,
            template=msg,
            lang=recap_lang if recap_lang in ("bn", "en", "banglish") else "bn",
        )
        polished = polish_expense_day_recap_message(
            msg,
            envelope=envelope,
            user_message=user_message,
            trace_id=trace_id,
        )
        if polished and polished.strip():
            return collapse_pdf_line_breaks(polished)

    return collapse_pdf_line_breaks(msg)
