"""
Natural leave wizard follow-up questions with collected-field acknowledgment.

Template-based (no LLM) — predictable, testable, bilingual-friendly.
"""

from __future__ import annotations

from typing import Any

from chat.services.leave_slot_extraction import LeaveSlotExtraction
from chat.services.leave_slots import (
    SLOT_DATE_CLARIFY,
    SLOT_DATES,
    SLOT_DOCUMENT,
    SLOT_LEAVE_TYPE,
    SLOT_PAYMENT,
    SLOT_REASON,
    SLOT_SCOPE,
)

_WIZ_MARKER = "_(ছুটি আবেদন — নিচে উত্তর দিন)_"


class LeaveConversationManager:
    """Build contextual wizard prompts — acknowledge collected, ask only missing."""

    def build_follow_up(
        self,
        draft: dict[str, Any],
        *,
        primary_slot: str,
        missing: list[str],
        date_error: str | None = None,
        extraction: LeaveSlotExtraction | None = None,
    ) -> str:
        if primary_slot == SLOT_DATE_CLARIFY and extraction and extraction.clarification_needed:
            return extraction.clarification_needed + _WIZ_MARKER

        ack = self._acknowledge_collected(draft, missing)
        body = self._build_ask_body(
            primary_slot,
            draft=draft,
            missing=missing,
            date_error=date_error,
        )
        if ack and body:
            return f"{ack}{body}{_WIZ_MARKER}"
        if body:
            return f"{body}{_WIZ_MARKER}"
        return "আর একটু তথ্য দরকার — নিচে লিখে পাঠান।" + _WIZ_MARKER

    def _acknowledge_collected(
        self, draft: dict[str, Any], missing: list[str]
    ) -> str:
        lines: list[str] = []
        missing_set = set(missing)

        if (
            draft.get("start_date")
            and SLOT_DATES not in missing_set
            and SLOT_DATE_CLARIFY not in missing_set
        ):
            lines.append(self._ack_date(draft))

        if draft.get("reason") and SLOT_REASON not in missing_set:
            lines.append(self._ack_reason(draft))

        if draft.get("leave_payment_category") and SLOT_PAYMENT not in missing_set:
            lines.append(self._ack_payment(draft))

        if draft.get("day_scope") and SLOT_SCOPE not in missing_set:
            lines.append(self._ack_scope(draft))

        if not lines:
            return ""
        return "\n".join(lines[:3]) + "\n\n"

    @staticmethod
    def _ack_date(draft: dict[str, Any]) -> str:
        start = str(draft.get("start_date") or "").split("T")[0]
        end = str(draft.get("end_date") or start or "").split("T")[0]
        if end and end != start:
            return f"ছুটির তারিখ **{start}** থেকে **{end}** — আমার কাছে আছে।"
        return f"ছুটির তারিখ **{start}** — আমার কাছে আছে।"

    @staticmethod
    def _ack_reason(draft: dict[str, Any]) -> str:
        reason = str(draft.get("reason") or "").strip()
        if len(reason) > 72:
            reason = reason[:69] + "..."
        return f"কারণ: {reason} — নোট করা হয়েছে।"

    @staticmethod
    def _ack_payment(draft: dict[str, Any]) -> str:
        pay = str(draft.get("leave_payment_category") or "").strip().lower()
        label = "Paid" if pay == "paid" else "Unpaid"
        return f"**{label}** leave — ঠিক আছে।"

    @staticmethod
    def _ack_scope(draft: dict[str, Any]) -> str:
        scope = str(draft.get("day_scope") or "").strip().lower()
        label = "Full day" if scope == "full" else "Half day"
        return f"**{label}** — ঠিক আছে।"

    def _build_ask_body(
        self,
        primary_slot: str,
        *,
        draft: dict[str, Any],
        missing: list[str],
        date_error: str | None = None,
    ) -> str:
        missing_set = set(missing)

        if (
            primary_slot == SLOT_PAYMENT
            and SLOT_SCOPE in missing_set
            and SLOT_PAYMENT in missing_set
        ):
            return (
                "এখন জানাবেন:\n"
                "• **Paid** নাকি **unpaid**?\n"
                "• **Full Day** নাকি **Half Day**?\n"
                "(যেমন: paid, full day — একসাথে লিখলেও চলবে)"
            )

        if primary_slot in (SLOT_LEAVE_TYPE, SLOT_PAYMENT):
            return (
                "**Select Leave** — paid নাকি unpaid?\n"
                "• paid\n"
                "• unpaid"
            )

        if primary_slot == SLOT_SCOPE:
            return (
                "**Full Day** নাকি **Half Day**?\n"
                "(full / half লিখলেও চলবে)"
            )

        if primary_slot == SLOT_DATES:
            if date_error == "IN_PAST":
                return "আজকের আগের তারিখে ছুটি দেওয়া যাবে না। আজ বা পরের দিন দিন।"
            if date_error == "BAD_RANGE":
                return "শেষ তারিখ যেন প্রথম তারিখের আগে না হয় — আবার লিখুন।"
            return (
                "**কোন তারিখ(গুলো)** ছুটি চান?\n"
                "• এক দিন: কাল / আগামীকাল / ২০২৬-০৫-১৫\n"
                "• একাধিক: ২০২৬-০৫-১২ থেকে ২০২৬-০৫-১৪"
            )

        if primary_slot == SLOT_REASON:
            return (
                "Reason টা এক লাইনে লিখুন।\n"
                "(যেমন: sick, family কাজ, travel — বাংলা/English যেকোনো)"
            )

        if primary_slot == SLOT_DOCUMENT:
            return (
                "এই ছুটির জন্য **ডাক্তারের চিট** বা কাগজ দিতে পারেন?\n"
                "আপলোড/পেস্ট করুন, অথবা এখন না হলে **skip** লিখুন — ম্যানেজার দেখবেন।"
            )

        return "আর একটু তথ্য দরকার — নিচে লিখে পাঠান।"
