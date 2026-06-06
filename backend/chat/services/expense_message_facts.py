"""Structured facts envelope for expense ack/summary LLM polish (Phase C)."""

from __future__ import annotations

from typing import Any, Literal

from chat.services.expense_copy import ReplyLang, lang_from_block, review_confirm_footer
from chat.services.expense.slots import (
    SLOT_CATEGORY,
    SLOT_FROM_TO,
    SLOT_ITEMS,
    SLOT_MORE_LINES,
    SLOT_REVIEW,
)

ExpenseMessageType = Literal["expense_ack", "expense_summary", "expense_wizard_prompt"]
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
    if validation_blocked or not question:
        return None
    stage = str(block.get("stage") or "")
    if stage == "submit_confirm":
        return None

    lang = lang_from_block(block)
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
