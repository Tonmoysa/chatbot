"""
LLM tone polish for template-based assistant messages (Phase B).

Facts (amounts, dates, categories, references) must survive unchanged.
On failure or guardrail miss, the original template is returned.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from chat.services.llm_client import LLMClient
from chat.services.translator import detect_user_language

MessagePolishType = Literal[
    "out_of_scope",
    "expense_wizard",
    "expense_ack",
    "expense_summary",
    "expense_day_recap",
    "expense_wizard_prompt",
    "expense_validation_block",
    "expense_clarify",
    "expense_clarify_praise_review",
    "expense_review_praise",
    "expense_submit_confirm",
    "expense_submit_success",
    "leave_wizard",
    "leave_review",
    "leave_submitted",
]

_LEAVE_WIZ_MARKER = "_(ছুটি আবেদন — নিচে উত্তর দিন)_"

_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_REF_RE = re.compile(r"\bEXP-[A-Za-z0-9-]+\b", re.I)
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_AMOUNT_IN_FACT_RE = re.compile(r"\d+(?:\.\d+)?")

_OUT_OF_SCOPE_SYSTEM = """You rephrase an HR assistant's polite decline message.

RULES
- Keep the EXACT same boundaries: cannot answer the user's off-topic question; only company HR (leave, expense, attendance, uploaded policies).
- Same language as REPLY_LANGUAGE (Bangla script for bn, English for en).
- 2 short paragraphs max, warm colleague tone — not robotic, not preachy.
- Preserve every **bold** token, date, number, and policy example verbatim.
- Do NOT add new facts, bullet lists, or mention being an AI.
- Output ONLY the rephrased message text."""

_EXPENSE_WIZARD_SYSTEM = """You rephrase an expense collection wizard message.

RULES
- Same language as REPLY_LANGUAGE (Bangla script for bn/banglish user, English for en).
- Keep ALL numbers, dates, category names, routes, and **bold** markdown EXACTLY as in REFERENCE.
- Keep bullet lines (- ...) if present; you may merge intro into one friendly sentence but bullets stay.
- Do NOT add or remove expense lines, amounts, or categories.
- Do NOT mention CRM submission unless REFERENCE does.
- 1 short warm intro + bullets + follow-up question — max ~120 words.
- Output ONLY the rephrased message text."""

_EXPENSE_ACK_SYSTEM = """You rephrase an expense acknowledgment (lines just collected).

RULES
- Same language as REPLY_LANGUAGE (Bangla script for bn/banglish, English for en).
- FACTS JSON lists every line, amount, category, route, and date — ALL must appear in output.
- You may use 1 warm intro sentence plus bullet list, OR 1–2 flowing sentences — user choice.
- Every amount and category from FACTS must appear exactly (same numbers, same category names).
- Preserve **bold** on amounts, categories, and dates when using markdown.
- Do NOT invent lines, do NOT mention CRM submit, do NOT add follow-up questions.
- Output ONLY the acknowledgment portion (no "any more lines?" question)."""

_EXPENSE_CLARIFY_PRAISE_ACK_SYSTEM = """You write a short warm acknowledgment when the user praised the expense bot during expense clarify or review.

RULES
- Same language as REPLY_LANGUAGE — match the user's Bangla / Banglish / English style.
- Respond naturally to what they said (thanks, awesome, very good observation, typo catch praise).
- Reference the review context when appropriate — they are looking at their expense list.
- 1–2 sentences max, warm HR colleague tone — not robotic, not overly long.
- Do NOT list expense lines, amounts, or categories (the review summary follows separately).
- Do NOT ask unrelated questions; a gentle "skim the list below" nudge is OK on review.
- Output ONLY the acknowledgment text."""

_EXPENSE_REVIEW_PRAISE_SYSTEM = """You write a short professional reply when the user praised the bot during expense REVIEW.

RULES
- Same language as REPLY_LANGUAGE — match the user's Bangla / Banglish / English style.
- Warmly acknowledge what they said (thanks, okay thank you, good job, etc.).
- Gently ask whether to submit the expense to CRM now — one clear professional question.
- FACTS JSON has item_count/total for context only — do NOT list line items or bullet amounts.
- 2–3 sentences total max; colleague tone, not robotic.
- Do NOT repeat the full expense review list.
- Output ONLY the reply text."""

_EXPENSE_SUMMARY_SYSTEM = """You rephrase an expense review summary before user confirmation.

