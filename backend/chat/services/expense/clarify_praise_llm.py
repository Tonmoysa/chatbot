"""LLM-first praise / meta-reply detection during expense clarify."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from chat.services.expense_copy import ReplyLang, normalize_reply_lang
from chat.services.llm_client import LLMClient

logger = logging.getLogger("hr_chatbot")

_CLARIFY_PRAISE_LLM_SYSTEM = """You analyze a user message sent during an active expense wizard (clarify, review, or submit step).

Return ONLY JSON — no prose:

{
  "is_praise_or_meta": true,
  "ack_text": "1-2 warm sentences"
}

Set is_praise_or_meta = true when the user:
- Thanks or praises the bot (awesome, very good observation, valo detect korcho, sundor, etc.)
- Admits a spelling mistake without naming the corrected place again (banan vul diyechilam, typo chilo)
- Sends warm conversational feedback while viewing the expense review — NOT correcting a line
- Combines praise with submit intent (e.g. "awesome ekhon submit koro") — still praise; submit is handled separately

Set is_praise_or_meta = false when the user:
- Names a place, category, or numbered answer (1 bus, motejheel, lunch, 2 metro rail)
- Only a bare yes/no with no praise (ha, yes, thik) — those are answers, not praise
- Only a submit/done command with zero praise words (submit koro, joma daw)

When is_praise_or_meta is true:
- ack_text = 1–2 warm sentences in REPLY_LANGUAGE, matching the user's Bangla/Banglish/English style
- Respond naturally to what they said
- When WIZARD_STAGE is review: thank them and ask if they want to submit now — do NOT repeat the expense list
- When WIZARD_STAGE is submit_confirm: brief thanks before final CRM submit
- If SUBMIT_COMMAND is true, gently remind them to skim once more before final yes — warm, not alarming
- Do NOT list expense lines, amounts, or categories
- Do NOT ask unrelated questions

When is_praise_or_meta is false:
- ack_text must be empty string ""
"""


@dataclass
class ClarifyPraiseLlmResult:
    is_praise_or_meta: bool = False
    ack_text: str = ""


def clarify_praise_llm_enabled(*, use_llm: bool = True) -> bool:
    if not use_llm:
        return False
    return LLMClient().is_configured()


def _lang_line(lang: ReplyLang) -> str:
    if lang == "en":
        return "REPLY_LANGUAGE: English."
    if lang == "banglish":
        return "REPLY_LANGUAGE: Banglish (Roman Bengali) — match the user's style."
    return "REPLY_LANGUAGE: Bangla (Bengali script)."


def llm_json_to_clarify_praise(raw: dict[str, Any] | None) -> ClarifyPraiseLlmResult | None:
    if not isinstance(raw, dict):
        return None
    is_praise = bool(raw.get("is_praise_or_meta"))
    ack = str(raw.get("ack_text") or "").strip()
    if is_praise and not ack:
        return None
    if not is_praise:
        return ClarifyPraiseLlmResult(is_praise_or_meta=False, ack_text="")
    return ClarifyPraiseLlmResult(is_praise_or_meta=True, ack_text=ack)


def parse_clarify_praise_llm(
    message: str,
    *,
    lang: str | None = None,
    trace_id: str = "",
    last_question: str = "",
    use_llm: bool = True,
    wizard_stage: str = "",
    submit_command: bool = False,
) -> ClarifyPraiseLlmResult | None:
    """Classify praise/meta reply and draft warm ack text (LLM-first)."""
    text = (message or "").strip()
    if not text or not clarify_praise_llm_enabled(use_llm=use_llm):
        return None

    reply_lang = normalize_reply_lang(lang)
    client = LLMClient()
    stage = (wizard_stage or "").strip() or "unknown"
    user_prompt = (
        f"{_lang_line(reply_lang)}\n\n"
        f"WIZARD_STAGE: {stage}\n"
        f"SUBMIT_COMMAND: {'yes' if submit_command else 'no'}\n\n"
        f"LAST BOT PROMPT (context):\n{(last_question or '(n/a)')[:1200]}\n\n"
        f"USER MESSAGE:\n{text}\n\n"
        "Return JSON only."
    )
    try:
        out = client.chat_json(
            system_prompt=_CLARIFY_PRAISE_LLM_SYSTEM,
            user_prompt=user_prompt,
            trace_id=trace_id or "expense-clarify-praise",
        )
    except Exception:
        logger.info("expense_clarify_praise_llm_failed trace_id=%s", trace_id or "n/a")
        return None

    result = llm_json_to_clarify_praise(out)
    if result is None and out is not None:
        logger.info(
            "expense_clarify_praise_llm_rejected trace_id=%s",
            trace_id or "n/a",
        )
    return result
