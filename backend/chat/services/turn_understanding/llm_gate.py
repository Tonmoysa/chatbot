"""LLM fallback for long / ambiguous utterances (Tier U extension)."""

from __future__ import annotations

import json
import logging
from typing import Any

from chat.services.turn_understanding.schemas import (
    ACT_CONTINUE,
    ACT_NEEDS_CLARIFY,
    ACT_OUT_OF_SCOPE,
    UtteranceResolution,
)

logger = logging.getLogger("hr_chatbot")

_UTTERANCE_LLM_SYSTEM = """You classify messages for a COMPANY HR assistant (leave, expense, attendance, uploaded policies).

STEP 1 — SCOPE
- in_scope TRUE only when the user talks about: leave/chuti, expense/khoroch/reimbursement, company policy/rules, leave balance, attendance, or HR workflow.
- in_scope FALSE for: weather, sports, cricket, recipes, coding, general trivia, celebrity gossip, travel tips unrelated to company reimbursement, "eid kobe", calendar trivia.

STEP 2 — ACT (only when in_scope true)
Allowed primary_act: add, modify, delete, submit, slot_answer, summary, query_policy, query_status, workflow_switch, cancel, needs_clarify, continue_wizard
Use out_of_scope ONLY when in_scope is false.

Return ONLY JSON:
{
  "primary_act": "...",
  "domain": "leave" | "expense" | "policy" | null,
  "confidence": 0.0 to 1.0,
  "in_scope": true | false,
  "needs_clarify": false,
  "entities": { "hr_domains": ["leave","expense","policy"] }
}

RULES
- Long messages may mention several topics — set needs_clarify true if leave+expense+policy mixed without clear priority.
- query_policy for company rules / handbook / "leave policy bolo".
- query_status for leave balance remaining.
- add/modify/delete/submit/summary for expense draft actions.
- workflow_switch for new leave application phrases.
- Never invent dates, amounts, or leave types not in the user message.
"""


def _build_llm_user_prompt(
    message: str,
    *,
    snapshot: Any,
    last_question: str = "",
) -> str:
    lines = [
        f"User message:\n{message}",
    ]
    if last_question:
        lines.append(f"\nBot last asked:\n{last_question[:500]}")
    if getattr(snapshot, "active_prompt_domain", None):
        lines.append(
            f"\nPending prompt: domain={snapshot.active_prompt_domain} "
            f"slot={snapshot.active_prompt_slot} kind={snapshot.expected_answer_kind}"
        )
    if getattr(snapshot, "leave_active", False):
        lines.append("\nContext: leave wizard active")
    if getattr(snapshot, "expense_domain_active", False):
        lines.append("\nContext: expense draft active")
    lines.append("\nReturn JSON only.")
    return "\n".join(lines)


def try_llm_resolve(
    message: str,
    *,
    snapshot: Any,
    last_question: str = "",
    trace_id: str = "",
) -> UtteranceResolution | None:
    """Return None when LLM unavailable or low confidence — caller uses rules."""
    text = (message or "").strip()
    if len(text) < 48:
        return None
    try:
        from chat.services.llm_client import LLMClient

        client = LLMClient()
        if not client.is_configured():
            return None
        out = client.chat_json(
            system_prompt=_UTTERANCE_LLM_SYSTEM,
            user_prompt=_build_llm_user_prompt(
                text, snapshot=snapshot, last_question=last_question
            ),
            trace_id=trace_id or "utterance-llm",
        )
    except Exception as exc:
        logger.debug("utterance_llm_failed: %s", exc)
        return None

    if not isinstance(out, dict):
        return None

    act = str(out.get("primary_act") or ACT_CONTINUE).strip().lower()
    confidence = float(out.get("confidence") or 0.0)
    in_scope = bool(out.get("in_scope", True))

    if not in_scope and confidence >= 0.75:
        return UtteranceResolution(
            primary_act=ACT_OUT_OF_SCOPE,
            confidence=confidence,
            in_scope=False,
            reason="llm_out_of_scope",
            source="llm",
        )

    if confidence < 0.75:
        return None

    if act == ACT_OUT_OF_SCOPE and not in_scope:
        return UtteranceResolution(
            primary_act=ACT_OUT_OF_SCOPE,
            confidence=confidence,
            in_scope=False,
            reason="llm_out_of_scope",
            source="llm",
        )

    if bool(out.get("needs_clarify")):
        return UtteranceResolution(
            primary_act=ACT_NEEDS_CLARIFY,
            confidence=confidence,
            needs_clarify=True,
            clarify_kind="llm_ambiguous",
            reason="llm_needs_clarify",
            source="llm",
        )

    domain = out.get("domain")
    if isinstance(domain, str):
        domain = domain.strip() or None
    else:
        domain = None

    entities = out.get("entities")
    if not isinstance(entities, dict):
        entities = {}

    return UtteranceResolution(
        primary_act=act,
        domain=domain,
        confidence=confidence,
        in_scope=in_scope,
        entities=entities,
        reason="llm_resolve",
        source="llm",
    )
