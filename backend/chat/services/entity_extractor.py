import re
from datetime import date, timedelta
from typing import Any

from chat.constants import INTENT_EXPENSE_CLAIM, INTENT_LEAVE_REQUEST
from chat.services.expense_incurred_date import infer_expense_incurred_date_iso
from chat.services.llm_client import LLMClient


_MONTH_MAP: dict[str, int] = {}
for _names, _num in (
    (("january", "jan"), 1),
    (("february", "feb"), 2),
    (("march", "mar"), 3),
    (("april", "apr"), 4),
    (("may",), 5),
    (("june", "jun"), 6),
    (("july", "jul"), 7),
    (("august", "aug"), 8),
    (("september", "sept", "sep"), 9),
    (("october", "oct"), 10),
    (("november", "nov"), 11),
    (("december", "dec"), 12),
):
    for _n in _names:
        _MONTH_MAP[_n] = _num

_MONTH_ALT = "|".join(sorted(_MONTH_MAP.keys(), key=len, reverse=True))


def _has_explicit_leave_duration(low: str) -> bool:
    return bool(
        re.search(r"\b\d+(\.\d+)?\s*(day|days|din|diner|দিন)\b", low)
    )


def _resolve_next_calendar_date(month_num: int, day: int, *, today: date) -> date | None:
    for y in range(today.year, today.year + 3):
        try:
            d = date(y, month_num, day)
        except ValueError:
            continue
        if d >= today:
            return d
    return None


def _infer_leave_calendar_start(low: str) -> str | None:
    """
    Banglish like \"may er 11 tarik\" / \"11 tarikh may\" — calendar day, not N days of leave.
    """
    from chat.services.bn_normalize import infer_bn_calendar_date, normalize_message_for_parsing

    today = date.today()
    low = normalize_message_for_parsing(low).lower()
    bn_iso = infer_bn_calendar_date(low, today=today)
    if bn_iso:
        return bn_iso
    # month (optional \"er\") day [tarik]
    m1 = re.search(
        rf"\b({_MONTH_ALT})\s+(?:er\s+)?(\d{{1,2}})(?:\s*(?:tarik|tarikh|th|st|nd|rd))?\b",
        low,
    )
    if m1:
        mon = _MONTH_MAP.get(m1.group(1), 0)
        try:
            dnum = int(m1.group(2))
        except ValueError:
            return None
        if mon:
            resolved = _resolve_next_calendar_date(mon, dnum, today=today)
            return resolved.isoformat() if resolved else None

    # day tarik ... month (short gap)
    m2 = re.search(
        rf"\b(\d{{1,2}})\s*(?:tarik|tarikh)\b[^\w]{{0,55}}?\b({_MONTH_ALT})\b",
        low,
    )
    if m2:
        try:
            dnum = int(m2.group(1))
        except ValueError:
            return None
        mon = _MONTH_MAP.get(m2.group(2), 0)
        if mon:
            resolved = _resolve_next_calendar_date(mon, dnum, today=today)
            return resolved.isoformat() if resolved else None

    # \"11 may\"
    m3 = re.search(rf"\b(\d{{1,2}})\s+({_MONTH_ALT})\b", low)
    if m3:
        try:
            dnum = int(m3.group(1))
        except ValueError:
            return None
        mon = _MONTH_MAP.get(m3.group(2), 0)
        if mon:
            resolved = _resolve_next_calendar_date(mon, dnum, today=today)
            return resolved.isoformat() if resolved else None

    return None


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
  "leave_payment_category": "paid"|"lwop"|null,
  "day_scope": "full"|"half"|null,
  "request_id": string or null,
  "description": string or null,
  "policy_topic": string or null,
  "reason": string or null,
  "expense_incurred_date": "YYYY-MM-DD" or null
}
Use null when unknown. Never invent personal identifiers not present in text.
For EXPENSE_CLAIM, expense_incurred_date is the calendar day the cost was incurred (not submission time).
For LEAVE_REQUEST, leave_payment_category is paid/time-off balance versus unpaid/LWOP; day_scope captures full versus half-day leave.
"""


def _expense_entity_system_prompt() -> str:
    today = date.today().isoformat()
    return f"""You extract EXPENSE CLAIM fields from Bangla, Banglish, and English messages.
