import re
from typing import Any

from chat.constants import (
    ALL_INTENTS,
    INTENT_APPROVAL_ESCALATION,
    INTENT_ATTENDANCE_CORRECTION,
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_EXPENSE_STATUS,
    INTENT_HR_POLICY,
    INTENT_LEAVE_BALANCE,
    INTENT_LEAVE_REQUEST,
    INTENT_REQUEST_STATUS,
    INTENT_UNKNOWN,
    INTENT_WFH_REQUEST,
)
from chat.services.llm_client import LLMClient


def _strong_expense_claim(message: str) -> bool:
    """Banglish / informal cost lines; do not match expense *status* queries."""
    low = (message or "").lower()
    if re.search(r"\b(expense|reimbursement|claim)\b", low) and re.search(
        r"\b(status|track|where)\b", low
    ):
        return False
    if re.search(r"\b(expense|reimbursement|claim)\b", low):
        return True
    if re.search(r"(taka|টাকা|cost|hoyeche|hoyese|খরচ|reimburse)", low) and re.search(
        r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?(?!\d)", message
    ):
        return True
    return False


def _strong_expense_day_summary(message: str) -> bool:
    """Same-day spend recap (not submitting a new line item with an amount)."""
    if _strong_expense_claim(message):
        return False
    low = (message or "").lower()
    raw = message or ""
    time_ok = bool(
        re.search(r"\b(today|ajke|aj\s+ke|eikhon|ei\s+din)\b", low)
        or re.search(r"(আজ|আজকে|এইদিন|আজকের)", raw)
    )
    # Banglish: "amar total cost koto hoyeche" — no explicit "today"; still a same-day spend recap.
    banglish_total_spend = bool(
        (
            re.search(r"\b(amar|my)\b", low)
            and (
                re.search(r"\b(total|mot|koto)\b", low)
                or re.search(r"(মোট|কত)", raw)
            )
            and re.search(
                r"\b(cost|kharcha|khoroch|kharch|expense|taka|money)\b",
                low,
            )
        )
        or re.search(
            r"(মোট\s*খরচ|total\s*kharcha|total\s*cost|kharcha\s*koto|খরচ\s*কত)",
            low,
        )
    )
    want_info = bool(
        re.search(
            r"\b(summary|summaries|breakdown|overview|how\s+much|total|totals|list|"
            r"forgot|don't remember|do not remember|lost track|remind|remaining|limit)\b",
            low,
        )
        or re.search(r"\bspent\b", low)
        or re.search(
            r"(ভুলে|ভুলে\s*গেছি|মোট|হিসাব|দেখাও|দেখান|কত\s*টাকা|কত\s*খরচ|সারাংশ|লিস্ট)",
            raw.lower(),
        )
        or re.search(r"\bkoto\s+hoyeche\b", low)
        or re.search(r"কত\s*হয়েছে", raw)
    )
    domain = bool(
        re.search(r"\b(expense|reimbursement|claim|spent|cost|money)\b", low)
        or re.search(r"(খরচ|টাকা|taka|খরচের)", raw.lower())
    )
    if banglish_total_spend and domain:
        return True
    return time_ok and want_info and domain


INTENT_SYSTEM = """You classify HR chatbot intents. Reply with STRICT JSON only (no prose):
{"intent":"<ONE_OF_INTENTS>","confidence":0.0-1.0}

ONE_OF_INTENTS must be exactly one of:
LEAVE_BALANCE, LEAVE_REQUEST, WFH_REQUEST, EXPENSE_CLAIM, EXPENSE_DAY_SUMMARY, EXPENSE_STATUS,
ATTENDANCE_CORRECTION, REQUEST_STATUS, HR_POLICY, APPROVAL_ESCALATION, UNKNOWN

Definitions:
- LEAVE_BALANCE: user asks remaining/vacation/PTO balance
- LEAVE_REQUEST: user wants to book/take/apply leave
- WFH_REQUEST: work from home
- EXPENSE_CLAIM: submit reimbursement/expense
- EXPENSE_DAY_SUMMARY: how much spent today / daily expense total / remaining 300 BDT limit; also Banglish like "amar total cost koto"
- EXPENSE_STATUS: track expense/reimbursement status
- ATTENDANCE_CORRECTION: fix clock-in/out, attendance mistake
- REQUEST_STATUS: generic status of leave/wfh/etc
- HR_POLICY: questions about company HR rules
- APPROVAL_ESCALATION: escalate pending approval
- UNKNOWN: none of the above
"""


