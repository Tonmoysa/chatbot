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
from chat.services.expense_workflow import (
    wants_expense_spend_recap_query,
    wants_expense_summary,
)
from chat.services.expense.session_ledger import wants_session_expense_ledger_query
from chat.services.llm_client import LLMClient
from chat.services.policy_intent_helpers import is_expense_entitlement_query, is_rules_query


def _strong_hr_policy(message: str) -> bool:
    """User is asking about company rules/regulations/policy/handbook."""
    low = (message or "").lower()
    raw = message or ""
    if re.search(
        r"\b(rules?\s+(?:and|&)\s+regulations?|handbook|employee\s+handbook|"
        r"company\s+(?:rules?|regulations?|policy|policies)|"
        r"hr\s+(?:rule|rules|policy|policies)|guideline|guidelines)\b",
        low,
    ):
        return True
    if re.search(r"\b(rule|rules|regulation|regulations|policy|policies)\b", low):
        return True
    if re.search(r"\bleave\s+poli\b", low) or (
        re.search(r"\bpoli\b", low) and re.search(r"\bleave\b", low)
    ):
        return True
    if re.search(r"(সব\s*নিয়ম|সকল\s*নিয়ম|নিয়ম|বিধি|নীতি|হ্যান্ডবুক|রুলস|পলিসি)", raw):
        return True
    if re.search(
        r"\b(shob|sob|sokol)?\s*(niyom|niyam|bidhi|niti|rules?|policy|policies|handbook)\b",
        low,
    ):
        return True
    return False


def _strong_expense_claim(message: str) -> bool:
    """Banglish / informal cost lines; do not match expense *status* or *summary* queries."""
    # BN voice dumps: preprocess converts একশ/ষাট → digits before recap heuristics run.
    try:
        from chat.services.expense_extraction import message_contains_expense_claim_lines

        if message_contains_expense_claim_lines(message):
            return True
    except Exception:
        pass
    from chat.services.expense_workflow import wants_resume_or_show_expense

    if (
        wants_expense_summary(message)
        or wants_expense_spend_recap_query(message)
        or wants_resume_or_show_expense(message)
    ):
        return False
    from chat.services.policy_intent_helpers import is_rules_query

    if _strong_hr_policy(message) and is_rules_query(message):
        return False
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


def wants_post_submit_expense_summary(message: str) -> bool:
    """
    Recap of logged/submitted expenses (CRM), not the in-wizard review step.
    Used after submit or when the user asks how much they spent on a date.
    """
    if not wants_expense_summary(message):
        return False
    low = (message or "").lower()
    raw = message or ""
    if re.search(
        r"\b(forgot|don't remember|do not remember|lost track|remind)\b",
        low,
    ) or re.search(r"(ভুলে|ভুলে\s*গেছি)", raw):
        return True
    if re.search(r"(summery|summary|সারাংশ|list|lists|লিস্ট).{0,25}(daw|dao|দাও)", low):
        return True
    if re.search(
        r"(amar|my).{0,30}(total|mot|koto).{0,30}(cost|kharcha|khoroch|expense|taka)",
        low,
    ):
        return True
    if re.search(r"(ajke|ajker|today).{0,30}(koto|total|spent|খরচ)", low) or re.search(
        r"(আজ|আজকে|আজকের).{0,20}(কত|মোট|খরচ)", raw
    ):
        return True
    if re.search(
        r"(ajke|ajker|today|আজকে|আজকের).{0,40}(expense|খরচ).{0,40}"
        r"(list|summery|summary|লিস্ট|সারাংশ|breakdown)",
        low,
    ) or re.search(
        r"(ajke|ajker|today|আজকে|আজকের).{0,40}(expense|খরচ).{0,40}"
        r"(list|summery|summary|লিস্ট|সারাংশ|breakdown)",
        raw,
        re.I,
    ):
        return True
    return False


