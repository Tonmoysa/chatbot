"""Parse leave wizard turns — field edits with inline values (rules + optional LLM)."""

from __future__ import annotations

import logging
import re
from typing import Any

from chat.services.leave.reason_value import extract_reason_value
from chat.services.leave.turn_schema import (
    CONFIDENCE_LLM_FALLBACK,
    TURN_CONFIRM,
    TURN_DENY,
    TURN_EDIT_FIELD,
    TURN_NONE,
    TURN_UNCLEAR,
    LeaveFieldUpdate,
    LeaveTurnDecision,
)
from chat.services.leave_confirm import (
    _EDIT_RE,
    is_confirmation_cancel,
    is_confirmation_yes,
    parse_edit_field_choice,
    parse_edit_slot,
)
from chat.services.leave_slots import (
    SLOT_DATES,
    SLOT_PAYMENT,
    SLOT_REASON,
    SLOT_SCOPE,
)
from chat.services.llm_client import LLMClient

logger = logging.getLogger("hr_chatbot")

_EDIT_VALUE_SIGNAL_RE = re.compile(
    r"(?:"
    r"hobe|habe|hoy|হবে|ashole|actually|asole|"
    r"pet\s*betha|matha\s*betha|fever|family|travel|wedding|"
    r"paid|unpaid|lwop|full|half|"
    r"\d{4}-\d{2}-\d{2}|kal|kalke|tomorrow"
    r")",
    re.I | re.UNICODE,
)

_LEAVE_EDIT_LLM_SYSTEM = """You extract a SINGLE leave draft field update from a user edit message.

The user is editing an existing leave draft at review or edit-menu stage.
Return STRICT JSON only:
{
  "slot": "reason" | "leave_payment_category" | "day_scope" | "leave_dates" | null,
  "value": string or null,
  "confidence": 0.0 to 1.0
}

RULES
- slot reason: extract ONLY the cause text (e.g. "pet betha", "family program"), not instructions
- slot leave_payment_category: "paid" or "lwop" (unpaid)
- slot day_scope: "full" or "half"
- slot leave_dates: ISO date YYYY-MM-DD or short phrase if clear
- Ignore filler: accha, tumi change koro, reason ta hobe, ashole
- If no clear value, return slot null and value null
"""


def draft_context_block(draft: dict[str, Any], *, stage: str = "") -> str:
    lines: list[str] = []
    if stage:
        lines.append(f"Stage: {stage}")
    lines.append(f"Reason: {draft.get('reason') or '(empty)'}")
    lines.append(f"Payment: {draft.get('leave_payment_category') or '?'}")
    lines.append(f"Scope: {draft.get('day_scope') or '?'}")
    lines.append(f"Dates: {draft.get('start_date') or '?'} → {draft.get('end_date') or '?'}")
    return "\n".join(lines)


def detect_edit_target_slot(message: str) -> str | None:
    slot = parse_edit_slot(message)
    if slot:
        return slot
    return parse_edit_field_choice(message)


def message_has_inline_edit_value(message: str, slot: str) -> bool:
    text = (message or "").strip()
    if not text or not slot:
        return False
    if slot == SLOT_REASON:
        return bool(extract_reason_value(text, edit_context=True))
    if slot == SLOT_SCOPE:
        low = text.lower()
        return bool(re.search(r"\b(half|full)\b|হাফ|পুরো", low, re.I))
    if slot == SLOT_PAYMENT:
        low = text.lower()
        return bool(re.search(r"\b(paid|unpaid|lwop)\b|বেতন", low, re.I))
    if slot == SLOT_DATES:
        return bool(re.search(r"\d{4}-\d{2}-\d{2}|\b(kal|kalke|tomorrow|ajke)\b", text, re.I))
    return bool(_EDIT_VALUE_SIGNAL_RE.search(text))


def extract_inline_field_value(
    slot: str,
    message: str,
    *,
    entities: dict[str, Any] | None = None,
    edit_context: bool = True,
) -> str | None:
    """Best-effort value for one slot from a combined edit utterance."""
    from chat.services.leave_workflow import (
        _force_scope_from_message,
        _infer_payment_category,
    )

    text = (message or "").strip()
    ent = entities or {}

    if slot == SLOT_REASON:
        llm_reason = str(ent.get("reason") or ent.get("description") or "").strip()
        if llm_reason and len(llm_reason) >= 3:
            return llm_reason[:2000]
        val = extract_reason_value(text, edit_context=edit_context)
        return val

    if slot == SLOT_SCOPE:
        probe: dict[str, Any] = {}
        if _force_scope_from_message(text, probe):
            return str(probe.get("day_scope") or "")
        scope = str(ent.get("day_scope") or "").strip().lower()
        return scope or None

    if slot == SLOT_PAYMENT:
        probe = {}
        _infer_payment_category(text, probe, force=True)
        pay = str(probe.get("leave_payment_category") or "").strip().lower()
        if pay:
            return pay
        pay_ent = str(ent.get("leave_payment_category") or "").strip().lower()
        return pay_ent or None

    if slot == SLOT_DATES:
        for key in ("start_date", "date", "end_date"):
            v = ent.get(key)
            if v:
                return str(v).split("T")[0]
        from chat.services.leave_slot_extraction import extract_leave_slots

        ex = extract_leave_slots(text, skip_leave_phrase_gate=True)
        if ex.start_date.value:
            return str(ex.start_date.value)
        return None

    return None


