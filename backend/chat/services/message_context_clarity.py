"""
Detect underspecified user replies and build professional clarification prompts.

When the user sends a short fragment (e.g. "7 days") without enough context for a
safe HR action, the orchestrator asks one friendly clarification instead of
guessing (e.g. showing leave balance).
"""

from __future__ import annotations

import re

from chat.constants import INTENT_LEAVE_BALANCE, INTENT_LEAVE_REQUEST, INTENT_UNKNOWN
from chat.services.translator import detect_reply_language, detect_user_language

_DURATION_ONLY_RE = re.compile(
    r"^(?:ha+|haa|hmm+|hm+|yes|yeah|yep|yup|ok|okay|ji|jee|thik|ঠিক)?\s*"
    r"(?P<n>\d{1,3})\s*(?:days?|din|দিন)\s*[!.?…]*\s*$",
    re.I,
)

_EXPLICIT_HR_ACTION_RE = re.compile(
    r"\b(balance|remaining|baki|baaki|pto|vacation\s*left|"
    r"apply|request|submit|book|need|take|lagbe|lage|chai|chuti|chhuti|leave|"
    r"expense|kharcha|khoroch|reimburse|taka|"
    r"policy|policies|rules?|handbook|niyom|niti)\b",
    re.I,
)
_EXPLICIT_HR_ACTION_BN_RE = re.compile(
    r"(ছুটি\s*কত|কত\s*দিন\s*আছে|ব্যালান্স|বাকি|আবেদন|জমা|খরচ|নিয়ম|পলিসি|"
    r"ছুটি.{0,15}(?:চাই|লাগবে|নিতে)|"
    r"আবার.{0,25}(?:ছুটি|leave))"
)

_LEAVE_WIZARD_ASSISTANT_MARKERS = (
    "Leave dates or duration required",
    "ছুটি ফর্ম",
    "ছুটি আবেদন",
    "**Step ",
    "Step 3 of 5",
    "কোন তারিখ",
    "ছুটি আবেদন — নিচে উত্তর দিন",
)


def looks_underspecified_message(message: str) -> bool:
    """True when the message is too short/vague to safely route to an HR action."""
    raw = (message or "").strip()
    if not raw or len(raw) > 120:
        return False
    try:
        from chat.services.policy_intent_helpers import (
            is_general_knowledge_out_of_scope,
            is_off_topic_for_hr_assistant,
        )

        if is_general_knowledge_out_of_scope(raw) or is_off_topic_for_hr_assistant(raw):
            return False
    except Exception:
        pass
    if _DURATION_ONLY_RE.match(raw):
        return True
    words = re.findall(r"\S+", raw)
    if len(words) <= 5 and re.search(r"\d", raw):
        if _EXPLICIT_HR_ACTION_RE.search(raw) or _EXPLICIT_HR_ACTION_BN_RE.search(raw):
            return False
        return True
    return False


def _last_assistant_text(context_lines: list[str]) -> str:
    for line in reversed(context_lines or []):
        if line.startswith("Assistant:"):
            return line[len("Assistant:") :].strip()
    return ""


def assistant_expects_slot_answer(context_lines: list[str], message: str) -> bool:
    """
    True when the previous assistant turn was clearly collecting a wizard field
    (dates, duration, etc.) so a short numeric reply is valid.
    """
    last = _last_assistant_text(context_lines)
    if not last:
        return False
    msg = (message or "").strip()
    if any(marker in last for marker in _LEAVE_WIZARD_ASSISTANT_MARKERS):
        if len(msg) <= 180 or re.search(r"\d", msg):
            return True
    return False


def should_ask_context_clarification(
    message: str,
    context_lines: list[str] | None,
    *,
    intent: str,
    balance_probe: bool,
    leave_active: bool,
    expense_active: bool,
    workflow_continuation: bool,
) -> bool:
    """
    Ask the user to elaborate instead of mis-routing underspecified fragments.
    """
    if balance_probe:
        return False
    if not looks_underspecified_message(message):
        return False
    if (leave_active or expense_active) and workflow_continuation:
        return False
    if assistant_expects_slot_answer(context_lines or [], message):
        return False
    if intent in (INTENT_LEAVE_BALANCE, INTENT_LEAVE_REQUEST, INTENT_UNKNOWN):
        return True
    return False


def _extract_duration_hint(message: str, *, lang: str) -> str | None:
    m = _DURATION_ONLY_RE.match((message or "").strip())
    if m:
        n = m.group("n")
        return f"{n} দিন" if lang == "bn" else f"{n} day(s)"
    dm = re.search(r"(\d{1,3})\s*(?:days?|din|দিন)", message or "", re.I)
    if dm:
        n = dm.group(1)
        return f"{n} দিন" if lang == "bn" else f"{n} day(s)"
    return None


def _reply_language(message: str, context_lines: list[str] | None) -> str:
    if context_lines:
        recent = "\n".join(context_lines[-6:])
        lang = detect_reply_language(f"{recent}\n{message}")
        if lang in ("bn", "en"):
            return lang
    return detect_user_language(message)


def build_context_clarification_message(
    message: str,
    context_lines: list[str] | None,
    *,
    lang: str | None = None,
) -> str:
    """Professional, human clarification — not robotic 'I don't understand'."""
    user_lang = lang or _reply_language(message, context_lines)
    hint = _extract_duration_hint(message, lang=user_lang)

    if user_lang == "bn":
        if hint:
            lead = (
                f"আপনি **{hint}** লিখেছেন — ধন্যবাদ। তবে আমি পুরো প্রসঙ্গটা "
                "একদম নিশ্চিত নই। একটু বিস্তারিত বললে ঠিকভাবে সাহায্য করতে পারব।"
            )
        else:
            lead = (
                "আপনার বার্তাটা একটু সংক্ষিপ্ত — পুরো প্রসঙ্গটা আমি নিশ্চিত "
                "ভাবে বুঝতে পারিনি। একটু বিস্তারিত বললে ভালো হবে।"
            )
        options = (
            "উদাহরণ:\n"
            "• ঈদ/ছুটিতে **কত দিন leave নিতে** চান?\n"
            "• নাকি **বর্তমান leave balance** জানতে চান?\n"
            "• নাকি **কোম্পানির ছুটির নীতি** (ঈদ ছুটি কত দিন) সম্পর্কে?"
        )
        return f"{lead}\n\n{options}"

    if hint:
        lead = (
            f"You mentioned **{hint}** — thanks. I'm not fully sure what you mean "
            "in context yet. A bit more detail will help me assist you properly."
        )
    else:
        lead = (
            "Your message is quite short — I'm not fully sure of the context yet. "
            "Could you add a little more detail?"
        )
    options = (
        "For example:\n"
        "• You want to **apply for leave** (how many days, and when)?\n"
        "• You want your **current leave balance**?\n"
        "• You're asking about **company holiday / Eid leave policy**?"
    )
    return f"{lead}\n\n{options}"
