"""
Natural leave wizard follow-up questions with collected-field acknowledgment.

Template-based (no LLM) — predictable, testable, bilingual-friendly.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from chat.services.leave_draft_utils import format_half_day_period_label, format_select_leave_label
from chat.services.leave_slot_extraction import LeaveSlotExtraction
from chat.services.leave_slots import (
    SLOT_DATE_CLARIFY,
    SLOT_DATES,
    SLOT_DOCUMENT,
    SLOT_HALF_PERIOD,
    SLOT_LEAVE_TYPE,
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

        if draft.get("days") and SLOT_DATES in missing_set:
            try:
                n = int(float(draft["days"]))
                if n > 1:
                    lines.append(f"**{n} দিনের** ছুটি — নোট করা হয়েছে।")
            except (TypeError, ValueError):
                pass

        if (
            draft.get("start_date")
            and SLOT_DATES not in missing_set
            and SLOT_DATE_CLARIFY not in missing_set
        ):
            lines.append(self._ack_date(draft))

        if draft.get("reason") and SLOT_REASON not in missing_set:
            lines.append(self._ack_reason(draft))

        if draft.get("leave_type") and SLOT_LEAVE_TYPE not in missing_set:
            lines.append(self._ack_leave_type(draft))

        if draft.get("day_scope") and SLOT_SCOPE not in missing_set:
            lines.append(self._ack_scope(draft))

        if draft.get("half_day_period") and SLOT_HALF_PERIOD not in missing_set:
            lines.append(self._ack_half_period(draft))

        if not lines:
            return ""
        return "\n".join(lines[:5]) + "\n\n"

    @staticmethod
    def _ack_date(draft: dict[str, Any]) -> str:
        start = str(draft.get("start_date") or "").split("T")[0]
        end = str(draft.get("end_date") or start or "").split("T")[0]
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        if start == tomorrow and (not end or end == start):
            return f"**আগামীকাল** ({start})-এর ছুটি — নোট করা হয়েছে।"
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
    def _ack_leave_type(draft: dict[str, Any]) -> str:
        label = format_select_leave_label(draft)
        return f"**{label}** — ঠিক আছে।"

    @staticmethod
    def _ack_scope(draft: dict[str, Any]) -> str:
        scope = str(draft.get("day_scope") or "").strip().lower()
        label = "Full day" if scope == "full" else "Half day"
        return f"**{label}** — ঠিক আছে।"

    @staticmethod
    def _ack_half_period(draft: dict[str, Any]) -> str:
        label = format_half_day_period_label(draft)
        return f"**{label}** — ঠিক আছে।"

    def _build_dates_question(self, draft: dict[str, Any], date_error: str | None) -> str:
        if date_error == "IN_PAST":
            return "আজকের আগের তারিখে ছুটি দেওয়া যাবে না। আজ বা পরের দিন দিন।"
        if date_error == "BAD_RANGE":
            return "শেষ তারিখ যেন প্রথম তারিখের আগে না হয় — আবার লিখুন।"

        today = date.today()
        tomorrow = today + timedelta(days=1)
        tomorrow_s = tomorrow.isoformat()
        today_s = today.isoformat()

        n_days: int | None = None
        try:
            if draft.get("days"):
                n_days = int(float(draft["days"]))
        except (TypeError, ValueError):
            n_days = None

        if n_days and n_days > 1:
            example_end = (today + timedelta(days=n_days - 1)).isoformat()
            return (
                f"আপনি **{n_days} দিনের** ছুটি চেয়েছেন। ছুটি **কোন তারিখ থেকে** শুরু হবে?\n"
                f"• **আগামীকাল** ({tomorrow_s}) থেকে\n"
                f"• নির্দিষ্ট শুরুর তারিখ — যেমন **{today_s}**\n"
                f"• অথবা রেঞ্জ লিখুন — **{today_s}** থেকে **{example_end}**"
            )

        return (
            "ছুটি **কোন তারিখে** চান?\n"
            f"• **আগামীকাল** ({tomorrow_s}) বা **কাল**\n"
            f"• নির্দিষ্ট তারিখ — যেমন **{today_s}**"
        )

    def _build_ask_body(
        self,
        primary_slot: str,
        *,
        draft: dict[str, Any],
        missing: list[str],
        date_error: str | None = None,
    ) -> str:
        missing_set = set(missing)
        from chat.services.leave_draft_utils import is_non_sick_wizard_leave

        non_sick = is_non_sick_wizard_leave(draft)

        if (
            primary_slot == SLOT_LEAVE_TYPE
            and SLOT_SCOPE in missing_set
            and SLOT_LEAVE_TYPE in missing_set
        ):
            if non_sick:
                return (
                    "এখন জানাবেন:\n"
                    "• **Annual leave** / **Leave without pay**\n"
                    "• **Full Day** নাকি **Half Day**?\n"
                    "(যেমন: annual leave, full day — একসাথে লিখলেও চলবে)"
                )
            return (
                "এখন জানাবেন:\n"
                "• **Sick leave** / **Annual leave** / **Leave without pay**\n"
                "• **Full Day** নাকি **Half Day**?\n"
                "(যেমন: sick leave, full day — একসাথে লিখলেও চলবে)"
            )

        if primary_slot == SLOT_LEAVE_TYPE:
            if non_sick:
                return (
                    "**Select Leave** — কোন ধরনের ছুটি?\n"
                    "• **annual leave** (বার্ষিক)\n"
                    "• **leave without pay** (বেতন ছাড়া)"
                )
            return (
                "**Select Leave** — কোন ধরনের ছুটি?\n"
                "• **sick leave** (অসুস্থতা)\n"
                "• **annual leave** (বার্ষিক)\n"
                "• **leave without pay** (বেতন ছাড়া)"
            )

        if primary_slot == SLOT_SCOPE:
            return (
                "**Full Day** নাকি **Half Day**?\n"
                "(full / half লিখলেও চলবে)"
            )

        if primary_slot == SLOT_HALF_PERIOD:
            return (
                "হাফ ডে ছুটি — **প্রথম অর্ধ** নাকি **দ্বিতীয় অর্ধ**?\n"
                "(first half / second half / সকাল / বিকেল)"
            )

        if primary_slot == SLOT_DATES:
            return self._build_dates_question(draft, date_error)

        if primary_slot == SLOT_REASON:
            return (
                "Reason টা এক লাইনে লিখুন (ঐচ্ছিক)।\n"
                "(যেমন: family কাজ, travel — বাংলা/English যেকোনো; না থাকলে **skip** লিখুন)"
            )

        if primary_slot == SLOT_DOCUMENT:
            return (
                "অসুস্থতার ছুটির জন্য **ডাক্তারের প্রাসঙ্গিক কাগজ দরকার হতে পারে।\n"
                "এখন **আপলোড/পেস্ট** করুন, না হলে **skip** বা **parbo na** লিখুন — "
                "ম্যানেজার রিভিউ নেবেন।"
            )

        return "আর একটু তথ্য দরকার — নিচে লিখে পাঠান।"