class IntentDetector:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self._llm = llm or LLMClient()

    def detect(self, message: str, trace_id: str) -> dict[str, Any]:
        text = (message or "").lower()
        # Strong heuristic overrides (esp. for Bengali/Banglish) so LLM misclassifications
        # don't break the workflow.
        strong_leave_request = bool(
            re.search(r"(ছুটি|chuti|chhuti|holiday)", text)
            and re.search(r"(চাই|lagbe|lage|dorkar|need|apply|request)", text)
        )
        strong_day_summary = _strong_expense_day_summary(message)
        if strong_day_summary:
            return {
                "intent": INTENT_EXPENSE_DAY_SUMMARY,
                "confidence": 0.99,
                "source": "rules_override",
            }
        if self._llm.is_configured():
            out = self._llm.chat_json(
                system_prompt=INTENT_SYSTEM,
                user_prompt=f"User message:\n{message}",
                trace_id=trace_id,
            )
            if out and isinstance(out.get("intent"), str):
                intent = out["intent"].strip().upper()
                if intent in ALL_INTENTS:
                    if strong_leave_request and intent != INTENT_LEAVE_REQUEST:
                        return {"intent": INTENT_LEAVE_REQUEST, "confidence": 0.99, "source": "rules_override"}
                    if _strong_expense_claim(message) and intent not in (
                        INTENT_EXPENSE_CLAIM,
                        INTENT_EXPENSE_STATUS,
                    ):
                        return {
                            "intent": INTENT_EXPENSE_CLAIM,
                            "confidence": 0.99,
                            "source": "rules_override",
                        }
                    return {
                        "intent": intent,
                        "confidence": float(out.get("confidence") or 0),
                        "source": "llm",
                    }
        return {"intent": self._rule_intent(text, message), "confidence": 0.6, "source": "rules"}

    def _rule_intent(self, text: str, raw_message: str = "") -> str:
        # Bengali / Banglish keywords (fallback path when LLM isn't used or fails)
        if _strong_expense_day_summary(raw_message or text):
            return INTENT_EXPENSE_DAY_SUMMARY
        if re.search(r"(ছুটি|chuti|chhuti|holiday)", text) and re.search(
            r"(চাই|lagbe|lage|dorkar|need|apply|request)", text
        ):
            return INTENT_LEAVE_REQUEST
        if re.search(r"(ছুটি|chuti|chhuti)", text) and re.search(
            r"(কত|koto|baki|remaining|balance)", text
        ):
            return INTENT_LEAVE_BALANCE
        if re.search(r"\b(balance|remaining|how many days|pto|vacation left)\b", text):
            return INTENT_LEAVE_BALANCE
        if re.search(r"\b(wfh|work from home|remote)\b", text):
            return INTENT_WFH_REQUEST
        if re.search(r"\b(expense|reimbursement|claim)\b", text) and re.search(
            r"\b(status|track|where)\b", text
        ):
            return INTENT_EXPENSE_STATUS
        if re.search(r"\b(expense|reimbursement|claim)\b", text):
            return INTENT_EXPENSE_CLAIM
        # Banglish cost/reimbursement phrasing without English "expense"
        if re.search(r"(taka|টাকা|cost|hoyeche|hoyese|খরচ|reimburse)", text.lower()) and re.search(
            r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?(?!\d)", text
        ):
            return INTENT_EXPENSE_CLAIM
        if re.search(r"\b(attendance|clock|timesheet|punch)\b", text) and re.search(
            r"\b(wrong|mistake|correct|fix)\b", text
        ):
            return INTENT_ATTENDANCE_CORRECTION
        if re.search(r"\b(status|tracking|pending)\b", text) and re.search(
            r"\b(request|application|ticket)\b", text
        ):
            return INTENT_REQUEST_STATUS
        if re.search(r"\b(policy|handbook|hr rule|guideline)\b", text):
            return INTENT_HR_POLICY
        if re.search(r"\b(escalat|escalate)\b", text.lower()):
            return INTENT_APPROVAL_ESCALATION
        if re.search(r"\b(manager|supervisor)\b", text.lower()) and re.search(
            r"\b(not approved|still pending|too long|slow)\b", text.lower()
        ):
            return INTENT_APPROVAL_ESCALATION
        if re.search(
            r"\b(leave|pto|vacation|time off|sick day|day off|holiday)\b", text
        ):
            if re.search(r"\b(request|apply|book|need|take)\b", text):
                return INTENT_LEAVE_REQUEST
            return INTENT_LEAVE_BALANCE
        return INTENT_UNKNOWN