Reply with STRICT JSON only (no markdown):
{{
  "expense_incurred_date": "YYYY-MM-DD" or null,
  "amount": null or number,
  "description": string or null,
  "expense_lines": [
    {{
      "category": "Lunch"|"Snack"|"Bus"|"Rickshaw"|"Train"|"Bike"|"CNG"|"Metro Rail"|"Other",
      "amount": number,
      "from_location": string or null,
      "to_location": string or null,
      "notes": string or null
    }}
  ] or null
}}
Rules:
- expense_lines: one object per cost when amounts can be inferred from the message.
- category: map food→Lunch, transport/rickshaw/bus→matching travel category, unknown→Other.
- Travel categories (Bus, Rickshaw, Train, Bike, CNG, Metro Rail) need from_location/to_location when a route is stated.
- Bengali voice: convert number words (একশো=100, দুইশো=200) and Bengali digits; বাসে/বাইকে/লাঞ্ছ → Bus/Bike/Lunch.
- Multi-line voice dumps: "বাসে mirpur টু motijheel একশো টাকা, তারপরে বাইকে ... দুইশো, লাঞ্ছ একশো" → three expense_lines.
- Free-form examples: "office theke bashay 150" → travel line with route + amount; "ajke khawa 200" → Lunch 200.
- expense_incurred_date: ajke/aj=today ({today}), kal/kalke=yesterday, agamikal=tomorrow.
- amount: top-level only when a single total is stated without line breakdown.
- Use null when unknown. Never invent locations or amounts not grounded in the message.
"""


def _leave_entity_system_prompt() -> str:
    today = date.today().isoformat()
    return f"""You extract LEAVE REQUEST fields from Bangla, Banglish, and English messages.
Reply with STRICT JSON only (no markdown):
{{
  "start_date": "YYYY-MM-DD" or null,
  "end_date": "YYYY-MM-DD" or null,
  "date": "YYYY-MM-DD" or null,
  "days": null or number,
  "leave_type": "sick"|"casual"|"annual"|"emergency"|null,
  "leave_payment_category": "paid"|"lwop"|null,
  "day_scope": "full"|"half"|null,
  "reason": string or null,
  "description": string or null
}}
Rules:
- reason: ALWAYS extract the user's stated cause when present — any natural wording.
  Health: matha betha, pet betha, jhor, fever, doctor, weakness, vomiting, etc.
  Personal: family program, wedding, travel, emergency at home, relative sick, etc.
  Causal Bangla/Banglish: text before tai/bole/er jonno is often the reason.
  Keep the user's own words (Bangla or English); do not paraphrase into generic text.
