"""
Parse leave wizard collecting-phase slot answers — rules + LLM fallback.

Used when the user is answering one pending question (e.g. full/half day) via
voice or free-form Bengali that regex alone may miss.
"""

from __future__ import annotations

import logging

from chat.services.leave.normalization import parse_day_scope_answer
from chat.services.leave.reason_value import extract_reason_replacement, extract_reason_value
from chat.services.leave.turn_schema import CONFIDENCE_LLM_FALLBACK, LeaveFieldUpdate
from chat.services.leave_slots import SLOT_REASON, SLOT_SCOPE
from chat.services.llm_client import LLMClient

logger = logging.getLogger("hr_chatbot")

_COLLECTING_LLM_SYSTEM = """You extract the user's answer to ONE pending leave wizard question.

Return STRICT JSON only:
{
  "slot": "day_scope" | "reason" | "leave_payment_category" | null,
  "value": string | null,
  "confidence": 0.0 to 1.0
}

RULES
- day_scope value must be exactly "full" or "half"
- reason: extract ONLY the cause text (not instructions)
- Bengali voice STT: ফুল ডে / পুরো দিন / full day → day_scope full; হাফ দিন / half → half
- Reason correction: "শরীর খারাপ হবে না ফ্যামিলি প্রবলেম হবে" → reason "family problem" or "ফ্যামিলি প্রবলেম"
- If the message does not answer the pending question, return slot null
"""


def _rules_collecting_slot(
    message: str,
    *,
    pending_slot: str,
) -> LeaveFieldUpdate | None:
    text = (message or "").strip()
    if not text:
        return None

    if pending_slot == SLOT_SCOPE:
        scope = parse_day_scope_answer(text)
        if scope:
            return LeaveFieldUpdate(
                slot=SLOT_SCOPE,
                value=scope,
                raw_value=text,
            )

    if pending_slot == SLOT_REASON:
        repl = extract_reason_replacement(text)
        if repl:
            return LeaveFieldUpdate(slot=SLOT_REASON, value=repl, raw_value=text)
        reason = extract_reason_value(text, edit_context=True)
        if reason and len(reason) >= 3:
            return LeaveFieldUpdate(slot=SLOT_REASON, value=reason, raw_value=text)

    return None


def _llm_collecting_slot(
    message: str,
    *,
    pending_slot: str,
    draft: dict,
    trace_id: str,
    llm: LLMClient | None = None,
) -> LeaveFieldUpdate | None:
    client = llm or LLMClient()
    if not client.is_configured():
        return None

    user_prompt = (
        f"Pending question slot: {pending_slot}\n"
        f"Draft reason: {draft.get('reason') or '(empty)'}\n"
        f"Draft payment: {draft.get('leave_payment_category') or '?'}\n"
        f"Draft scope: {draft.get('day_scope') or '?'}\n"
        f"Draft dates: {draft.get('start_date') or '?'} → {draft.get('end_date') or '?'}\n\n"
        f"User message:\n{(message or '').strip()}\n\n"
        "Return JSON only."
    )
    out = client.chat_json(
        system_prompt=_COLLECTING_LLM_SYSTEM,
        user_prompt=user_prompt,
        trace_id=trace_id or "leave-collecting-llm",
    )
    if not isinstance(out, dict):
        return None

    slot = str(out.get("slot") or pending_slot or "").strip()
    value = out.get("value")
    confidence = float(out.get("confidence") or 0.0)
    if not slot or value is None or not str(value).strip():
        return None
    if confidence < CONFIDENCE_LLM_FALLBACK:
        return None

    if slot == SLOT_SCOPE:
        scope = parse_day_scope_answer(str(value)) or str(value).strip().lower()
        if scope in ("full", "half"):
            logger.info(
                "leave_collecting_llm scope trace_id=%s value=%s",
                trace_id,
                scope,
            )
            return LeaveFieldUpdate(slot=SLOT_SCOPE, value=scope, raw_value=message)

    if slot == SLOT_REASON:
        reason = str(value).strip()
        if len(reason) >= 3:
            logger.info("leave_collecting_llm reason trace_id=%s", trace_id)
            return LeaveFieldUpdate(slot=SLOT_REASON, value=reason, raw_value=message)

    return None


def try_resolve_collecting_slot(
    message: str,
    *,
    pending_slot: str,
    draft: dict,
    entities: dict | None = None,
    trace_id: str = "",
    use_llm: bool = True,
) -> LeaveFieldUpdate | None:
    """Rules-first slot answer; LLM when voice/free-form still unresolved."""
    del entities
    rules = _rules_collecting_slot(message, pending_slot=pending_slot)
    if rules:
        return rules

    text = (message or "").strip()
    if not use_llm or len(text) < 3:
        return None

    return _llm_collecting_slot(
        message,
        pending_slot=pending_slot,
        draft=draft,
        trace_id=trace_id,
    )
