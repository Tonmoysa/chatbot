"""Finish-collecting intent (rules + LLM) and incomplete-draft prompts."""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense.clarify import (
    ClarificationIssue,
    collect_clarification_issues,
    format_clarification_prompt,
)
from chat.services.expense.wizard_commands import wants_expense_done_command_rules

_DONE_PHRASE_RE = re.compile(
    r"(?:"
    r"\ball\s+done\b|\beverything(?:'s|\s+is)?\s+(?:ok(?:ay)?|fine|good|perfect|right|correct)\b|"
    r"\bthat'?s\s+all\b|\bi'?m\s+done\b|\bwe'?re\s+done\b|"
    r"\bnothing\s+(?:else|more)\b|\bno\s+more\b|"
    r"\bsob\s+(?:thik|done|complete|hoyeche|hoise)\b|\bshob\s+(?:thik|done|complete|hoyeche)\b|"
    r"\bbas\s+ei\b|\beverything\s+perfect\b|\blooks?\s+good\b|\ball\s+good\b|"
    r"\bdone\s+now\b|\bwrap\s+up\b|\bfinished?\b"
    r")",
    re.I,
)


def wants_expense_done_phrase(message: str) -> bool:
    """Broader rule patterns before LLM (not anchored to full message)."""
    return bool(_DONE_PHRASE_RE.search((message or "").strip()))


def _looks_like_conversational_wrap_up(message: str) -> bool:
    low = (message or "").strip().lower()
    if not low:
        return False
    return bool(
        re.search(
            r"\b(?:yeah|yes|ok(?:ay)?|fine|good|perfect|great|nice|alright|"
            r"seems?|looks?|everything|all|done|shesh|thik)\b",
            low,
        )
    )


_AMOUNT_INSTEAD_HINT_RE = re.compile(
    r"(?:"
    r"(?:use|make|set)\s+\d+(?:[.,]\d{1,2})?\s+instead\s+of\s+\d+"
    r"|"
    r"\d+(?:[.,]\d{1,2})?\s+instead\s+of\s+\d+"
    r")",
    re.I,
)


_BARE_CONFIRM_RE = re.compile(
    r"^(?:"
    r"yes|yep|yeah|y|ok|okay|confirm|ha|hmm?\s*yes|thik\s*ache|thik|"
    r"হ্যাঁ|হ্যা|ঠিক\s*আছে|ঠিক|no|nope|না"
    r")\s*\.?$",
    re.I,
)


def done_intent_llm_should_use(message: str, *, wizard_stage: str = "") -> bool:
    """Skip LLM when the message is clearly a line item or wizard command."""
    text = (message or "").strip()
    if not text or len(text) > 220:
        return False
    if wizard_stage in ("review", "submit_confirm"):
        return False
    if _BARE_CONFIRM_RE.match(text):
        return False
    if _AMOUNT_INSTEAD_HINT_RE.search(text):
        return False
    try:
        from chat.services.expense.wizard_commands import wants_expense_submit_command

        if wants_expense_submit_command(text):
            return False
    except Exception:
        pass
    try:
        from chat.services.expense.expense_confirm import looks_like_expense_correction
        from chat.services.expense.clarify import looks_like_clarify_reply_signal
        from chat.services.expense_extraction import (
            parse_amount_only,
            parse_category_token,
        )
        from chat.services.intent_detector import _strong_expense_claim

        if looks_like_expense_correction(text):
            return False
        if looks_like_clarify_reply_signal(text) and not _looks_like_conversational_wrap_up(
            text
        ):
            return False
        if _strong_expense_claim(text):
            return False
        if parse_category_token(text) and parse_amount_only(text) is not None:
            return False
    except Exception:
        pass
    return _looks_like_conversational_wrap_up(text)


def detect_finish_collecting_intent(
    message: str,
    *,
    trace_id: str = "",
    use_llm: bool = True,
    wizard_stage: str = "",
) -> bool:
    """True when user wants to finish collecting (rules first, then LLM)."""
    if wants_expense_done_command_rules(message):
        return True
    if wants_expense_done_phrase(message):
        return True
    if use_llm and done_intent_llm_should_use(message, wizard_stage=wizard_stage):
        from chat.services.expense.done_intent_llm import parse_finish_collecting_llm

        return parse_finish_collecting_llm(message, trace_id=trace_id, use_llm=use_llm)
    return False


def collect_incomplete_draft_issues(
    block: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    pending_entries: list[dict[str, Any]] | None = None,
) -> list[ClarificationIssue]:
    from chat.services.expense_workflow import _pending_entries_list

    pending = (
        list(pending_entries)
        if pending_entries is not None
        else _pending_entries_list(block)
    )
    return collect_clarification_issues(items, pending)


def expense_draft_is_incomplete(
    block: dict[str, Any],
    items: list[dict[str, Any]],
) -> bool:
    from chat.services.expense_workflow import _has_pending_expense_line

    if _has_pending_expense_line(block):
        return True
    return bool(collect_incomplete_draft_issues(block, items))


def _warm_done_incomplete_intro(lang: str) -> str:
    if lang == "en":
        return (
            "Good — sounds like you want to wrap up. "
            "A few details are still missing though:"
        )
    if lang == "banglish":
        return (
            "Bhalo — mone hocche apni shesh korte chaichen. "
            "Kintu ekhono kichu info baki ache:"
        )
    return (
        "ভালো — মনে হচ্ছে আপনি শেষ করতে চাইছেন। "
        "তবে এখনো কিছু তথ্য বাকি আছে:"
    )


def format_done_incomplete_prompt(
    issues: list[ClarificationIssue],
    *,
    lang: str | None = None,
) -> str:
    """Warm wrap-up line + numbered missing-detail list."""
    from chat.services.expense_copy import normalize_reply_lang

    reply_lang = normalize_reply_lang(lang)
    if not issues:
        return _warm_done_incomplete_intro(reply_lang)
    body = format_clarification_prompt(issues, lang=reply_lang)
    intro = _warm_done_incomplete_intro(reply_lang)
    # Replace default clarify intro with our warmer done-specific opener.
    lines = body.split("\n")
    if lines:
        lines[0] = intro
    return "\n".join(lines)
