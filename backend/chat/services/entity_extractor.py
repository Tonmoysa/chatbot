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
        # Guardrail: for leave-like messages without an explicit range or day-count,
        # ignore any LLM-provided end_date/days that may have leaked from prior context.
        looks_like_leave = bool(
            re.search(r"(leave|time off|pto|vacation|holiday|day off|ছুটি|chuti|chhuti)", low)
        )
        has_explicit_range = bool(
            re.search(r"\b(from|to|until|till)\b", low) or re.search(r"(থেকে|পর্যন্ত)", message)
        )
        has_explicit_days = bool(re.search(r"\b\d+(\.\d+)?\s*(day|days|din|diner)\b", low))
        has_any_digit = bool(re.search(r"\d", message))
        if looks_like_leave and not has_explicit_range:
            if not has_any_digit and not has_explicit_days:
                # e.g. "amar kalke chuti lagbe" -> default to a single day leave
                e["days"] = None
                e["end_date"] = None
            else:
                # a single date was provided, but no range keyword -> treat as one day
                e["end_date"] = None
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
        # Bengali/Banglish: "kal/kalke/agami kal" => tomorrow
        if re.search(r"(আগামীকাল|kalke|kal)", low):
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

        # Numeric date parsing for common user inputs like "5-7-2026" or "05/07/2026"
        if e.get("start_date") is None:
            m_iso = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", message)
            m_dmy = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", message)
            if m_iso:
                y, mo, da = m_iso.group(1), m_iso.group(2), m_iso.group(3)
                try:
                    ds = date(int(y), int(mo), int(da)).isoformat()
                    e["start_date"] = ds
                    if e.get("date") is None:
                        e["date"] = ds
                except Exception:
                    pass
            elif m_dmy:
                d1, m1, y1 = m_dmy.group(1), m_dmy.group(2), m_dmy.group(3)
                yy = int(y1)
                if yy < 100:
                    yy += 2000
                # Assume D-M-YYYY for this project locale
                try:
                    ds = date(yy, int(m1), int(d1)).isoformat()
                    e["start_date"] = ds
                    if e.get("date") is None:
                        e["date"] = ds
                except Exception:
                    pass
        # request/reference ids (support "request REQ-123", "Reference: MOCK-XXXX", etc.)
        mrid = re.search(r"\b(req|request)[-_#:]?\s*([A-Za-z0-9-]{4,})\b", message, re.I)
        if mrid and not e.get("request_id"):
            e["request_id"] = mrid.group(2)
        mref = re.search(r"\b(ref|reference)[-_#:]?\s*([A-Za-z0-9-]{4,})\b", message, re.I)
        if mref and not e.get("request_id"):
            e["request_id"] = mref.group(2)
        mmock = re.search(r"\bMOCK-[A-Za-z0-9]{6,}\b", message, re.I)
        if mmock and not e.get("request_id"):
            e["request_id"] = mmock.group(0).upper()
        if re.search(r"\bsick\b", low):
            e["leave_type"] = e.get("leave_type") or "sick"
        if re.search(r"\bannual|vacation|pto\b", low):
            e["leave_type"] = e.get("leave_type") or "annual"
        return e
