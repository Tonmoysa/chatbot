"""
Natural expense wizard follow-up questions with collected-line acknowledgment.

Template-based (no LLM) — predictable, testable, bilingual-friendly.
"""

from __future__ import annotations

from typing import Any

from chat.services.expense.slots import (
    SLOT_CATEGORY,
    SLOT_FROM_TO,
    SLOT_INCURRED_DATE,
    SLOT_ITEMS,
    SLOT_MORE_LINES,
    SLOT_REVIEW,
    SLOT_SUBMIT_CONFIRM,
)
from chat.services.expense_copy import (
    ReplyLang,
    ask_category_prompt,
    ask_from_to_prompt,
    ask_more_lines_prompt,
    collect_start_prompt,
    format_expense_line_bullet,
    grouped_expense_ack_header,
    review_confirm_footer,
    submit_confirm_prompt,
)


class ExpenseConversationManager:
    """Build contextual wizard prompts — acknowledge collected, ask only missing."""

    def build_follow_up(
        self,
        block: dict[str, Any],
        items: list[dict[str, Any]],
        *,
        primary_slot: str,
        missing: list[str],
        lang: ReplyLang,
        pending_line: dict[str, Any] | None = None,
        incurred_date_iso: str = "",
        warnings: list[str] | None = None,
    ) -> str:
        ack, ask, _meta = self.compose_follow_up_parts(
            block,
            items,
            primary_slot=primary_slot,
            missing=missing,
            lang=lang,
            pending_line=pending_line,
            incurred_date_iso=incurred_date_iso,
            warnings=warnings,
        )
        if ack and ask:
            return f"{ack}{ask}"
        if ask:
            return ask
        return collect_start_prompt(lang)

    def compose_follow_up_parts(
        self,
        block: dict[str, Any],
        items: list[dict[str, Any]],
        *,
        primary_slot: str,
        missing: list[str],
        lang: ReplyLang,
        pending_line: dict[str, Any] | None = None,
        incurred_date_iso: str = "",
        warnings: list[str] | None = None,
    ) -> tuple[str, str, dict[str, Any] | None]:
        """Return (ack_or_summary_body, ask_or_footer, facts envelope)."""
        if primary_slot == SLOT_SUBMIT_CONFIRM:
            ask = submit_confirm_prompt(lang)
            return "", ask, None
        if primary_slot == SLOT_REVIEW:
            from chat.services.expense_workflow import format_expense_summary

            full = format_expense_summary(
                items,
                incurred_date_iso=incurred_date_iso,
                warnings=warnings,
                lang=lang,
            )
            footer = review_confirm_footer(lang)
            body = full
            if footer and full.rstrip().endswith(footer):
                body = full[: full.rfind(footer)].rstrip() + "\n\n"
            from chat.services.expense_message_facts import build_wizard_message_meta

            meta = build_wizard_message_meta(
                ack=body,
                ask=footer,
                items=items,
                incurred_date_iso=incurred_date_iso,
                lang=lang,
                primary_slot=primary_slot,
                warnings=warnings,
            )
            return body, footer, meta

        pending = pending_line if isinstance(pending_line, dict) else None
        if pending is None:
            raw_pending = block.get("pending_line")
            pending = raw_pending if isinstance(raw_pending, dict) else None

        ack = self._acknowledge_collected(
            items,
            missing=missing,
            lang=lang,
            incurred_date_iso=incurred_date_iso,
            pending=pending,
            primary_slot=primary_slot,
        )
        body = self._build_ask_body(
            primary_slot,
            block=block,
            items=items,
            missing=missing,
            lang=lang,
            pending=pending,
            skip_lead=bool(ack) and primary_slot == SLOT_FROM_TO,
            ack_seed=incurred_date_iso or str(len(items)),
        )
        from chat.services.expense_message_facts import build_wizard_message_meta

        meta = build_wizard_message_meta(
            ack=ack,
            ask=body,
            items=items,
            incurred_date_iso=incurred_date_iso,
            lang=lang,
            primary_slot=primary_slot,
            pending=pending,
            warnings=warnings,
        )
        return ack, body, meta

    def _acknowledge_collected(
        self,
        items: list[dict[str, Any]],
        *,
        missing: list[str],
        lang: ReplyLang,
        incurred_date_iso: str,
        pending: dict[str, Any] | None,
        primary_slot: str,
    ) -> str:
        missing_set = set(missing)
        bullets: list[str] = []

        if items and SLOT_ITEMS not in missing_set and primary_slot != SLOT_ITEMS:
            for row in items[-4:]:
                bullets.append(format_expense_line_bullet(row, lang))

        if (
            pending
            and primary_slot == SLOT_FROM_TO
            and pending.get("category")
            and pending.get("amount")
        ):
            pending_row = {
                "category": pending.get("category"),
                "amount": pending.get("amount"),
                "from_location": pending.get("from_location") or "",
                "to_location": pending.get("to_location") or "",
            }
            pending_bullet = format_expense_line_bullet(pending_row, lang)
            if pending_bullet not in bullets:
                bullets.append(pending_bullet)

        show_date = bool(incurred_date_iso and SLOT_INCURRED_DATE not in missing_set)
        if not bullets and not show_date:
            return ""

        seed = f"{incurred_date_iso}:{len(items)}:{len(bullets)}"
        date_for_header = incurred_date_iso if show_date else ""
        header = grouped_expense_ack_header(date_for_header, lang, seed=seed)
        if bullets:
            body = "\n".join(f"- {b}" for b in bullets)
            return f"{header}\n{body}\n\n"
        return f"{header}\n\n"

    def _build_ask_body(
        self,
        primary_slot: str,
        *,
        block: dict[str, Any],
        items: list[dict[str, Any]],
        missing: list[str],
        lang: ReplyLang,
        pending: dict[str, Any] | None,
        skip_lead: bool,
        ack_seed: str = "",
    ) -> str:
        del block, items, missing

        if primary_slot == SLOT_CATEGORY:
            amt = float((pending or {}).get("amount") or 0)
            if amt <= 0:
                return collect_start_prompt(lang)
            return ask_category_prompt(amt, lang, include_lead=True)

        if primary_slot == SLOT_FROM_TO:
            cat = str((pending or {}).get("category") or "Bus")
            amt = float((pending or {}).get("amount") or 0)
            return ask_from_to_prompt(
                cat,
                amt,
                lang,
                include_lead=not skip_lead,
            )

        if primary_slot == SLOT_MORE_LINES:
            return ask_more_lines_prompt(lang, seed=ack_seed)

        if primary_slot == SLOT_ITEMS:
            return collect_start_prompt(lang)

        return collect_start_prompt(lang)
