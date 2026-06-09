"""Structured facts envelope for expense ack/summary LLM polish (Phase C)."""

from __future__ import annotations

from typing import Any, Literal

from chat.services.expense_copy import ReplyLang, lang_from_block, review_confirm_footer
from chat.services.expense_extraction import is_travel_category
from chat.services.expense.slots import (
    SLOT_CATEGORY,
    SLOT_FROM_TO,
    SLOT_ITEMS,
    SLOT_MORE_LINES,
    SLOT_REVIEW,
)

ExpenseMessageType = Literal[
    "expense_ack",
    "expense_summary",
    "expense_wizard_prompt",
    "expense_validation_block",
    "expense_clarify",
    "expense_clarify_praise_review",
    "expense_review_praise",
    "expense_submit_confirm",
    "expense_submit_success",
]
ExpensePromptKind = Literal["category", "from_to", "more_lines", "collect"]

_POLISHABLE_ASK_SLOTS = frozenset(
    {SLOT_CATEGORY, SLOT_FROM_TO, SLOT_MORE_LINES, SLOT_ITEMS}
)


def _line_fact(row: dict[str, Any]) -> dict[str, Any]:
    fact: dict[str, Any] = {
        "category": str(row.get("category") or "Other"),
        "amount": float(row.get("amount") or 0),
    }
    frm = str(row.get("from_location") or "").strip()
    to = str(row.get("to_location") or "").strip()
    if frm:
        fact["from"] = frm
    if to:
        fact["to"] = to
    return fact