- leave_type: infer sick when illness/pain/fever/medical visit is implied; otherwise null unless explicit.
- Dates: kal/kalke/agamikal=tomorrow, ajke/aj=today (today is {today}).
- paid/lwop from paid/বেতনসহ vs unpaid/বেতন ছাড়া.
- full/half from full day/পুরো দিন vs half/হাফ.
- Use null when unknown. Never invent reasons or dates not in the message.
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
            if intent == INTENT_LEAVE_REQUEST:
                system_prompt = _leave_entity_system_prompt()
            elif intent == INTENT_EXPENSE_CLAIM:
                system_prompt = _expense_entity_system_prompt()
            else:
                system_prompt = ENTITY_SYSTEM
            out = self._llm.chat_json(
                system_prompt=system_prompt,
                user_prompt=user_block,
                trace_id=trace_id,
            )
            if isinstance(out, dict):
                merged = self._rule_enrich(message, out, intent=intent)
                return {"entities": merged, "source": "llm"}
        entities = self._rule_enrich(message, {}, intent=intent)
        return {"entities": entities, "source": "rules"}

    def _rule_enrich(
        self, message: str, base: dict[str, Any], intent: str | None = None
    ) -> dict[str, Any]:
        e = {k: base.get(k) for k in (
            "amount",
            "currency",
            "start_date",
            "end_date",
            "date",
            "days",
            "leave_type",
            "leave_payment_category",
            "day_scope",
            "request_id",
            "description",
            "policy_topic",
            "reason",
            "expense_incurred_date",
            "expense_lines",
            "document_read",
        )}
        low = message.lower()
        wants_doc_read_positive = bool(
            re.search(
                r"(eikhane|ekhane|ei|here).*(lekha|likha|text)|"
                r"(ki\s+lekha\s+ache)|"
                r"\b(read|extract|what\s+is\s+written|what's\s+written)\b|"
                r"(poro|পড়ো|পড়ো|পড়|পড়)",
                low,
            )
        )
        wants_doc_read_negated = bool(
            re.search(
                r"\b(don't|do\s*not|dont|never|no)\b.*\b(read|extract)\b|"
                r"\b(read|extract)\b.*\b(don't|do\s*not|dont|never)\b|"
                r"(poro|porte|porio|porben)\s+na\b|"
                r"(পড়ো|পড়ো|পড়|পড়|পড়বেন|পড়বেন)\s+না\b|"
                r"\bna\s+(poro|porte)\b",
                low,
            )
        )
        wants_doc_read = wants_doc_read_positive and not wants_doc_read_negated
        # Guardrail: for leave-like messages without an explicit range or day-count,
        # ignore any LLM-provided end_date/days that may have leaked from prior context.
        looks_like_leave = bool(
            re.search(
                r"(leave|time\s*off|pto|vacation|holiday|day\s*off|ছুটি|chuti|chhuti|chutti|"
                r"sick\s*leave|medical\s*leave|leave\s*lagbe|leave\s*nite|ছুটি\s*লাগবে|ছুটি\s*নিতে|ছুটি\s*চাই)",
                low,
            )
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
        if re.search(
            r"\b(tomorrow|tomarrow|tommorow|tommorrow|tomorow|tmrw|tmw)\b", low
        ):
            d = date.today() + timedelta(days=1)
            ds = d.isoformat()
            if e.get("date") is None:
                e["date"] = ds
            if e.get("start_date") is None:
                e["start_date"] = ds
        # Bengali/Banglish: tomorrow (avoid matching "kal" inside "kalker")
        if re.search(r"(আগামীকাল|kalke|kaller|\bkal\b)", low):
            d = date.today() + timedelta(days=1)
            ds = d.isoformat()
            if e.get("date") is None:
                e["date"] = ds
            if e.get("start_date") is None:
                e["start_date"] = ds
        if re.search(r"\b(today|ajke|aj\s*ke)\b|আজকে|আজ\b", low):
            d = date.today().isoformat()
            if e.get("date") is None:
                e["date"] = d
            if e.get("start_date") is None:
                e["start_date"] = d
        m_dur = re.search(r"\b(\d+)\s*(din|diner|days?|দিন)\b", low)
        if m_dur and e.get("days") is None:
            try:
                n = int(m_dur.group(1))
                e["days"] = float(n)
                if e.get("start_date") and not e.get("end_date"):
                    s = date.fromisoformat(str(e["start_date"]).split("T")[0])
                    e["end_date"] = (s + timedelta(days=n - 1)).isoformat()
            except ValueError:
                pass
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
        mexp = re.search(r"\b(EXP-\d{4}-[A-Z0-9]+)\b", message, re.I)
        if mexp and not e.get("request_id"):
            e["request_id"] = mexp.group(1).upper()
        mleave = re.search(r"\b(PHP-LEAVE-[A-Z0-9]+)\b", message, re.I)
        if mleave and not e.get("request_id"):
            e["request_id"] = mleave.group(1).upper()
        from chat.services.leave_slot_extraction import (
            explicit_leave_type_from_message,
            message_mentions_leave_type,
        )

        explicit_lt = explicit_leave_type_from_message(message)
        if explicit_lt:
            e["leave_type"] = explicit_lt
        elif intent == INTENT_LEAVE_REQUEST and not message_mentions_leave_type(message):
            # Keep LLM leave_type when a semantic reason or health signal is present.
            from chat.services.leave.normalization import (
                infer_leave_type_from_text,
                text_has_sick_signal,
            )

            llm_reason = str(e.get("reason") or e.get("description") or "").strip()
            has_semantic = bool(llm_reason) or text_has_sick_signal(message)
            if not has_semantic:
                e["leave_type"] = None
            elif not e.get("leave_type"):
                inferred = infer_leave_type_from_text(message, llm_reason)
                if inferred:
                    e["leave_type"] = inferred
        elif re.search(r"\bsick(?:ness)?\b|\bill(?:ness)?\b|অসুস্থ|জ্বর", low):
            e["leave_type"] = e.get("leave_type") or "sick"
        elif re.search(r"\bannual|vacation|\bpto\b|বার্ষিক", low):
            e["leave_type"] = e.get("leave_type") or "annual"
        if re.search(r"\bcasual\b|ক্যাজুয়াল|নৈমিত্তিক", low):
            e["leave_type"] = e.get("leave_type") or "casual"
        if re.search(r"\b(maternity)\b|মাতৃত্ব", low):
            e["leave_type"] = e.get("leave_type") or "maternity"
        if re.search(r"\b(paternity)\b|পিতৃত্ব", low):
            e["leave_type"] = e.get("leave_type") or "paternity"
        if re.search(r"\b(emergency)\b|জরুরি", low):
            e["leave_type"] = e.get("leave_type") or "emergency"
        if re.search(r"\b(compensatory|comp\s*off)\b", low):
            e["leave_type"] = e.get("leave_type") or "compensatory"
        from chat.services.leave.normalization import (
            message_explicitly_states_day_scope,
            parse_day_scope_answer,
        )

        if message_explicitly_states_day_scope(message):
            scope = parse_day_scope_answer(message)
            if scope:
                e["day_scope"] = scope

        if looks_like_leave:
            inferred_start = _infer_leave_calendar_start(low)
            if inferred_start:
                e["start_date"] = inferred_start
                if not e.get("date"):
                    e["date"] = inferred_start
                e["end_date"] = None
            if (
                not has_explicit_range
                and e.get("end_date") is None
                and not _has_explicit_leave_duration(low)
            ):
                e["days"] = None

        if intent == INTENT_EXPENSE_CLAIM:
            e["expense_incurred_date"] = infer_expense_incurred_date_iso(
                message=message, hints=e, today=date.today()
            )

        from chat.services.leave.normalization import should_suppress_inferred_leave_dates

        if should_suppress_inferred_leave_dates(message):
            e.pop("start_date", None)
            e.pop("end_date", None)
            e.pop("date", None)

        # If user asks "read what's written here", tag it for decision engine.
        if wants_doc_read:
            e["document_read"] = True

        return e

    def extract_rules_only(self, message: str, intent: str | None = None) -> dict[str, Any]:
        """Deterministic hints for duplicate detection (no LLM)."""
        return self._rule_enrich(message or "", {}, intent=intent)
