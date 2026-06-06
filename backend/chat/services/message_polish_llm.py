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
    "expense_wizard_prompt",
]

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

_EXPENSE_SUMMARY_SYSTEM = """You rephrase an expense review summary before user confirmation.

RULES
- Same language as REPLY_LANGUAGE (Bangla script for bn/banglish, English for en).
- FACTS JSON is authoritative — every line, amount, category, route, date, and total must appear.
- Keep bullet list format (- ...) for line items; you may warm up the header sentence only.
- Total amount must match FACTS total exactly.
- Do NOT add yes/no confirmation prompts — those are appended separately.
- Do NOT add or remove expense lines.
- Output ONLY the summary body (header + bullets + total)."""

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
    if message_type == "expense_wizard_prompt":
        return _EXPENSE_WIZARD_PROMPT_SYSTEM
    if message_type == "expense_wizard":
        return _EXPENSE_WIZARD_SYSTEM
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
    ask_template = str(
        (ask_env or {}).get("polishable_part") or envelope.get("fixed_part") or ""
    ).strip()
    ask = ask_template
    if isinstance(ask_env, dict) and ask_template:
        ask = polish_expense_wizard_prompt(
            ask_template,
            envelope=ask_env,
            user_message=user_message,
            trace_id=trace_id,
            llm=llm,
        )

    if body and ask:
        return body.rstrip() + "\n\n" + ask
    if body:
        return body
    if ask:
        return ask
    return None


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