RULES
- Same language as REPLY_LANGUAGE (Bangla script for bn/banglish, English for en).
- FACTS JSON is authoritative — every line, amount, category, route, date, and total must appear.
- Keep bullet list format (- ...) for line items; you may warm up the header sentence only.
- Total amount must match FACTS total exactly.
- Do NOT add yes/no confirmation prompts — those are appended separately.
- Do NOT add or remove expense lines.
- Output ONLY the summary body (header + bullets + total)."""

_EXPENSE_DAY_RECAP_SYSTEM = """You rephrase a read-only expense day / session recap for the user.

RULES
- Same language as REPLY_LANGUAGE (Bangla script for bn/banglish, English for en).
- FACTS JSON is authoritative — date, every line amount/category/route, totals, cap, pending vs submitted must appear.
- When empty_session is true, say clearly nothing was found in this chat session and how to start a new claim (keep the example format).
- Warm, professional HR colleague tone — not robotic.
- Preserve **bold** markdown on amounts, dates, references, and section headers when using markdown.
- Do NOT invent expense lines, amounts, or reference IDs.
- Do NOT ask wizard collection questions unless REFERENCE does for empty state guidance only.
- Output ONLY the recap message text."""

_LEAVE_WIZARD_SYSTEM = """You rephrase a leave application wizard message (collecting dates, payment, scope, document).

RULES
- Same language as REPLY_LANGUAGE (Bangla script for bn/banglish user, English for en).
- Keep ALL dates (YYYY-MM-DD), day counts, **bold** tokens, paid/unpaid, Full/Half day EXACTLY as in REFERENCE.
- Keep bullet lines (- ...) and option examples; you may warm up intro/acknowledgment sentences only.
- Same intent: note what was captured, then ask the pending question.
- Warm, professional HR colleague tone — clear and respectful, not robotic.
- Max ~150 words.
- Do NOT invent dates, reasons, leave types, or new options.
- Output ONLY the rephrased message text."""

_LEAVE_REVIEW_SYSTEM = """You rephrase a leave application review before submit.

RULES
- Same language as REPLY_LANGUAGE (Bangla script for bn/banglish, English for en).
- Every date, date range (→), reason text, paid/unpaid, scope, attachment line must appear exactly.
- Keep **yes** / **edit** / **cancel** options and their Bengali hints if present.
- 1 short professional intro + summary bullets + confirm question.
- Do NOT change factual field values or add new fields.
- Output ONLY the rephrased message text."""

_EXPENSE_VALIDATION_BLOCK_SYSTEM = """You rephrase an expense wizard validation/block message.

RULES
- Same language as REPLY_LANGUAGE (Bangla script for bn/banglish, English for en).
- FACTS JSON is authoritative — block reason, category, amounts, pending lines must appear.
- Warm, clear colleague tone — explain what is missing and what to do next.
- Preserve **bold** on amounts, categories, and line labels.
- Do NOT invent amounts, categories, or routes.
- Output ONLY the block message text."""

_EXPENSE_CLARIFY_SYSTEM = """You rephrase a batched expense clarification prompt before review.

RULES
- Same language as REPLY_LANGUAGE (Bangla script for bn/banglish, English for en).
- FACTS.issues is the ONLY list of open questions — rephrase those items only.
- IGNORE expense lines from the user's original voice dump that are NOT listed in FACTS.issues.
- Already-detected lines (with category in the message) must NOT appear as new questions.
- FACTS JSON lists every open issue — amount, typo original/suggestion must appear.
- NEVER switch REPLY_LANGUAGE to English when it is bn or banglish.
- If REPLY_LANGUAGE is bn or banglish, output MUST include Bengali script OR natural Banglish — not English-only.
- prompt_variant guides tone:
  - initial: friendly intro that review is almost ready, list what needs confirming
  - followup: thank user for partial answers; only ask what is still open
  - disambiguation: user gave one answer for multiple items — ask which numbered item they mean (warm, not scolding)
  - done_incomplete: user said they are done but items remain — warm wrap-up then list open issues only
