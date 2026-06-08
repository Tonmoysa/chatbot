"""
Parse supporting-document wizard answers — rules + LLM fallback.

Voice/Banglish refusals (\"pore debo\", \"chit nai\") cannot be covered by regex alone.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from chat.services.leave_draft_utils import is_supporting_document_skip_message
from chat.services.leave.turn_schema import CONFIDENCE_LLM_FALLBACK
from chat.services.llm_client import LLMClient

logger = logging.getLogger("hr_chatbot")

ACTION_WAIVE = "waive"
ACTION_ATTACH = "attach"
ACTION_NONE = "none"

_DOCUMENT_PROVIDE_RE = re.compile(
    r"(?:"
    r"prescription|diagnosis|medical\s+certificate|doctor(?:'s)?\s+note|"
    r"ডাক্তার|চিট|প্রেসক্রিপশন|সার্টিফিকেট|"
    r"patient\s*:|rx\b|"
    r"uploaded|attached|এটাচ"
    r")",
    re.I | re.UNICODE,
)

_DOCUMENT_LLM_SYSTEM = """You classify the user's answer about a sick-leave supporting document.

We asked whether they can upload/paste a doctor's note now, or skip for manager review.

Return STRICT JSON only:
{
  "intent": "waive" | "provide" | "unclear",
  "document_text": string or null,
  "confidence": 0.0 to 1.0
}

RULES
- waive: cannot/will not attach now — skip, parbo na, pore debo, chit nai, manager dekhun, nei, etc.
- provide: pasted medical note text OR clearly supplying document content
- unclear: unrelated question or does not answer the document prompt
- NEVER treat a short refusal as document_text
"""


@dataclass
class DocumentTurnResult:
    action: str = ACTION_NONE
    content: str = ""
    source: str = "none"


def _rules_document_turn(message: str) -> DocumentTurnResult:
    text = (message or "").strip()
    if not text:
        return DocumentTurnResult()

    if is_supporting_document_skip_message(text):
        return DocumentTurnResult(action=ACTION_WAIVE, source="rules")

    if len(text) >= 40 and _DOCUMENT_PROVIDE_RE.search(text):
        return DocumentTurnResult(action=ACTION_ATTACH, content=text[:2000], source="rules")

    if len(text) >= 120 and not is_supporting_document_skip_message(text):
        return DocumentTurnResult(action=ACTION_ATTACH, content=text[:2000], source="rules_heuristic")

    return DocumentTurnResult()


def _llm_document_turn(
    message: str,
    *,
    draft: dict[str, Any],
    trace_id: str,
    llm: LLMClient | None = None,
) -> DocumentTurnResult | None:
    client = llm or LLMClient()
    if not client.is_configured():
        return None

    user_prompt = (
        f"Leave: sick, {draft.get('start_date') or '?'} → {draft.get('end_date') or '?'}\n"
        f"Reason: {draft.get('reason') or '(empty)'}\n\n"
        f"User answer about doctor's note / document:\n{(message or '').strip()}\n\n"
        "Return JSON only."
    )
    out = client.chat_json(
        system_prompt=_DOCUMENT_LLM_SYSTEM,
        user_prompt=user_prompt,
        trace_id=trace_id or "leave-document-llm",
    )
    if not isinstance(out, dict):
        return None
    if float(out.get("confidence") or 0.0) < CONFIDENCE_LLM_FALLBACK:
        return None

    intent = str(out.get("intent") or "").strip().lower()
    if intent == "waive":
        logger.info("leave_document_llm waive trace_id=%s", trace_id)
        return DocumentTurnResult(action=ACTION_WAIVE, source="llm")
    if intent == "provide":
        doc = str(out.get("document_text") or message or "").strip()
        if len(doc) >= 8 and not is_supporting_document_skip_message(doc):
            logger.info("leave_document_llm provide trace_id=%s", trace_id)
            return DocumentTurnResult(
                action=ACTION_ATTACH, content=doc[:2000], source="llm"
            )
    return DocumentTurnResult()


def resolve_document_turn(
    message: str,
    *,
    draft: dict[str, Any] | None = None,
    trace_id: str = "",
    use_llm: bool = True,
) -> DocumentTurnResult:
    """Rules-first; LLM when refusal/provide intent is still ambiguous."""
    from chat.services.leave.reason_correction_parser import looks_like_reason_correction

    if looks_like_reason_correction(message):
        return DocumentTurnResult()

    rules = _rules_document_turn(message)
    if rules.action != ACTION_NONE:
        return rules

    text = (message or "").strip()
    if use_llm and len(text) >= 3:
        llm_dec = _llm_document_turn(
            message, draft=draft or {}, trace_id=trace_id
        )
        if llm_dec and llm_dec.action != ACTION_NONE:
            return llm_dec

    return DocumentTurnResult()


def is_document_slot_resolvable(
    message: str,
    *,
    draft: dict[str, Any] | None = None,
    use_llm: bool = False,
) -> bool:
    return resolve_document_turn(
        message, draft=draft, use_llm=use_llm
    ).action != ACTION_NONE


def apply_document_answer(
    draft: dict[str, Any],
    message: str,
    *,
    trace_id: str = "",
    use_llm: bool = True,
) -> bool:
    """Apply waive or attach to draft. Returns True when the turn was handled."""
    decision = resolve_document_turn(
        message, draft=draft, trace_id=trace_id, use_llm=use_llm
    )
    if decision.action == ACTION_WAIVE:
        draft["supporting_document_waived"] = True
        draft.pop("document_text", None)
        return True
    if decision.action == ACTION_ATTACH and decision.content:
        draft["document_text"] = decision.content[:2000]
        draft.pop("supporting_document_waived", None)
        return True
    return False
