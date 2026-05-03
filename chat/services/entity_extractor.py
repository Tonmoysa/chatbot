import re
from datetime import date, timedelta
from typing import Any

from chat.services.llm_client import LLMClient


ENTITY_SYSTEM = """You extract structured HR entities from the user message and short context.
Reply with STRICT JSON only (no markdown, no explanation) using this shape:
{
  "amount": null or number,
  "currency": "USD" or string,
  "start_date": "YYYY-MM-DD" or null,
  "end_date": "YYYY-MM-DD" or null,
  "date": "YYYY-MM-DD" or null,
  "days": null or number,
  "leave_type": "annual"|"sick"|"casual"|null,
  "request_id": string or null,
  "description": string or null,
  "policy_topic": string or null,
  "reason": string or null
}
Use null when unknown. Never invent personal identifiers not present in text.
"""


class EntityExtractor:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm or LLMClient()

    def extract(
        self,
        message: str,
        intent: str,
        context_lines: list[str],
        trace_id: str,
    ) -> dict[str, Any]:
        ctx = "\n".join(context_lines[-8:])
        user_block = f"Intent: {intent}\nRecent context:\n{ctx}\n\nUser message:\n{message}"
        if self._llm.is_configured():
            out = self._llm.chat_json(
                system_prompt=ENTITY_SYSTEM,
                user_prompt=user_block,
                trace_id=trace_id,
            )
            if isinstance(out, dict):
                merged = self._rule_enrich(message, out)
                return {"entities": merged, "source": "llm"}
        entities = self._rule_enrich(message, {})
        return {"entities": entities, "source": "rules"}

    def _rule_enrich(self, message: str, base: dict[str, Any]) -> dict[str, Any]:
        e = {k: base.get(k) for k in (
            "amount",
            "currency",
            "start_date",
            "end_date",
            "date",
            "days",
            "leave_type",
            "request_id",
            "description",
            "policy_topic",
            "reason",
        )}
        low = message.lower()
        m = re.search(
            r"\b(\d+(?:\.\d+)?)\s*(usd|\$|dollars?|eur|gbp)?\b", low, re.I
        )
        if m and e.get("amount") is None:
            try:
                e["amount"] = float(m.group(1))
            except ValueError:
                pass
        if re.search(r"\btomorrow\b", low):
            d = date.today() + timedelta(days=1)
            ds = d.isoformat()
            if e.get("date") is None:
                e["date"] = ds
            if e.get("start_date") is None:
                e["start_date"] = ds
        if re.search(r"\btoday\b", low):
            d = date.today().isoformat()
            if e.get("date") is None:
                e["date"] = d
        if re.search(r"\bnext week\b", low):
            d0 = date.today() + timedelta(days=7)
            if e.get("start_date") is None:
                e["start_date"] = d0.isoformat()
        mrid = re.search(r"\b(req|request)[-_#]?\s*([A-Za-z0-9-]{4,})\b", message, re.I)
        if mrid and not e.get("request_id"):
            e["request_id"] = mrid.group(2)
        if re.search(r"\bsick\b", low):
            e["leave_type"] = e.get("leave_type") or "sick"
        if re.search(r"\bannual|vacation|pto\b", low):
            e["leave_type"] = e.get("leave_type") or "annual"
        return e