- Numbered list format is fine; keep every issue visible; same count as FACTS.issues.
- Preserve **bold** on amounts, categories, locations, and suggestions.
- Keep the one-message reply instruction at the end (vary wording slightly each time).
- Warm HR colleague tone — natural Bangla/Banglish mix when REPLY_LANGUAGE is banglish.
- Do NOT add or remove clarification items.
- Output ONLY the clarification prompt."""

_EXPENSE_SUBMIT_CONFIRM_SYSTEM = """You rephrase the intro line before expense CRM submit confirmation.

RULES
- Same language as REPLY_LANGUAGE (Bangla script for bn/banglish, English for en).
- Same meaning: data looks good, ready to submit to CRM.
- If the user combined praise with submit intent, thank them briefly and gently suggest one quick skim of the lines before final yes.
- 1–2 warm professional sentences only — the yes/no options are appended separately.
- Do NOT include yes/no bullets or CRM submit options.
- Output ONLY the intro sentence(s)."""

_EXPENSE_SUBMIT_SUCCESS_SYSTEM = """You rephrase an expense submission success card.

RULES
- Same language as REPLY_LANGUAGE (Bangla script for bn/banglish, English for en).
- FACTS JSON is authoritative — date, line count, total, reference ID must appear exactly.
- Professional HR colleague tone — concise success card with bullet list.
- Preserve **bold** markdown and reference ID format.
- Keep the CRM/Finance disclaimer meaning (final approval happens in company system).
- Do NOT invent reference IDs or change amounts.
- Output ONLY the success card text."""

_LEAVE_SUBMITTED_SYSTEM = """You rephrase a leave request submission success card.

RULES
- Same language as REPLY_LANGUAGE (Bangla script for bn/banglish, English for en).
- Every date, date range (→), leave type, paid/unpaid, scope, days requested, balance, reference must appear exactly.
- Professional HR colleague tone — organized bullet card.
- Preserve **bold** markdown and reference ID.
- Keep CRM/HR disclaimer meaning.
- Do NOT invent dates or change field values.
- Output ONLY the success card text."""

_EXPENSE_WIZARD_PROMPT_SYSTEM = """You rephrase ONE expense wizard follow-up question.