def build_ack_envelope(
    items: list[dict[str, Any]],
    *,
    incurred_date_iso: str,
    lang: ReplyLang,
    primary_slot: str,
    pending: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lines = [_line_fact(row) for row in items[-4:]]
    if (
        pending
        and primary_slot == SLOT_FROM_TO
        and pending.get("category")
        and pending.get("amount")
    ):
        pending_fact = _line_fact(
            {
                "category": pending.get("category"),
                "amount": pending.get("amount"),
                "from_location": pending.get("from_location") or "",
                "to_location": pending.get("to_location") or "",
            }
        )
        if pending_fact not in lines:
            lines.append(pending_fact)

    next_ask = {
        SLOT_MORE_LINES: "more_lines",
        SLOT_CATEGORY: "category",
        SLOT_FROM_TO: "from_to",
        SLOT_ITEMS: "items",
    }.get(primary_slot, "collect")

    return {
        "message_type": "expense_ack",
        "lang": lang,
        "facts": {
            "date": incurred_date_iso or None,
            "lines": lines,
            "next_ask": next_ask,
        },
    }


def build_summary_envelope(
    items: list[dict[str, Any]],
    *,
    incurred_date_iso: str,
    warnings: list[str] | None,
    lang: ReplyLang,
) -> dict[str, Any]:
    total = sum(float(r.get("amount") or 0) for r in items)
    return {
        "message_type": "expense_summary",
        "lang": lang,
        "facts": {
            "date": incurred_date_iso or None,
            "lines": [_line_fact(row) for row in items],
            "total": total,
            "warnings": list(warnings or []),
        },
    }


def build_review_praise_envelope(
    *,
    template: str,
    lang: ReplyLang,
    item_count: int = 0,
    total: float = 0.0,
    incurred_date_iso: str = "",
) -> dict[str, Any]:
    """Review-screen praise + submit ask (no expense line list)."""
    return {
        "message_type": "expense_review_praise",
        "lang": lang,
        "polishable_part": (template or "").strip(),
        "template_fallback": (template or "").strip(),
        "facts": {
            "praise_ack": True,
            "review_stage": True,
            "item_count": int(item_count),
            "total": float(total),
            "date": incurred_date_iso or None,
            "ask_submit": True,
        },
    }


def build_clarify_praise_review_envelope(
    *,
    praise_template: str,
    summary_template: str,
    items: list[dict[str, Any]],
    incurred_date_iso: str,
    warnings: list[str] | None,
    lang: ReplyLang,
) -> dict[str, Any]:
    """Praise ack (LLM-polished) + fixed expense review summary."""
    envelope = build_summary_envelope(
        items,
        incurred_date_iso=incurred_date_iso,
        warnings=warnings,
        lang=lang,
    )
    envelope["message_type"] = "expense_clarify_praise_review"
    envelope["polishable_part"] = (praise_template or "").strip()
    envelope["fixed_part"] = (summary_template or "").strip()
    envelope["template_fallback"] = (praise_template or "").strip()
    envelope["facts"]["praise_ack"] = True
    return envelope


def clarify_praise_facts_preserved(envelope: dict[str, Any], polished: str) -> bool:
    """Praise ack only — language lock; expense lines live in fixed_part."""
    from chat.services.expense.clarify_polish import clarify_polish_language_ok

    text = (polished or "").strip()
    template = str(envelope.get("template_fallback") or "")
    target_lang = str(envelope.get("lang") or "bn")
    if not text or len(text) < 8:
        return False
    return clarify_polish_language_ok(target_lang, text, template=template)


def build_session_ledger_envelope(
    ledger: dict[str, Any],
    *,
    template: str,
    lang: ReplyLang = "bn",
) -> dict[str, Any]:
    """Facts envelope for read-only session / day expense recap polish."""
    submitted = list(ledger.get("submitted_batches") or [])
    pending = ledger.get("pending_draft")
    has_data = bool(submitted or pending)
    lines: list[dict[str, Any]] = []
    for batch in submitted:
        for row in list(batch.get("items") or []):
            lines.append(_line_fact(row))
    if isinstance(pending, dict):
        for row in list(pending.get("items") or []):
            lines.append(_line_fact(row))
    facts: dict[str, Any] = {
        "date": ledger.get("incurred_date_iso"),
        "submitted_total": float(ledger.get("submitted_total") or 0),
        "pending_total": float(ledger.get("pending_total") or 0),
        "combined_total": float(ledger.get("combined_total") or 0),
        "daily_cap": float(ledger.get("daily_cap") or 0),
        "over_cap": bool(ledger.get("over_cap")),
        "empty_session": not has_data,
        "lines": lines,
    }
    refs = [
        str(b.get("reference_id") or "").strip()
        for b in submitted
        if str(b.get("reference_id") or "").strip()
    ]
    if refs:
        facts["reference_ids"] = refs
    return {
        "message_type": "expense_day_recap",
        "lang": lang,
        "facts": facts,
        "template_fallback": (template or "").strip(),
    }


def session_recap_facts_preserved(envelope: dict[str, Any], polished: str) -> bool:
    """Verify session recap polish kept totals, lines, and template facts."""
    from chat.services.message_polish_llm import facts_preserved

    text = polished or ""
    facts = envelope.get("facts") or {}
    if facts.get("empty_session"):
        template = str(envelope.get("template_fallback") or "")
        return facts_preserved(template, text) if template else bool(text.strip())

    date = facts.get("date")
    if date and str(date) not in text:
        return False

    for key in ("submitted_total", "pending_total", "combined_total", "daily_cap"):
        if key in facts:
            val = float(facts[key] or 0)
            if val > 0 and f"{val:g}" not in text:
                return False

    for row in facts.get("lines") or []:
        cat = str(row.get("category") or "")
        if cat and cat.lower() not in text.lower():
            return False
        amt = float(row.get("amount") or 0)
        if amt > 0 and f"{amt:g}" not in text:
            return False

    template = str(envelope.get("template_fallback") or "")
    if template:
        return facts_preserved(template, text)
    return bool(text.strip())


def envelope_facts_preserved(envelope: dict[str, Any], polished: str) -> bool:
    """Verify every structured fact appears in polished output."""
    from chat.services.message_polish_llm import facts_preserved

    text = polished or ""
    facts = envelope.get("facts") or {}
    date = facts.get("date")
    if date and str(date) not in text:
        return False

    for row in facts.get("lines") or []:
        cat = str(row.get("category") or "")
        if cat and cat.lower() not in text.lower():
            return False
        amt = float(row.get("amount") or 0)
        if f"{amt:g}" not in text:
            return False
        for key in ("from", "to"):
            val = str(row.get(key) or "").strip()
            if val and val.lower() not in text.lower():
                return False

    if "total" in facts:
        total = float(facts["total"] or 0)
        if f"{total:g}" not in text:
            return False

    template = str(envelope.get("template_fallback") or "")
    if template:
        return facts_preserved(template, text)
    return bool(text.strip())


def build_ask_envelope(
    *,
    ask: str,
    lang: ReplyLang,
    primary_slot: str,
    pending: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Facts envelope for wizard follow-up prompts (Phase D)."""
    if primary_slot not in _POLISHABLE_ASK_SLOTS or not (ask or "").strip():
        return None

    kind_map: dict[str, ExpensePromptKind] = {
        SLOT_CATEGORY: "category",
        SLOT_FROM_TO: "from_to",
        SLOT_MORE_LINES: "more_lines",
        SLOT_ITEMS: "collect",
    }
    prompt_kind = kind_map[primary_slot]
    facts: dict[str, Any] = {"prompt_kind": prompt_kind}
    pending = pending if isinstance(pending, dict) else {}

    if pending.get("amount") is not None:
        facts["amount"] = float(pending.get("amount") or 0)
    if pending.get("category"):
        facts["category"] = str(pending.get("category"))

    if prompt_kind == "category":
        from chat.services.expense_copy import category_options_line

        facts["category_options"] = category_options_line()

    template = ask.strip()
    return {
        "message_type": "expense_wizard_prompt",
        "lang": lang,
        "facts": facts,
        "polishable_part": template,
        "template_fallback": template,
        "skip_polish": False,
    }


def validation_block_facts_preserved(envelope: dict[str, Any], polished: str) -> bool:
    from chat.services.message_polish_llm import facts_preserved

    text = polished or ""
    template = str(envelope.get("template_fallback") or "")
    facts = envelope.get("facts") or {}
    if facts.get("category"):
        cat = str(facts["category"])
        if cat.lower() not in text.lower():
            return False
    if facts.get("amount") is not None:
        amt = f"{float(facts['amount']):g}"
        if amt in template and amt not in text:
            return False
    for row in facts.get("pending_lines") or []:
        cat = str(row.get("category") or "")
        if cat and cat.lower() not in text.lower():
            return False
        amt = float(row.get("amount") or 0)
        if amt > 0 and f"{amt:g}" in template and f"{amt:g}" not in text:
            return False
    if template:
        return facts_preserved(template, text)
    return bool(text.strip())


def _amounts_in_polish_text(text: str) -> set[float]:
    import re

    found: set[float] = set()
    for m in re.finditer(
        r"(?<!\d)([\d০-৯]{1,6})(?:[.,](\d{1,2}))?(?!\d)",
        text or "",
        flags=re.I,
    ):
        whole = str(m.group(1) or "").translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))
        frac = str(m.group(2) or "").translate(str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789"))
        try:
            val = round(float(f"{whole}.{frac}" if frac else whole), 2)
        except ValueError:
            continue
        if 1 <= val <= 500_000:
            found.add(val)
    return found


def _amount_mentioned_in_text(amount: float, text: str) -> bool:
    token = f"{float(amount):g}"
    if token in text:
        return True
    if float(amount).is_integer() and str(int(amount)) in text:
        return True
    return False


def clarify_facts_preserved(envelope: dict[str, Any], polished: str) -> bool:
    from chat.services.expense.clarify_polish import clarify_polish_language_ok
    from chat.services.message_polish_llm import facts_preserved

    text = polished or ""
    template = str(envelope.get("template_fallback") or "")
    target_lang = str(envelope.get("lang") or "bn")
    if not clarify_polish_language_ok(target_lang, text, template=template):
        return False

    issues = list((envelope.get("facts") or {}).get("issues") or [])
    issue_amounts = {
        round(float(row.get("amount") or 0), 2)
        for row in issues
        if row.get("amount") is not None
    }
    template_amounts = _amounts_in_polish_text(template)
    polished_amounts = _amounts_in_polish_text(text)
    # Reject polish that adds questions for amounts outside the open issues list.
    if issue_amounts:
        for amt in polished_amounts:
            if amt in template_amounts:
                continue
            if amt >= 10 and amt not in issue_amounts:
                return False
        for amt in issue_amounts:
            if not _amount_mentioned_in_text(amt, text):
                return False

    for row in issues:
        cat = str(row.get("category") or "")
        if cat and cat.lower() not in text.lower():
            return False
        original = str(row.get("original") or "").strip()
        if original and original.lower() not in text.lower():
            return False
        suggestion = str(row.get("suggestion") or "").strip()
        if suggestion and suggestion.lower() not in text.lower():
            return False
    if template:
        return facts_preserved(template, text)
    return bool(text.strip())


def submit_success_facts_preserved(envelope: dict[str, Any], polished: str) -> bool:
    from chat.services.message_polish_llm import facts_preserved

    text = polished or ""
    facts = envelope.get("facts") or {}
    date = facts.get("date")
    if date and str(date) not in text:
        return False
    template = str(envelope.get("template_fallback") or "")
    if facts.get("line_count") is not None:
        count = int(facts["line_count"])
        if count > 0 and str(count) in template and str(count) not in text:
            return False
    if facts.get("total") is not None:
        total = f"{float(facts['total']):g}"
        if total in template and total not in text:
            return False
    ref = str(facts.get("reference_id") or "").strip()
    if ref and ref in template and ref not in text:
        return False
    if template:
        return facts_preserved(template, text)
    return bool(text.strip())


def prompt_envelope_facts_preserved(envelope: dict[str, Any], polished: str) -> bool:
    """Verify prompt facts (amount, category) survive polish."""
    from chat.services.message_polish_llm import facts_preserved

    text = polished or ""
    facts = envelope.get("facts") or {}
    if facts.get("amount") is not None:
        if f"{float(facts['amount']):g}" not in text:
            return False
    category = str(facts.get("category") or "").strip()
    if category and category.lower() not in text.lower():
        return False
    template = str(envelope.get("template_fallback") or "")
    if template:
        return facts_preserved(template, text)
    return bool(text.strip())


def _attach_ask_envelope(
    envelope: dict[str, Any],
    *,
    ask: str,
    lang: ReplyLang,
    primary_slot: str,
    pending: dict[str, Any] | None,
) -> dict[str, Any]:
    ask_env = build_ask_envelope(
        ask=ask,
        lang=lang,
        primary_slot=primary_slot,
        pending=pending,
    )
    if ask_env:
        envelope["ask_envelope"] = ask_env
    envelope["fixed_part"] = (ask or "").strip()
    return envelope


def build_wizard_message_meta(
    *,
    ack: str,
    ask: str,
    items: list[dict[str, Any]],
    incurred_date_iso: str,
    lang: ReplyLang,
    primary_slot: str,
    pending: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any] | None:
    """Build polish metadata for ack/summary and/or wizard ask prompts."""
    ask = (ask or "").strip()

    if primary_slot == SLOT_REVIEW:
        if not items:
            return None
        envelope = build_summary_envelope(
            items,
            incurred_date_iso=incurred_date_iso,
            warnings=warnings,
            lang=lang,
        )
        body = (ack or "").strip()
        footer = review_confirm_footer(lang)
        envelope["polishable_part"] = body
        envelope["template_fallback"] = body
        return _attach_ask_envelope(
            envelope,
            ask=footer if ask == footer else ask,
            lang=lang,
            primary_slot=primary_slot,
            pending=pending,
        )

    ask_env = build_ask_envelope(
        ask=ask,
        lang=lang,
        primary_slot=primary_slot,
        pending=pending,
    )

    if (ack or "").strip():
        if primary_slot in (SLOT_CATEGORY, SLOT_FROM_TO, SLOT_ITEMS) and not items:
            return None
        envelope = build_ack_envelope(
            items,
            incurred_date_iso=incurred_date_iso,
            lang=lang,
            primary_slot=primary_slot,
            pending=pending,
        )
        envelope["polishable_part"] = ack.strip()
        envelope["template_fallback"] = ack.strip()
        return _attach_ask_envelope(
            envelope,
            ask=ask,
            lang=lang,
            primary_slot=primary_slot,
            pending=pending,
        )

    if ask_env:
        return {
            "message_type": "expense_wizard_prompt",
            "lang": lang,
            "polishable_part": "",
            "fixed_part": ask,
            "ask_envelope": ask_env,
            "template_fallback": ask,
        }
    return None


def _split_submit_confirm_parts(template: str) -> tuple[str, str]:
    """Intro (polishable) vs yes/no footer (fixed)."""
    text = (template or "").strip()
    for marker in (
        "**Expense CRM",
        "**Submit expense",
        "**Expense CRM-এ",
    ):
        idx = text.find(marker)
        if idx > 0:
            return text[:idx].rstrip(), text[idx:].strip()
    return text, ""


def build_submit_confirm_envelope(
    template: str,
    *,
    lang: ReplyLang,
) -> dict[str, Any]:
    intro, footer = _split_submit_confirm_parts(template)
    return {
        "message_type": "expense_submit_confirm",
        "lang": lang,
        "polishable_part": intro or template.strip(),
        "fixed_part": footer,
        "template_fallback": template.strip(),
        "facts": {"prompt_kind": "submit_confirm"},
    }


def build_submit_success_envelope(
    *,
    item_count: int,
    total: float,
    incurred_date_iso: str,
    reference_id: str,
    lang: ReplyLang,
    template: str,
) -> dict[str, Any]:
    return {
        "message_type": "expense_submit_success",
        "lang": lang,
        "polishable_part": template.strip(),
        "template_fallback": template.strip(),
        "facts": {
            "date": incurred_date_iso or None,
            "line_count": int(item_count),
            "total": float(total),
            "reference_id": (reference_id or "").strip() or None,
        },
    }


def build_clarify_envelope(
    issues: list[Any],
    *,
    template: str,
    lang: ReplyLang,
    prompt_variant: str = "initial",
    resolved_count: int = 0,
    total_issues: int = 0,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for issue in issues:
        if hasattr(issue, "to_dict"):
            raw = issue.to_dict()
        elif isinstance(issue, dict):
            raw = issue
        else:
            continue
        row: dict[str, Any] = {"kind": str(raw.get("kind") or "")}
        if raw.get("category"):
            row["category"] = str(raw.get("category"))
        if raw.get("amount"):
            row["amount"] = float(raw.get("amount") or 0)
        if raw.get("original"):
            row["original"] = str(raw.get("original"))
        if raw.get("suggestion"):
            row["suggestion"] = str(raw.get("suggestion"))
        if raw.get("field"):
            row["field"] = str(raw.get("field"))
        rows.append(row)
    open_count = len(rows)
    total = total_issues or open_count
    return {
        "message_type": "expense_clarify",
        "lang": lang,
        "polishable_part": template.strip(),
        "template_fallback": template.strip(),
        "facts": {
            "issues": rows,
            "prompt_variant": prompt_variant,
            "resolved_count": resolved_count,
            "total_issues": total,
            "open_count": open_count,
        },
    }


def build_validation_block_envelope(
    template: str,
    *,
    items: list[dict[str, Any]],
    block: dict[str, Any],
    lang: ReplyLang,
) -> dict[str, Any]:
    text = (template or "").strip()
    facts: dict[str, Any] = {}
    low = text.lower()
    if "from" in low and "to" in low:
        facts["block_kind"] = "missing_from_to"
        for row in items:
            cat = str(row.get("category") or "").strip()
            if cat and is_travel_category(cat):
                frm = str(row.get("from_location") or "").strip()
                to = str(row.get("to_location") or "").strip()
                if not frm or not to:
                    facts["category"] = cat
                    facts["amount"] = float(row.get("amount") or 0)
                    break
    elif "pending" in low or "finish pending" in low or "shesh korun" in low:
        facts["block_kind"] = "pending_lines"
        pending_rows: list[dict[str, Any]] = []
        pending = block.get("pending_line") if isinstance(block.get("pending_line"), dict) else {}
        if pending.get("amount"):
            pending_rows.append(
                {
                    "category": str(pending.get("category") or "line"),
                    "amount": float(pending.get("amount") or 0),
                }
            )
        for qrow in list(block.get("pending_queue") or []):
            cat = str(qrow.get("category") or "").strip()
            amt = float(qrow.get("amount") or 0)
            if cat and amt > 0:
                pending_rows.append({"category": cat, "amount": amt})
        if pending_rows:
            facts["pending_lines"] = pending_rows
    elif "category" in low:
        facts["block_kind"] = "missing_category"
    else:
        facts["block_kind"] = "generic"
    return {
        "message_type": "expense_validation_block",
        "lang": lang,
        "polishable_part": text,
        "template_fallback": text,
        "facts": facts,
    }


def message_meta_from_block(
    block: dict[str, Any],
    items: list[dict[str, Any]],
    question: str,
    *,
    incurred_date_iso: str = "",
    warnings: list[str] | None = None,
    validation_blocked: bool = False,
    primary_slot: str | None = None,
) -> dict[str, Any] | None:
    """Fallback meta builder when compose_follow_up_parts was not used."""
    if not question:
        return None

    lang = lang_from_block(block)

    if validation_blocked:
        return build_validation_block_envelope(
            question,
            items=items,
            block=block,
            lang=lang,
        )

    stage = str(block.get("stage") or "")
    if stage == "submit_confirm":
        return build_submit_confirm_envelope(question, lang=lang)

    if str(block.get("pending_step") or "") == "clarify":
        from chat.services.expense.clarify import deserialize_clarification_issues

        issues = deserialize_clarification_issues(block.get("clarification_issues"))
        if issues:
            return build_clarify_envelope(
                issues,
                template=question,
                lang=lang,
                prompt_variant="initial",
            )

    pending = block.get("pending_line") if isinstance(block.get("pending_line"), dict) else None

    if stage in ("review", "confirming") or primary_slot == SLOT_REVIEW:
        footer = review_confirm_footer(lang)
        body = question
        if footer in question:
            body = question[: question.rfind(footer)].rstrip()
        meta = build_summary_envelope(
            items,
            incurred_date_iso=incurred_date_iso,
            warnings=warnings,
            lang=lang,
        )
        meta["polishable_part"] = body
        meta["template_fallback"] = body
        return _attach_ask_envelope(
            meta,
            ask=footer if footer in question else "",
            lang=lang,
            primary_slot=SLOT_REVIEW,
            pending=pending,
        )

    slot = primary_slot or str(block.get("pending_step") or SLOT_MORE_LINES)

    if not items or "- **" not in question:
        ask_only = build_ask_envelope(
            ask=question,
            lang=lang,
            primary_slot=slot,
            pending=pending,
        )
        if ask_only:
            return {
                "message_type": "expense_wizard_prompt",
                "lang": lang,
                "polishable_part": "",
                "fixed_part": question.strip(),
                "ask_envelope": ask_only,
                "template_fallback": question.strip(),
            }
        return None
    ack_end = question.find("\n\n", question.find("- **"))
    if ack_end <= 0:
        return None
    ack = question[: ack_end + 2]
    ask = question[ack_end + 2 :]
    return build_wizard_message_meta(
        ack=ack,
        ask=ask,
        items=items,
        incurred_date_iso=incurred_date_iso,
        lang=lang,
        primary_slot=slot,
        pending=pending,
        warnings=warnings,
    )