def parse_turn_rules(
    message: str,
    *,
    draft: dict[str, Any],
    review_pending: bool = False,
) -> LeaveTurnDecision:
    text = (message or "").strip()
    if not text:
        return LeaveTurnDecision()

    if review_pending:
        if is_confirmation_yes(text):
            return LeaveTurnDecision(turn_type=TURN_CONFIRM, confidence=1.0)
        if is_confirmation_cancel(text):
            return LeaveTurnDecision(turn_type=TURN_DENY, confidence=1.0)

    slot = detect_edit_target_slot(text)
    if slot and (_EDIT_RE.search(text) or parse_edit_field_choice(text)):
        value = extract_inline_field_value(slot, text, edit_context=True)
        if value:
            return LeaveTurnDecision(
                turn_type=TURN_EDIT_FIELD,
                confidence=0.92,
                field_update=LeaveFieldUpdate(slot=slot, value=value, raw_value=text),
                target_slot=slot,
                source="rules",
            )
        return LeaveTurnDecision(
            turn_type=TURN_EDIT_FIELD,
            confidence=0.6,
            target_slot=slot,
            source="rules_heuristic",
        )

    if review_pending and message_has_inline_edit_value(text, SLOT_REASON):
        val = extract_reason_value(text, edit_context=True)
        if val:
            return LeaveTurnDecision(
                turn_type=TURN_EDIT_FIELD,
                confidence=0.85,
                field_update=LeaveFieldUpdate(
                    slot=SLOT_REASON, value=val, raw_value=text
                ),
                target_slot=SLOT_REASON,
                source="rules",
            )

    return LeaveTurnDecision()


def parse_turn_llm(
    message: str,
    draft: dict[str, Any],
    *,
    target_slot: str = "",
    trace_id: str = "",
    llm: LLMClient | None = None,
) -> LeaveTurnDecision | None:
    text = (message or "").strip()
    if not text:
        return None
    client = llm or LLMClient()
    if not client.is_configured():
        return None

    user_prompt = (
        f"{draft_context_block(draft, stage='review_edit')}\n"
        f"Target field hint: {target_slot or 'auto'}\n\n"
        f"User message:\n{text}\n\n"
        "Return JSON only."
    )
    out = client.chat_json(
        system_prompt=_LEAVE_EDIT_LLM_SYSTEM,
        user_prompt=user_prompt,
        trace_id=trace_id or "leave-edit-llm",
    )
    if not isinstance(out, dict):
        return None

    slot = str(out.get("slot") or target_slot or "").strip()
    value = out.get("value")
    confidence = float(out.get("confidence") or 0.0)
    if not slot or value is None or not str(value).strip():
        return None

    return LeaveTurnDecision(
        turn_type=TURN_EDIT_FIELD,
        confidence=confidence,
        field_update=LeaveFieldUpdate(
            slot=slot,
            value=str(value).strip(),
            raw_value=text,
        ),
        target_slot=slot,
        source="llm",
    )


def resolve_leave_turn(
    message: str,
    *,
    draft: dict[str, Any],
    review_pending: bool = False,
    entities: dict[str, Any] | None = None,
    trace_id: str = "",
    use_llm: bool = True,
    router_turn: LeaveTurnDecision | None = None,
) -> LeaveTurnDecision:
    if router_turn is not None and router_turn.is_handled():
        if router_turn.turn_type == TURN_EDIT_FIELD and not (
            router_turn.field_update and router_turn.field_update.value
        ):
            pass
        else:
            return router_turn

    rules = parse_turn_rules(message, draft=draft, review_pending=review_pending)

    if rules.turn_type == TURN_EDIT_FIELD and rules.field_update:
        return rules

    if (
        use_llm
        and rules.turn_type == TURN_EDIT_FIELD
        and rules.target_slot
        and not (rules.field_update and rules.field_update.value)
    ):
        llm_dec = parse_turn_llm(
            message,
            draft,
            target_slot=rules.target_slot,
            trace_id=trace_id,
        )
        if llm_dec and llm_dec.field_update and llm_dec.confidence >= CONFIDENCE_LLM_FALLBACK:
            logger.info(
                "leave_turn_llm_edit trace_id=%s slot=%s",
                trace_id,
                llm_dec.target_slot,
            )
            return llm_dec
        if llm_dec and llm_dec.confidence < CONFIDENCE_LLM_FALLBACK:
            llm_dec.turn_type = TURN_UNCLEAR
            return llm_dec

    if rules.turn_type == TURN_EDIT_FIELD and rules.target_slot:
        val = extract_inline_field_value(
            rules.target_slot,
            message,
            entities=entities,
            edit_context=True,
        )
        if val:
            rules.field_update = LeaveFieldUpdate(
                slot=rules.target_slot, value=val, raw_value=message
            )
            rules.confidence = 0.88
            return rules

    return rules if rules.is_handled() else LeaveTurnDecision()
