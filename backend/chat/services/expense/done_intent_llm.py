"""LLM detection for finish-collecting intent (all done, everything perfect, …)."""

from __future__ import annotations

import logging
from typing import Any

from chat.services.llm_client import LLMClient

logger = logging.getLogger("hr_chatbot")

_DONE_INTENT_LLM_SYSTEM = """You detect whether the user wants to FINISH entering expense lines and move on.

They are in an expense wizard (collecting or clarify). Return ONLY JSON:

{
  "finish_collecting": true,
  "confidence": 0.0
}

Set finish_collecting = true when the user signals they are DONE adding lines, e.g.:
- all done, everything is okay/perfect/fine/good
- that's all, nothing more, I'm done, we're done
- sob thik, shob complete, bas ei, shesh
- warm wrap-up without naming new amounts/categories

Set finish_collecting = false when the user:
- Names amounts, categories, or routes (145 bus, lunch 100, mirpur to gulshan)
- Submits to CRM (joma daw, submit koro) — that is submit, not done-collecting
- Asks a side question (policy, leave balance)
- Corrects a line (bus 50 hobe, remove train)
- Says yes/no to a specific prompt only (ha, thik ache) without wrap-up meaning
- Navigates elsewhere (expense e jao, leave apply)

confidence: 0.0–1.0 how sure you are.
"""


def done_intent_llm_enabled(*, use_llm: bool = True) -> bool:
    if not use_llm:
        return False
    return LLMClient().is_configured()


def llm_json_to_done_intent(raw: dict[str, Any] | None) -> bool:
    if not isinstance(raw, dict):
        return False
    if not bool(raw.get("finish_collecting")):
        return False
    try:
        conf = float(raw.get("confidence") or 0)
    except (TypeError, ValueError):
        conf = 0.0
    return conf >= 0.55


def parse_finish_collecting_llm(
    message: str,
    trace_id: str = "",
    *,
    use_llm: bool = True,
) -> bool:
    if not done_intent_llm_enabled(use_llm=use_llm):
        return False
    text = (message or "").strip()
    if not text:
        return False
    client = LLMClient()
    try:
        data = client.chat_json(
            system_prompt=_DONE_INTENT_LLM_SYSTEM,
            user_prompt=text,
            trace_id=trace_id or "expense-done-intent",
        )
    except Exception as exc:
        logger.warning(
            "done_intent_llm_failed trace_id=%s err=%s",
            trace_id or "expense-done-intent",
            type(exc).__name__,
        )
        return False
    return llm_json_to_done_intent(data)