RULES
- Same language as REPLY_LANGUAGE (Bangla script for bn/banglish, English for en).
- Preserve ALL amounts, categories, route hints, and **bold** markdown exactly.
- Keep the same intent: pick category, provide from/to, add more lines, or start collecting.
- If REFERENCE lists category options, keep the list (you may shorten the intro only).
- Warm, concise colleague tone — max ~80 words.
- Do NOT mention CRM submission unless REFERENCE does.
- Do NOT invent amounts or categories.
- Output ONLY the rephrased prompt text."""


def is_llm_message_polish_enabled() -> bool:
    from django.conf import settings

    return bool(getattr(settings, "LLM_MESSAGE_POLISH", True))


def extract_locked_facts(text: str) -> list[str]:
    """Tokens that must appear unchanged (or numerically) in polished output."""
    seen: set[str] = set()
    facts: list[str] = []
    for m in _BOLD_RE.finditer(text or ""):
        token = m.group(1).strip()
        if token and token not in seen:
            seen.add(token)
            facts.append(token)
    for m in _ISO_DATE_RE.finditer(text or ""):
        token = m.group(0)
        if token not in seen:
            seen.add(token)
            facts.append(token)
    for m in _REF_RE.finditer(text or ""):
        token = m.group(0)
        if token not in seen:
            seen.add(token)
            facts.append(token)
    return facts


def _fact_preserved(fact: str, polished: str) -> bool:
    if not fact:
        return True
    if fact in polished:
        return True
    if _AMOUNT_IN_FACT_RE.search(fact):
        for num in _AMOUNT_IN_FACT_RE.findall(fact):
            if num not in polished:
                return False
        return True
    return fact.lower() in polished.lower()


def facts_preserved(original: str, polished: str) -> bool:
    facts = extract_locked_facts(original)
    if not facts:
        return bool((polished or "").strip())
    return all(_fact_preserved(f, polished) for f in facts)


def leave_facts_preserved(original: str, polished: str) -> bool:
    """
    Semantic guardrail for leave wizard/review polish.

    Allows natural rephrasing (no exact **bold** match) but keeps dates, duration,
    reason, payment/scope, skip hints, and document intent.
    """
    orig = (original or "").strip()
    pol = (polished or "").strip()
    if not pol:
        return False

    for iso in _ISO_DATE_RE.findall(orig):
        if iso not in pol:
            return False

    if "→" in orig and "→" not in pol:
        return False

    for m in re.finditer(r"(\d+)\s*দিন", orig):
        n = m.group(1)
        if n not in pol and m.group(0) not in pol:
            return False

    reason_m = re.search(r"কারণ:\s*([^—\n]+)", orig)
    if reason_m:
        reason = re.sub(r"\*+", "", reason_m.group(1)).strip()
        if len(reason) >= 3 and reason.lower() not in pol.lower():
            return False

    low_o = orig.lower()
    low_p = pol.lower()

    if re.search(r"\bpaid\b", low_o) and not re.search(r"\bpaid\b", low_p):
        return False
    if "unpaid" in low_o and "unpaid" not in low_p:
        return False
    if ("full day" in low_o or "পুরো দিন" in orig) and not (
        "full day" in low_p or "পুরো দিন" in pol
    ):
        return False
    if ("half day" in low_o or "হাফ দিন" in orig) and not (
        "half day" in low_p or "হাফ দিন" in pol
    ):
        return False
    if ("skip" in low_o or "parbo na" in low_o) and not (
        "skip" in low_p or "parbo na" in low_p
    ):
        return False

    if re.search(r"ডাক্তার|চিট|doctor|medical\s+note", orig, re.I):
        if not re.search(
            r"ডাক্তার|চিট|doctor|medical|কাগজ|document|prescription|note",
            pol,
            re.I,
        ):
            return False

    if "জমা দেবেন" in orig or "**yes**" in orig:
        if "yes" not in low_p and "জমা" not in pol:
            return False

    if "সংযুক্তি" in orig and "সংযুক্তি" not in pol:
        return False

    return True


def _lang_line(user_lang: str) -> str:
    if user_lang == "en":
        return "REPLY_LANGUAGE: English."
    if user_lang == "banglish":
        return "REPLY_LANGUAGE: Bangla (Bengali script) — user wrote in Banglish."
    return "REPLY_LANGUAGE: Bangla (Bengali script)."


def _system_prompt(message_type: MessagePolishType) -> str:
    if message_type == "expense_ack":
        return _EXPENSE_ACK_SYSTEM
    if message_type == "expense_summary":
        return _EXPENSE_SUMMARY_SYSTEM
    if message_type == "expense_day_recap":
        return _EXPENSE_DAY_RECAP_SYSTEM
    if message_type == "expense_wizard_prompt":
        return _EXPENSE_WIZARD_PROMPT_SYSTEM
    if message_type == "expense_wizard":
        return _EXPENSE_WIZARD_SYSTEM
    if message_type == "leave_wizard":
        return _LEAVE_WIZARD_SYSTEM
    if message_type == "leave_review":
        return _LEAVE_REVIEW_SYSTEM
    if message_type == "expense_validation_block":
        return _EXPENSE_VALIDATION_BLOCK_SYSTEM
    if message_type == "expense_clarify":
        return _EXPENSE_CLARIFY_SYSTEM
    if message_type == "expense_clarify_praise_review":
        return _EXPENSE_CLARIFY_PRAISE_ACK_SYSTEM
    if message_type == "expense_review_praise":
        return _EXPENSE_REVIEW_PRAISE_SYSTEM
    if message_type == "expense_submit_confirm":
        return _EXPENSE_SUBMIT_CONFIRM_SYSTEM
    if message_type == "expense_submit_success":
        return _EXPENSE_SUBMIT_SUCCESS_SYSTEM
    if message_type == "leave_submitted":
        return _LEAVE_SUBMITTED_SYSTEM
    return _OUT_OF_SCOPE_SYSTEM


def polish_expense_facts_message(
    base: str,
    *,
    envelope: dict[str, Any],
    user_message: str,
    trace_id: str | None = None,
    min_length: int = 20,
    llm: LLMClient | None = None,
) -> str:
    """Polish ack/summary using structured facts JSON; falls back to template on failure."""
    from chat.services.expense_message_facts import envelope_facts_preserved

    template = (base or "").strip()
    if not template or not is_llm_message_polish_enabled() or not trace_id:
        return template

    message_type = str(envelope.get("message_type") or "expense_ack")
    if message_type not in ("expense_ack", "expense_summary"):
        return template

    client = llm or LLMClient()
    if not client.is_configured():
        return template

    lang = str(envelope.get("lang") or detect_user_language(user_message or template))
    facts_json = json.dumps(envelope.get("facts") or {}, ensure_ascii=False, indent=2)
    try:
        out = client.chat_text(
            system_prompt=_system_prompt(message_type),  # type: ignore[arg-type]
            user_prompt=(
                f"{_lang_line(lang)}\n\n"
                f"User said:\n{user_message or '(n/a)'}\n\n"
                f"FACTS (every amount, category, date, route must appear in output):\n"
                f"{facts_json}\n\n"
                f"REFERENCE display (same meaning, preserve all numbers and categories):\n"
                f"{template}"
            ),
            trace_id=trace_id,
        )
    except Exception:
        return template

    cleaned = (out or "").strip()
    if len(cleaned) < min_length:
        return template
    check_envelope = {**envelope, "template_fallback": template}
    if not envelope_facts_preserved(check_envelope, cleaned):
        return template
    return cleaned


def polish_expense_wizard_prompt(
    base: str,
    *,
    envelope: dict[str, Any],
    user_message: str,
    trace_id: str | None = None,
    min_length: int = 15,
    llm: LLMClient | None = None,
) -> str:
    """Polish a wizard follow-up prompt (category / route / more lines / collect)."""
    from chat.services.expense_message_facts import prompt_envelope_facts_preserved

    template = (base or "").strip()
    if not template or envelope.get("skip_polish") or not is_llm_message_polish_enabled():
        return template
    if not trace_id:
        return template

    client = llm or LLMClient()
    if not client.is_configured():
        return template

    lang = str(envelope.get("lang") or detect_user_language(user_message or template))
    facts_json = json.dumps(envelope.get("facts") or {}, ensure_ascii=False, indent=2)
    try:
        out = client.chat_text(
            system_prompt=_EXPENSE_WIZARD_PROMPT_SYSTEM,
            user_prompt=(
                f"{_lang_line(lang)}\n\n"
                f"User said:\n{user_message or '(n/a)'}\n\n"
                f"PROMPT FACTS (preserve amounts, categories, options):\n"
                f"{facts_json}\n\n"
                f"REFERENCE prompt (same meaning, preserve **bold** and numbers):\n"
                f"{template}"
            ),
            trace_id=trace_id,
        )
    except Exception:
        return template

    cleaned = (out or "").strip()
    if len(cleaned) < min_length:
        return template
    check = {**envelope, "template_fallback": template}
    if not prompt_envelope_facts_preserved(check, cleaned):
        return template
    return cleaned


def _envelope_facts_guard_ok(envelope: dict[str, Any], polished: str) -> bool:
    from chat.services.expense_message_facts import (
        clarify_facts_preserved,
        clarify_praise_facts_preserved,
        envelope_facts_preserved,
        submit_success_facts_preserved,
        validation_block_facts_preserved,
    )

    message_type = str(envelope.get("message_type") or "")
    if message_type in ("expense_ack", "expense_summary"):
        return envelope_facts_preserved(envelope, polished)
    if message_type == "expense_validation_block":
        return validation_block_facts_preserved(envelope, polished)
    if message_type == "expense_clarify":
        return clarify_facts_preserved(envelope, polished)
    if message_type in ("expense_clarify_praise_review", "expense_review_praise"):
        return clarify_praise_facts_preserved(envelope, polished)
    if message_type == "expense_submit_success":
        return submit_success_facts_preserved(envelope, polished)
    template = str(envelope.get("template_fallback") or "")
    if template:
        return facts_preserved(template, polished)
    return bool((polished or "").strip())


def polish_envelope_message(
    base: str,
    *,
    envelope: dict[str, Any],
    user_message: str,
    trace_id: str | None = None,
    min_length: int = 20,
    llm: LLMClient | None = None,
) -> str:
    """Polish any facts-backed envelope; falls back to template on failure."""
    template = (base or "").strip()
    if not template or not is_llm_message_polish_enabled() or not trace_id:
        return template

    message_type = str(envelope.get("message_type") or "expense_wizard")
    if message_type not in (
        "expense_validation_block",
        "expense_clarify",
        "expense_clarify_praise_review",
    "expense_review_praise",
        "expense_submit_confirm",
        "expense_submit_success",
    ):
        return template

    client = llm or LLMClient()
    if not client.is_configured():
        return template

    lang = str(envelope.get("lang") or detect_user_language(user_message or template))
    facts_json = json.dumps(envelope.get("facts") or {}, ensure_ascii=False, indent=2)
    facts_label = (
        "FACTS (context only — do NOT list expense lines in output):\n"
        if message_type in ("expense_clarify_praise_review", "expense_review_praise")
        else "FACTS (preserve every amount, category, date, reference):\n"
    )
    if message_type == "expense_clarify":
        user_block = (
            "Context: user sent a multi-line expense claim. "
            "Clarify ONLY the open items in FACTS.issues — "
            "do not ask about lines already parsed with a category.\n"
        )
    else:
        user_block = f"User said:\n{user_message or '(n/a)'}\n"
    try:
        out = client.chat_text(
            system_prompt=_system_prompt(message_type),  # type: ignore[arg-type]
            user_prompt=(
                f"{_lang_line(lang)}\n\n"
                f"{user_block}\n"
                f"{facts_label}"
                f"{facts_json}\n\n"
                f"REFERENCE display (same meaning, preserve all **bold** and numbers):\n"
                f"{template}"
            ),
            trace_id=trace_id,
        )
    except Exception:
        return template

    cleaned = (out or "").strip()
    if len(cleaned) < min_length:
        return template
    check_envelope = {**envelope, "template_fallback": template}
    if not _envelope_facts_guard_ok(check_envelope, cleaned):
        return template
    return cleaned


def _polish_body_from_envelope(
    envelope: dict[str, Any],
    *,
    user_message: str,
    trace_id: str | None,
    llm: LLMClient | None,
) -> str:
    message_type = str(envelope.get("message_type") or "")
    polishable = str(envelope.get("polishable_part") or "").strip()
    if not polishable:
        return ""
    if message_type in ("expense_ack", "expense_summary"):
        return polish_expense_facts_message(
            polishable,
            envelope=envelope,
            user_message=user_message,
            trace_id=trace_id,
            llm=llm,
        )
    if message_type == "expense_wizard_prompt":
        return polish_expense_wizard_prompt(
            polishable,
            envelope=envelope,
            user_message=user_message,
            trace_id=trace_id,
            llm=llm,
        )
    if message_type in (
        "expense_validation_block",
        "expense_clarify",
        "expense_clarify_praise_review",
    "expense_review_praise",
        "expense_submit_confirm",
        "expense_submit_success",
    ):
        return polish_envelope_message(
            polishable,
            envelope=envelope,
            user_message=user_message,
            trace_id=trace_id,
            llm=llm,
        )
    return polishable


def polish_expense_message_with_envelope(
    envelope: dict[str, Any],
    *,
    user_message: str,
    trace_id: str | None = None,
    llm: LLMClient | None = None,
) -> str | None:
    """Polish ack/summary body and wizard ask prompt; reassemble in order."""
    body = _polish_body_from_envelope(
        envelope,
        user_message=user_message,
        trace_id=trace_id,
        llm=llm,
    )
    ask_env = envelope.get("ask_envelope")
    ask_template = ""
    if isinstance(ask_env, dict):
        ask_template = str(ask_env.get("polishable_part") or "").strip()
    ask = ask_template
    if isinstance(ask_env, dict) and ask_template:
        ask = polish_expense_wizard_prompt(
            ask_template,
            envelope=ask_env,
            user_message=user_message,
            trace_id=trace_id,
            llm=llm,
        )

    fixed = str(envelope.get("fixed_part") or "").strip()
    msg_type = str(envelope.get("message_type") or "")
    if body and fixed and msg_type in (
        "expense_submit_confirm",
        "expense_clarify_praise_review",
    "expense_review_praise",
    ):
        return body.rstrip() + "\n\n" + fixed
    if body and ask:
        return body.rstrip() + "\n\n" + ask
    if body:
        return body
    if ask:
        return ask
    if fixed:
        return fixed
    return None


def polish_leave_wizard_message(
    base: str,
    *,
    user_message: str,
    trace_id: str | None = None,
    review: bool = False,
    user_lang: str | None = None,
    min_length: int = 20,
    llm: LLMClient | None = None,
) -> str:
    """
    Rephrase leave wizard/review templates — same facts, warmer professional tone.
    Preserves the collecting-phase footer marker when present.
    """
    template = (base or "").strip()
    if not template:
        return template

    if not is_llm_message_polish_enabled() or not trace_id:
        return template

    has_marker = _LEAVE_WIZ_MARKER in template
    body = template.replace(_LEAVE_WIZ_MARKER, "").strip() if has_marker else template
    message_type: MessagePolishType = "leave_review" if review else "leave_wizard"

    client = llm or LLMClient()
    if not client.is_configured():
        return template

    lang = user_lang or detect_user_language(user_message or body)
    try:
        out = client.chat_text(
            system_prompt=_system_prompt(message_type),
            user_prompt=(
                f"{_lang_line(lang)}\n\n"
                f"User said:\n{user_message or '(n/a)'}\n\n"
                f"REFERENCE (same meaning — preserve dates, day counts, reason, options):\n{body}"
            ),
            trace_id=trace_id,
        )
    except Exception:
        return template

    polished_body = (out or "").strip()
    if len(polished_body) < min_length:
        return template
    if not leave_facts_preserved(body, polished_body):
        return template

    if has_marker:
        return polished_body.rstrip() + _LEAVE_WIZ_MARKER
    return polished_body


def polish_expense_day_recap_message(
    base: str,
    *,
    envelope: dict[str, Any],
    user_message: str,
    trace_id: str | None = None,
    min_length: int = 25,
    llm: LLMClient | None = None,
) -> str:
    """Polish session/CRM expense day recap — same facts, natural professional tone."""
    template = (base or "").strip()
    if not template or not is_llm_message_polish_enabled() or not trace_id:
        return template

    client = llm or LLMClient()
    if not client.is_configured():
        return template

    lang = str(envelope.get("lang") or detect_user_language(user_message or template))
    facts_json = json.dumps(envelope.get("facts") or {}, ensure_ascii=False, indent=2)
    try:
        out = client.chat_text(
            system_prompt=_EXPENSE_DAY_RECAP_SYSTEM,
            user_prompt=(
                f"{_lang_line(lang)}\n\n"
                f"User asked:\n{user_message or '(n/a)'}\n\n"
                f"FACTS (every amount, category, date, total, cap must appear in output):\n"
                f"{facts_json}\n\n"
                f"REFERENCE display (same meaning, preserve all **bold** and numbers):\n"
                f"{template}"
            ),
            trace_id=trace_id,
        )
    except Exception:
        return template

    cleaned = (out or "").strip()
    if len(cleaned) < min_length:
        return template
    check_envelope = {**envelope, "template_fallback": template}
    from chat.services.expense_message_facts import session_recap_facts_preserved

    if not session_recap_facts_preserved(check_envelope, cleaned):
        if not facts_preserved(template, cleaned):
            return template
    return cleaned


def polish_template_message(
    base: str,
    *,
    user_message: str,
    message_type: MessagePolishType,
    trace_id: str | None = None,
    user_lang: str | None = None,
    min_length: int = 40,
    llm: LLMClient | None = None,
) -> str:
    """
    Rephrase a template message for natural tone while preserving locked facts.
    Returns ``base`` when polish is disabled, LLM unavailable, or guardrails fail.
    """
    template = (base or "").strip()
    if not template or not is_llm_message_polish_enabled() or not trace_id:
        return template

    client = llm or LLMClient()
    if not client.is_configured():
        return template

    lang = user_lang or detect_user_language(user_message or template)
    try:
        out = client.chat_text(
            system_prompt=_system_prompt(message_type),
            user_prompt=(
                f"{_lang_line(lang)}\n\n"
                f"User said:\n{user_message or '(n/a)'}\n\n"
                f"REFERENCE (same meaning, preserve all **bold** and numbers):\n{template}"
            ),
            trace_id=trace_id,
        )
    except Exception:
        return template

    cleaned = (out or "").strip()
    if len(cleaned) < min_length:
        return template
    if not facts_preserved(template, cleaned):
        return template
    return cleaned