def _strong_expense_day_summary(message: str) -> bool:
    """Same-day spend recap (not submitting a new line item with an amount)."""
    try:
        from chat.services.expense.expense_policy import is_expense_daily_cap_query

        if is_expense_daily_cap_query(message):
            return False
    except Exception:
        pass
    if wants_post_submit_expense_summary(message):
        return True
    if wants_session_expense_ledger_query(message):
        return True
    if _strong_expense_claim(message):
        return False
    low = (message or "").lower()
    raw = message or ""
    time_ok = bool(
        re.search(r"\b(today|ajke|ajker|aj\s+ke|eikhon|ei\s+din|sara\s+din)\b", low)
        or re.search(r"(আজ|আজকে|এইদিন|আজকের|সারা\s*দিন)", raw)
    )
    # Banglish: "amar total cost koto hoyeche" — no explicit "today"; still a same-day spend recap.
    banglish_total_spend = bool(
        (
            re.search(r"\b(amar|amai|amake|my|ami|am)\b", low)
            and (
                re.search(r"\b(total|mot|koto)\b", low)
                or re.search(r"(মোট|কত)", raw)
            )
            and re.search(
                r"\b(cost|kharcha|khoroch|khorose|koroch|kharch|expense|taka|money|koroch)\b",
                low,
            )
        )
        or re.search(
            r"(মোট\s*খরচ|total\s*kharcha|total\s*cost|kharcha\s*koto|খরচ\s*কত)",
            low,
        )
    )
    domain = bool(
        re.search(
            r"\b(expense|reimbursement|claim|spent|cost|money|khorose|koroch|khoroch|kharcha|kharch)\b",
            low,
        )
        or re.search(r"(খরচ|টাকা|taka|খরচের)", raw.lower())
    )
    want_info = bool(
        re.search(
            r"\b(summary|summaries|summery|breakdown|overview|how\s+much|total|totals|list|"
            r"forgot|don't remember|do not remember|lost track|remind|remaining|limit)\b",
            low,
        )
        or re.search(r"\bspent\b", low)
        or re.search(
            r"(ভুলে|ভুলে\s*গেছি|মোট|হিসাব|দেখাও|দেখান|কত\s*টাকা|কত\s*খরচ|সারাংশ|লিস্ট)",
            raw.lower(),
        )
        or re.search(r"\bkoto\s+hoyeche\b", low)
        or re.search(r"\bkoto\s+hoise\b", low)
        or re.search(r"\bkoto\s+hoyese\b", low)
        or re.search(r"কত\s*হয়েছে", raw)
        or (
            domain
            and re.search(r"\b(amar|amai|amake|my)\b", low)
            and re.search(r"\b(bolo|daw|dao|dekhao|show|tell|list)\b", low)
        )
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
- EXPENSE_DAY_SUMMARY: how much spent today / daily expense total / list or summary of today's expenses / remaining 300 BDT limit; also Banglish like "amar total cost koto" or "ajker expense er list daw"; also pending draft questions like "pending kono expense ache tomar kache?" or "pending expense ta daw"
- EXPENSE_STATUS: track expense/reimbursement status
- ATTENDANCE_CORRECTION: fix clock-in/out, attendance mistake
- REQUEST_STATUS: generic status of leave/wfh/etc
- HR_POLICY: questions about company HR rules / policies / regulations / handbook
- APPROVAL_ESCALATION: escalate pending approval
- UNKNOWN: greetings, small talk, jokes, thanks, identity questions, off-topic chit-chat, or anything NOT clearly an HR action. When in doubt and the message has no HR keywords, choose UNKNOWN.

Classification examples (follow exactly):
- "hi" / "hello" / "hey" → UNKNOWN
- "kemon acho" / "how are you" / "ki khobor" → UNKNOWN
- "ki re ki koris" / "what's up" → UNKNOWN
- "amake ekta jokes bolo" / "tell me a joke" → UNKNOWN
- "thanks" / "dhonnobad" / "ok" → UNKNOWN
- "tumi ke" / "who are you" / "what's your name" → UNKNOWN
- "show me all rules and regulations" / "company rules" → HR_POLICY
- "leave policy ki" / "what's the leave policy" → HR_POLICY
- "kotodin chuti ache" / "how many leave days do I have" → LEAVE_BALANCE
- "kalke chuti lagbe" / "I want leave tomorrow" → LEAVE_REQUEST
- "350 taka cost hoyeche" / "submit 200 BDT expense" → EXPENSE_CLAIM
- "ajke koto khoroch hoyeche" → EXPENSE_DAY_SUMMARY
- "pending kono expense ache tomar kache?" / "amar kache pending expense ache ki?" → EXPENSE_DAY_SUMMARY
"""


_HR_SIGNAL_RE = re.compile(
    r"\b("
    r"leave|chuti|chhuti|holiday|pto|vacation|time\s*off|"
    r"expense|reimburs|claim|cost|kharcha|khoroch|taka|money|"
    r"salary|beton|maine|payroll|overtime|"
    r"attendance|clock|punch|timesheet|"
    r"policy|policies|rule|rules|regulation|regulations|handbook|guideline|"
    r"wfh|remote|work\s+from\s+home|"
    r"status|track|request|ticket|reference|ref|"
    r"manager|supervisor|hr|approval|escalat|"
    r"sick|maternity|paternity|emergency|"
    r"dress\s*code|ppe|safety|"
    r"document|receipt|invoice|bill|"
    r"crm|erp|kpi"
    r")\b",
    re.I,
)
_HR_SIGNAL_BN_RE = re.compile(
    r"(ছুটি|খরচ|বেতন|নিয়ম|বিধি|পলিসি|হ্যান্ডবুক|উপস্থিতি|"
    r"টাকা|রিইম্বার্স|মেডিকেল|অসুস্থ|অনুমোদন|ম্যানেজার)"
)
_CHITCHAT_RE = re.compile(
    r"\b("
    # English greetings
    r"hi|hello|hey|hola|salam|salaam|asalam|namaste|sup|yo|"
    # Banglish vocatives / greetings
    r"oi|oy|oye|ay|ei|hai|halo|hoy|are|abe|"
    r"good\s*(morning|afternoon|evening|night)|"
    # Gratitude / farewell
    r"thanks?|thx|ty|thank\s*you|dhonnobad|dhonnyobad|dhonnabaad|"
    r"bye|goodbye|tata|ttyl|cya|see\s*you|"
    # Tiny affirmations / negations
    r"yes|yeah|yep|no|nope|ok|okay|sure|fine|alright|hmm+|"
    # Banglish "how are you" variants
    r"kemon\s*ach[oe]n?|emon\s*ach[oe]|kemon\s*kemon|kmn\s*acho|"
    # Banglish "what's up / what are you doing"
    r"ki\s*khobor|ki\s*obostha|ki\s*koris|ki\s*koros|ki\s*korchish|"
    r"ki\s*kor[oae]|ki\s*korcho|ki\s*korch[ie]n|ki\s*korben|"
    r"ki\s*re|ki\s*naam|tumi\s*ke|tomar\s*naam|ki\s*bolb[oe]|"
    # English chit-chat (including typos like ypou / r u)
    r"what'?s\s*up|how\s*ar[ey]?\s*y?o?u?|how\s*r\s*u|how'?s\s*it\s*going|how'?s\s*things|"
    r"who\s*are\s*you|what'?s\s*your\s*name|"
    # Jokes / humor
    r"joke|jokes|jok|funny|"
    # Banglish affirmations / mood
    r"thik\s*ach[ei]|valo|bhalo|achi|acho|"
    r"lol|haha|hehe"
    r")\b",
    re.I,
)
_CHITCHAT_BN_RE = re.compile(
    r"(কেমন\s*আছ[ো]?|কেমন\s*আছেন|কী\s*খবর|কি\s*খবর|"
    r"হ্যালো|হাই|ওই|এই|"
    r"ধন্যবাদ|শুভ\s*সকাল|শুভ\s*রাত্রি|"
    r"ভালো\s*আছি|ভালো|"
    r"কী\s*কর|কি\s*কর|তুমি\s*কে|তোমার\s*নাম)"
)


# ---------------------------------------------------------------------------
# Wizard-answer signals.
#
# When a leave-wizard is mid-collection, we need to tell apart real wizard
# answers (which advance the form) from side-talk (which should NOT be
# fed into the wizard). For each step we know roughly what tokens count
# as a plausible answer; if none are present, the message is almost
# certainly side-talk and we route it to the conversational fallback.
# Step 4 (reason) and step 5 (document) accept free text, so they fall
# through to the wizard by default.
# ---------------------------------------------------------------------------
_WIZARD_PAYMENT_RE = re.compile(
    r"\b(paid|unpaid|lwop|pto|annual|casual|"
    r"with\s*pay|without\s*pay|on\s*pay|off\s*pay|bezeton|bezaton)\b",
    re.I,
)
_WIZARD_PAYMENT_BN_RE = re.compile(
    r"(বেতনসহ|বেতন\s*ছাড়া|বিনা\s*বেতন|বেতন\s*সহ)"
)
_WIZARD_DAYSCOPE_RE = re.compile(
    r"\b(full|half|fullday|halfday|ful\s*day|whole\s*day|half\s*day|semi)\b",
    re.I,
)
_WIZARD_DAYSCOPE_BN_RE = re.compile(
    r"(পুরো\s*দিন|হাফ\s*দিন|হাফ\s*ডে|অর্ধ\s*দিন|সম্পূর্ণ\s*দিন|অর্ধ|"
    r"ফুল{1,2}(?:ি)?\s*(?:ডে|দিন))"
)
_WIZARD_DATES_RE = re.compile(
    r"\b(today|tomorrow|yesterday|tonight|tonite|"
    r"next\s*(week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"this\s*(week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)|"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|"
    r"kal|ajke|aj|aaj|porshu|aagamikal|agamikal|gotokal)\b",
    re.I,
)
_WIZARD_DATES_BN_RE = re.compile(
    r"(আজ|কাল|আগামীকাল|গতকাল|পরশু|আগামী|গত|"
    r"জানুয়ারি|ফেব্রুয়ারি|মার্চ|এপ্রিল|মে|জুন|জুলাই|আগস্ট|সেপ্টেম্বর|অক্টোবর|নভেম্বর|ডিসেম্বর)"
)


# Pure greetings / explicit cancel signals: when the user opens a new line of
# conversation we should NOT keep dragging the old wizard along. These match
# only stand-alone short messages so we don't dismiss the form just because
# the user said "hi i need leave".
_FRESH_START_GREETING_RE = re.compile(
    r"^\s*("
    r"hi|hello|hey|hola|sup|yo|salam|salaam|asalam|namaste|"
    r"oi|oy|oye|ay|ei|hai|halo|hoy|"
    r"good\s*(morning|afternoon|evening|night)|"
    r"হ্যালো|হাই|ওই|এই|নমস্কার|আসসালামু\s*আলাইকুম"
    r")\s*[!.?,…]*\s*$",
    re.I,
)
_CANCEL_FORM_RE = re.compile(
    r"\b("
    r"cancel|stop|exit|quit|abort|reset|restart|nevermind|never\s*mind|"
    r"forget\s*it|forget\s*this|drop\s*it|"
    r"cancel\s*(the\s*)?(form|wizard|leave)|skip\s*(the\s*)?(form|wizard)|"
    r"bad\s*koro|bad\s*korbo|baad\s*koro|baad\s*den|baad\s*din|"
    r"chai\s*na|chaina|lagbe\s*na|lagbena|lagbe\s*nah|"
    r"ekhon\s*na|ekhon\s*tha[ck]|ekhon\s*lagbe\s*na|"
    r"vule\s*ja[oe]|bhule\s*ja[oe]|"
    r"form\s*cancel|form\s*bad"
    r")\b",
    re.I,
)
_CANCEL_FORM_BN_RE = re.compile(
    r"(বাদ\s*দাও|বাদ\s*দিন|বাদ\s*দে|বাদ\s*করো|বাদ\s*কর|"
    r"বাতিল|"
    r"ভুলে\s*যাও|ভুলে\s*যান|"
    r"চাই\s*না|লাগবে\s*না|লাগবেনা|দরকার\s*নেই|"
    r"এখন\s*না|এখন\s*থাক|এখন\s*লাগবে\s*না|"
    r"ফর্ম\s*বাতিল|ফর্ম\s*বাদ)"
)

# Stand-alone wizard confirmations — must not be classified as chit-chat in strict mode.
_WIZARD_CONFIRM_SHORT_RE = re.compile(
    r"^(?:"
    r"yes|yep|yeah|yup|ok|okay|sure|fine|alright|"
    r"ha|hae|haan|han|hmm|hmmm|ji|j|hoy|"
    r"no|nope|"
    r"হ্যাঁ|হ্যা|ঠিক\s*আছে|ঠিক|জমা\s*দাও|জমা\s*দিন|না|"
    r"thik\s*ache|thik|hmm?\s*yes|submit\s*koro|শেষ"
    r")\s*\.?$",
    re.I,
)


def _is_fresh_start_greeting(message: str) -> bool:
    """True when the message is just a stand-alone greeting (no follow-on)."""
    if not message:
        return False
    return bool(_FRESH_START_GREETING_RE.match(message.strip()))


def _is_cancel_form_request(message: str) -> bool:
    """True when the user wants to drop the current wizard / form."""
    if not message:
        return False
    return bool(
        _CANCEL_FORM_RE.search(message) or _CANCEL_FORM_BN_RE.search(message)
    )


# Off-topic questions during free-text wizard steps (e.g. "can I use airplane?").
_WIZARD_SIDE_QUESTION_RE = re.compile(
    r"(?:"
    r"\?\s*$|"
    r"^\s*(?:can|could|may|might|will|would|shall|should|is|are|was|were|"
    r"do|does|did|have|has|had|what|when|where|why|how|who|which|am\s+i|ami\s+ki)\b|"
    r"\b(can\s+i|could\s+i|may\s+i|am\s+i\s+allowed|is\s+it\s+(?:ok|okay|allowed))\b|"
    r"(?:ami\s+)?(?:ekta\s+)?question\s*korchi|প্রশ্ন\s*কর|"
    r"(কি\s+পারি|পারব\s+কি|আমি\s+কি\s+)"
    r")",
    re.I | re.UNICODE,
)


def looks_like_wizard_side_question(message: str) -> bool:
    """True when the user is asking a question, not supplying a wizard slot value."""
    from chat.services.leave_balance_intent import is_leave_balance_query
    from chat.services.expense.routing import is_expense_draft_status_question

    text = (message or "").strip()
    if not text:
        return False
    if is_expense_draft_status_question(text):
        return False
    if is_leave_balance_query(text):
        return True
    if not _WIZARD_SIDE_QUESTION_RE.search(text):
        return False
    low = text.lower()
    # Still on-topic for the leave wizard (payment/type/dates), not a random HR question.
    if re.search(
        r"\b(leave|chuti|chhuti|chutti|sick|casual|annual|paid|unpaid|lwop|pto|"
        r"half\s*day|full\s*day|tomorrow|today|kal|agamikal|ছুটি|বেতন)\b",
        low,
    ) and not re.search(
        r"\b(airplane|aeroplane|flight|plane|train|bus|travel|hotel|visa|"
        r"parking|smoking|commute)\b",
        low,
    ):
        return False
    return True


def _message_answers_wizard_step(message: str, step: str | None) -> bool:
    """True if the message plausibly answers the wizard's *current* step.

    Free-text steps (reason / supporting_document) accept answers unless the
    message is clearly a side question (e.g. travel eligibility), which the
    orchestrator handles outside the slot-filling flow.
    """
    if not step:
        return False
    if step in ("reason", "supporting_document"):
        text = (message or "").strip()
        if not text:
            return False
        if looks_like_wizard_side_question(message):
            return False
        try:
            from chat.services.wizard_turn_gate import is_casual_wizard_side_statement

            if is_casual_wizard_side_statement(message):
                return False
        except Exception:
            pass
        return True
    text = (message or "").strip()
    if not text:
        return False
    if step == "leave_payment_category":
        return bool(
            _WIZARD_PAYMENT_RE.search(text)
            or _WIZARD_PAYMENT_BN_RE.search(text)
        )
    if step == "day_scope":
        return bool(
            _WIZARD_DAYSCOPE_RE.search(text)
            or _WIZARD_DAYSCOPE_BN_RE.search(text)
        )
    if step == "leave_dates":
        if re.search(r"\d", text):
            return True
        return bool(
            _WIZARD_DATES_RE.search(text)
            or _WIZARD_DATES_BN_RE.search(text)
        )
    if step in ("leave_type",):
        from chat.services.leave.normalization import (
            looks_like_wizard_leave_type_answer,
            parse_wizard_leave_type_answer,
        )
        from chat.services.leave.session_action_memory import wants_leave_meta_question

        if wants_leave_meta_question(text):
            return False
        try:
            from chat.services.expense.session_action_memory import (
                wants_expense_meta_question,
            )

            if wants_expense_meta_question(text):
                return False
        except Exception:
            pass
        if looks_like_wizard_leave_type_answer(text) or parse_wizard_leave_type_answer(text):
            return True
        return False
    return True


def _looks_like_chitchat(message: str, *, strict: bool = False) -> bool:
    """True when the message looks like greeting / small talk and has no HR signal.

    When ``strict=True``, only explicit chit-chat regex hits count. The
    "short message" fallback is skipped so single-token wizard answers like
    "paid", "unpaid", "yes", "annual", "full day" are NOT misread as
    chit-chat. Use strict mode wherever a wizard is mid-collection.
    """
    if not message:
        return False
    text = message.strip()
    if not text:
        return False
    if _HR_SIGNAL_RE.search(text) or _HR_SIGNAL_BN_RE.search(text):
        return False
    if strict and _WIZARD_CONFIRM_SHORT_RE.match(text):
        return False
    if _CHITCHAT_RE.search(text) or _CHITCHAT_BN_RE.search(text):
        return True
    if strict:
        return False
    # Very short messages (<= 4 words) with no HR signal are almost certainly
    # chit-chat ("kotha kotha", "kichu na", "achi", etc.) — fall back to UNKNOWN
    # rather than letting the LLM hallucinate an HR intent.
    words = re.findall(r"\S+", text)
    if len(words) <= 4 and not re.search(r"\d", text):
        return True
    return False


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
        if is_expense_entitlement_query(message):
            return {
                "intent": INTENT_HR_POLICY,
                "confidence": 0.99,
                "source": "rules_override_entitlement",
            }
        from chat.services.expense.session_action_memory import (
            wants_expense_meta_question,
        )

        if wants_expense_meta_question(message):
            return {
                "intent": INTENT_EXPENSE_STATUS,
                "confidence": 0.99,
                "source": "rules_override_expense_meta",
            }
        from chat.services.leave.session_action_memory import wants_leave_meta_question

        if wants_leave_meta_question(message):
            return {
                "intent": INTENT_REQUEST_STATUS,
                "confidence": 0.99,
                "source": "rules_override_leave_meta",
            }
        if strong_day_summary:
            return {
                "intent": INTENT_EXPENSE_DAY_SUMMARY,
                "confidence": 0.99,
                "source": "rules_override",
            }
        # Rules / regulations / handbook queries are deterministic — never let
        # the LLM steer them into another bucket (e.g. LEAVE_REQUEST just because
        # the user typed "leave policy").
        strong_hr_policy = _strong_hr_policy(message) and is_rules_query(message)
        if strong_hr_policy:
            return {
                "intent": INTENT_HR_POLICY,
                "confidence": 0.99,
                "source": "rules_override",
            }
        # Casual chit-chat with no HR signal → force UNKNOWN so the orchestrator
        # can route to the friendly conversational fallback. Without this guard
        # the LLM tends to over-classify greetings into the closest HR bucket.
        chitchat = _looks_like_chitchat(message)
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
                    if is_expense_entitlement_query(message) and intent in (
                        INTENT_EXPENSE_CLAIM,
                        INTENT_EXPENSE_DAY_SUMMARY,
                    ):
                        return {
                            "intent": INTENT_HR_POLICY,
                            "confidence": 0.99,
                            "source": "rules_override_entitlement",
                        }
                    if wants_expense_spend_recap_query(message) and intent in (
                        INTENT_EXPENSE_CLAIM,
                        INTENT_UNKNOWN,
                    ):
                        from chat.services.expense_extraction import (
                            message_contains_expense_claim_lines,
                        )

                        if message_contains_expense_claim_lines(message):
                            return {
                                "intent": INTENT_EXPENSE_CLAIM,
                                "confidence": 0.99,
                                "source": "rules_override_claim_lines",
                            }
                        return {
                            "intent": INTENT_EXPENSE_DAY_SUMMARY,
                            "confidence": 0.99,
                            "source": "rules_override_recap",
                        }
                    if _strong_expense_claim(message) and intent not in (
                        INTENT_EXPENSE_CLAIM,
                        INTENT_EXPENSE_STATUS,
                    ):
                        return {
                            "intent": INTENT_EXPENSE_CLAIM,
                            "confidence": 0.99,
                            "source": "rules_override",
                        }
                    if chitchat and intent != INTENT_UNKNOWN:
                        return {
                            "intent": INTENT_UNKNOWN,
                            "confidence": 0.9,
                            "source": "rules_override_chitchat",
                        }
                    return {
                        "intent": intent,
                        "confidence": float(out.get("confidence") or 0),
                        "source": "llm",
                    }
        if chitchat:
            return {"intent": INTENT_UNKNOWN, "confidence": 0.9, "source": "rules_chitchat"}
        from chat.services.hr_query_classifier import (
            CONFIDENCE_RULES,
            HrQueryContext,
            rules_classify_hr_query,
        )

        hr_rules = rules_classify_hr_query(message, context=HrQueryContext())
        if hr_rules.maps_to_intent and hr_rules.confidence >= CONFIDENCE_RULES:
            return {
                "intent": hr_rules.maps_to_intent,
                "confidence": hr_rules.confidence,
                "source": f"hr_query_rules+{hr_rules.source}",
            }
        return {"intent": self._rule_intent(text, message), "confidence": 0.6, "source": "rules"}

    def _rule_intent(self, text: str, raw_message: str = "") -> str:
        from chat.services.leave_balance_intent import is_leave_balance_query

        # Bengali / Banglish keywords (fallback path when LLM isn't used or fails)
        if is_expense_entitlement_query(raw_message or text):
            return INTENT_HR_POLICY
        if is_leave_balance_query(raw_message or text):
            return INTENT_LEAVE_BALANCE
        from chat.services.expense.expense_total_dispute import (
            is_expense_total_check_query,
        )

        if is_expense_total_check_query(raw_message or text):
            return INTENT_EXPENSE_STATUS
        if _strong_expense_day_summary(raw_message or text):
            return INTENT_EXPENSE_DAY_SUMMARY
        from chat.services.leave.session_action_memory import wants_leave_meta_question

        if wants_leave_meta_question(raw_message or text):
            return INTENT_REQUEST_STATUS
        from chat.services.workflow_navigation import is_leave_application_message

        if is_leave_application_message(raw_message or text):
            return INTENT_LEAVE_REQUEST
        if re.search(r"(ছুটি|chuti|chhuti|holiday)", text) and re.search(
            r"(চাই|chai|lagbe|lage|dorkar|need|apply|request)", text
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
        if wants_expense_summary(raw_message or text):
            return INTENT_EXPENSE_DAY_SUMMARY
        if wants_expense_spend_recap_query(raw_message or text):
            return INTENT_EXPENSE_DAY_SUMMARY
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
        if re.search(
            r"\b(rule|rules|regulation|regulations|policy|policies|handbook|"
            r"hr\s*rule|guideline|guidelines)\b",
            text,
        ):
            return INTENT_HR_POLICY
        if re.search(r"\b(escalat|escalate)\b", text.lower()):
            return INTENT_APPROVAL_ESCALATION
        if re.search(r"\b(manager|supervisor)\b", text.lower()) and re.search(
            r"\b(not approved|still pending|too long|slow)\b", text.lower()
        ):
            return INTENT_APPROVAL_ESCALATION
        from chat.services.leave_meta_queries import wants_cancel_leave_command

        if wants_cancel_leave_command(raw_message or text):
            return INTENT_LEAVE_REQUEST
        from chat.services.leave_meta_queries import wants_leave_session_summary

        if wants_leave_session_summary(raw_message or text):
            return INTENT_LEAVE_REQUEST
        from chat.services.leave_meta_queries import (
            wants_leave_submission_status,
            wants_submitted_leave_details,
        )

        if wants_leave_submission_status(raw_message or text) or wants_submitted_leave_details(
            raw_message or text
        ):
            return INTENT_REQUEST_STATUS
        if re.search(
            r"\b(leave|pto|vacation|time off|sick day|day off|holiday)\b", text
        ):
            if re.search(
                r"\b(request|apply|book|need|take|chai|lagbe|lage)\b", text
            ):
                return INTENT_LEAVE_REQUEST
            return INTENT_LEAVE_BALANCE
        return INTENT_UNKNOWN
