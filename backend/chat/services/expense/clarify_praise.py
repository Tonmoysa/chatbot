"""Warm acknowledgment when user praises the bot during expense clarify."""

from __future__ import annotations

import re
from dataclasses import dataclass

from chat.services.expense.clarify_affirmatives import looks_like_typo_acknowledgment
from chat.services.expense_copy import ReplyLang, normalize_reply_lang, pick_rotating_phrase

_CLARIFY_PRAISE_RE = re.compile(
    r"(?:"
    r"awesome|thanks|thank\s*you|dhonnobad|"
    r"valo\s*vabei|khub\s*bhalo|good\s*job|nice\s*work|"
    r"analysis\s*kor|detect\s*kor|perfectly|"
    r"very\s*good|good\s*observation|observation|"
    r"sundor|shundor|khub\s*sundor|well\s*done|appreciate"
    r")",
    re.I,
)

_PRAISE_ACK_TEMPLATES: dict[ReplyLang, tuple[str, ...]] = {
    "bn": (
        "ধন্যবাদ! আপনার প্রশংসা পেয়ে ভালো লাগলো — খরচগুলো ঠিক করে নিয়েছি, নিচে পর্যালোচনা দেখুন।",
        "আপনি যেভাবে বলেছেন, সেটা মাথায় রেখেছি — সব ঠিক করে নিচের তালিকায় দেখানো হলো।",
        "খুব ভালো লাগলো! টাইপো ধরতে পেরেছি বলে আনন্দিত — নিচে আপনার খরচের সারাংশ।",
    ),
    "banglish": (
        "Thanks! Apnar proshongsha peye bhalo laglo — kharcha gulo thik kore niyechi, niche review dekhen.",
        "Bhalo laglo! Typo detect korte perechi — niche apnar expense list.",
        "Awesome feedback! Sob thik kore niche summary dekhun.",
    ),
    "en": (
        "Thanks — glad the typo catch helped! Your expenses are sorted; review below.",
        "Appreciate that! I've applied the fixes — here's your review summary.",
        "Great to hear — everything's lined up below for your review.",
    ),
}


@dataclass
class ClarifyPraiseContext:
    """Resolved praise ack for review transition."""

    is_praise: bool
    ack_text: str
    source: str  # llm | regex | clarify_llm


def looks_like_clarify_praise_message(message: str) -> bool:
    """Regex fallback when LLM unavailable."""
    return looks_like_wizard_praise_message(message)


def looks_like_wizard_praise_message(message: str) -> bool:
    """Praise / warm feedback during clarify, review, or submit — not a form answer."""
    if looks_like_typo_acknowledgment(message):
        return True
    text = (message or "").strip()
    if not text:
        return False
    if re.match(r"^\d+\s", text):
        return False
    try:
        from chat.services.expense.wizard_commands import wants_expense_submit_command

        if wants_expense_submit_command(text) and not _CLARIFY_PRAISE_RE.search(text):
            return False
    except Exception:
        pass
    return bool(_CLARIFY_PRAISE_RE.search(text))


def review_praise_submit_nudge(lang: str | None) -> str:
    """Short submit ask after warm praise on review — no expense list repeat."""
    reply_lang = normalize_reply_lang(lang)
    if reply_lang == "en":
        return (
            "If everything looks good, shall I submit this expense to CRM? "
            "Reply **yes** or **submit** to proceed."
        )
    if reply_lang == "banglish":
        return (
            "Sob thik thakle CRM-e submit korbo? **yes** ba **submit** likhun."
        )
    return (
        "সব ঠিক থাকলে CRM-এ জমা দেব? **হ্যাঁ** বা **submit** লিখুন।"
    )


def clarify_praise_ack_template(lang: str | None, *, seed: str = "") -> str:
    """Template fallback for warm praise ack before review."""
    reply_lang = normalize_reply_lang(lang)
    pool = _PRAISE_ACK_TEMPLATES.get(reply_lang, _PRAISE_ACK_TEMPLATES["bn"])
    return pick_rotating_phrase(pool, seed=seed or "praise")


def _language_ok_for_ack(lang: str | None, ack: str, *, template: str = "") -> bool:
    from chat.services.expense.clarify_polish import clarify_polish_language_ok

    return clarify_polish_language_ok(normalize_reply_lang(lang), ack, template=template)


def resolve_clarify_praise_for_review(
    message: str,
    *,
    lang: str | None = None,
    trace_id: str = "",
    last_question: str = "",
    clarify_llm_praise: bool = False,
    use_llm: bool = True,
    wizard_stage: str = "",
    submit_command: bool = False,
) -> ClarifyPraiseContext | None:
    """
    LLM-first praise detection + ack draft for review screen.

    Falls back to regex + template when LLM is off or rejects output.
    """
    text = (message or "").strip()
    if not text:
        return None

    from chat.services.expense.clarify_praise_llm import (
        clarify_praise_llm_enabled,
        parse_clarify_praise_llm,
    )

    if clarify_praise_llm_enabled(use_llm=use_llm):
        llm_result = parse_clarify_praise_llm(
            text,
            lang=lang,
            trace_id=trace_id,
            last_question=last_question,
            use_llm=use_llm,
            wizard_stage=wizard_stage,
            submit_command=submit_command,
        )
        if llm_result is not None:
            if llm_result.is_praise_or_meta and llm_result.ack_text:
                if _language_ok_for_ack(lang, llm_result.ack_text):
                    source = "llm"
                    if clarify_llm_praise:
                        source = "clarify_llm+llm"
                    return ClarifyPraiseContext(
                        is_praise=True,
                        ack_text=llm_result.ack_text,
                        source=source,
                    )
            if not llm_result.is_praise_or_meta and not clarify_llm_praise:
                return None

    if clarify_llm_praise or looks_like_clarify_praise_message(text):
        return ClarifyPraiseContext(
            is_praise=True,
            ack_text=clarify_praise_ack_template(lang, seed=text),
            source="clarify_llm" if clarify_llm_praise else "regex",
        )
    return None
