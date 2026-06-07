"""Lightweight policy / rules topic detection for routing (no static handbook).

`rules_handbook.py` is kept in the repo for reference only; orchestrator and RAG
must not import it for answers. This module duplicates only the regex heuristics
needed so `IntentDetector` and `ChatOrchestrator` can recognize policy-shaped
messages without pulling in handbook data.
"""

from __future__ import annotations

import random
import re

_RULES_QUERY_PATTERNS = (
    r"\b(rule|rules|regulation|regulations|policy|policies|handbook|guideline|guidelines)\b",
    r"\b(allowed|prohibited|must|mustn't|forbidden|mandatory|required|may\s+not)\b",
)

_BENGALI_RULES_HINT = (
    r"(নিয়ম|বিধি|নীতি|হ্যান্ডবুক|রুলস|পলিসি)",
    r"\b(niyom|niyam|bidhi|niti|rules?|policy|policies|handbook)\b",
)


def is_rules_query(message: str) -> bool:
    """True if the message is about rules / regulations / handbook topics."""
    if not message:
        return False
    if is_policy_handbook_complaint(message):
        return False
    low = message.lower()
    for pat in _RULES_QUERY_PATTERNS:
        if re.search(pat, low):
            return True
    for pat in _BENGALI_RULES_HINT:
        if re.search(pat, message) or re.search(pat, low):
            return True
    return False


_EXPENSE_SUBMIT_RE = re.compile(
    r"\b(submit|submitted|log(?:ged)?|spent|hoyeche|hoyese|claim|reimburse)\b",
    re.I,
)
_EXPENSE_SPEND_DOMAIN_RE = re.compile(
    r"\b(expense|kharcha|khoroch|খরচ|reimbursement)\b",
    re.I,
)
_AMOUNT_RE = re.compile(r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?(?!\d)")


def is_expense_entitlement_query(message: str) -> bool:
    """
    User asks about daily allowance / TA-DA / per-day limits (policy lookup),
    not logging or summarizing actual spend.
    """
    if not message:
        return False
    low = message.lower()
    raw = message or ""
    if _EXPENSE_SUBMIT_RE.search(low) and (
        _EXPENSE_SPEND_DOMAIN_RE.search(low) or re.search(r"খরচ", raw)
    ):
        return False
    if _AMOUNT_RE.search(message) and re.search(
        r"\b(cost|kharcha|khoroch|hoyeche|taka|টাকা)\b", low
    ):
        return False

    if re.search(
        r"\b(allowance|allowances|travel\s+allowance|dearness\s+allowance|daily\s+allowance)\b",
        low,
    ):
        if re.search(r"\b(daily|per\s*day|each\s*day|protidin)\b", low) or re.search(
            r"\bkoto\b", low
        ):
            return True
        if re.search(r"\b(amar|my|entitled|rate|limit|cap)\b", low) or re.search(
            r"(কত|ভাতা)", raw
        ):
            return True

    if re.search(r"\b(ta\s*/\s*da|t\s*/\s*d|tada|ta\s+da)\b", low):
        return True

    if re.search(r"(দৈনিক\s*ভাতা|ভাতা\s*কত|টিএ|ডিএ|টিএ\s*/\s*ডিএ)", raw, re.I):
        return True

    if (
        re.search(r"\b(amar|my)\b", low)
        and re.search(r"\b(allowance|ta|da)\b", low)
        and re.search(r"\bkoto\b", low)
    ):
        return True

    if re.search(r"\b(per\s*day|protidin|protiti\s*din)\b", low) and re.search(
        r"\b(koto|limit|cap|rate|allowance|ta|da)\b", low
    ):
        if _EXPENSE_SPEND_DOMAIN_RE.search(low) or re.search(r"খরচ", raw):
            return False
        return True

    if re.search(r"\b(daily\s+budget|budget\s+koto|daily\s+cap)\b", low) or re.search(
        r"(দৈনিক\s*বাজেট|বাজেট\s*কত)", raw, re.I
    ):
        return True

    if re.search(r"\b(expense|reimbursement|reimburse)\b", low) and (
        is_rules_query(message)
        or re.search(r"\b(budget|cap|limit)\b", low)
        or re.search(r"(বাজেট|সীমা|নিয়ম)", raw)
    ):
        if _EXPENSE_SUBMIT_RE.search(low) and _AMOUNT_RE.search(message):
            return False
        return True

    return False


_BAD_ANSWER_COMPLAINT_RE = re.compile(
    r"(relation\s*nai|related\s*na|relevant\s*na|not\s*related|no\s*relation|"
    r"wrong\s*answer|hallucinat|manasse\s*nai|"
    r"প্রাসঙ্গিক\s*না|সম্পর্ক\s*নেই|মিল\s*নেই|ভুল\s*উত্তর|এই\s*উত্তর|"
    r"ei\s*ans|amar\s*question.{0,40}(sathe|satha).{0,20}(nai|ney|na))",
    re.I | re.UNICODE,
)

_POLICY_HANDBOOK_COMPLAINT_RE = re.compile(
    r"(?:"
    r"policy\s*te\s*nai|policies?\s*te\s*nai|rules?\s*te\s*nai|"
    r"পলিসি\s*তে\s*না[ইে]|নীতি\s*তে\s*না[ইে]|"
    r"ei\s*dhoroner|এই\s*ধরনের|ei\s*besoy|এই\s*বিষয়|ei\s*bishoy|"
    r"kivabe\s*pele|কিভাবে\s*পেল|pele\s*keno|"
    r"tahole\s*tumi|তাহলে\s*তুমি|eta\s*kivabe|এটা\s*কিভাবে"
    r")",
    re.I | re.UNICODE,
)

_CALENDAR_QUESTION_RE = re.compile(
    r"(?:"
    r"\b(?:when\s+is|what\s+(?:day|date)\s+is|which\s+day\s+is)\b|"
    r"\b(?:kobe|kon\s*din|ki\s*din|ki\s*dibosh|ki\s*disbosh|ki\s*dibos)\b|"
    r"(?:কবে|কী\s*দিন|কি\s*দিন|কী\s*দিবস|কি\s*দিবস|কোন\s*দিবস|কোন\s*দিন)"
    r")",
    re.I | re.UNICODE,
)

_FESTIVAL_OR_OCCASION_RE = re.compile(
    r"(?:"
    r"\beid\b|ঈদ|bijoy|বিজয়|victory\s+day|independence|স্বাধীন|"
    r"26\s*march|২৬\s*মার্চ|durga|puja|pujo|পূজা|"
    r"\bdibosh\b|\bdebosh\b|\bdisbosh\b|দিবস|"
    r"divali|diwali|christmas|xmas|boro\s*din|বড়\s*দিন|pohela|নববর্ষ"
    r")",
    re.I | re.UNICODE,
)

_MONTH_NAME_RE = re.compile(
    r"\b(?:"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?|"
    r"জানুয়ারি|ফেব্রুয়ারি|মার্চ|এপ্রিল|মে|জুন|জুলাই|আগস্ট|সেপ্টেম্বর|অক্টোবর|নভেম্বর|ডিসেম্বর"
    r")\b",
    re.I | re.UNICODE,
)

_ORDINAL_DATE_RE = re.compile(
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|"
    r"apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.I,
)

_WHEN_WORD_RE = re.compile(r"(?:\bkobe\b|কবে|\bwhen\b)", re.I)

_QUESTION_SHAPE_RE = re.compile(
    r"(?:"
    r"\?\s*$|"
    r"^\s*(?:what|when|where|why|how|who|which|can|could|is|are|do|does)\b|"
    r"\b(?:ki|kobe|keno|kothay|kemon|kon)\b|"
    r"(?:কী|কি|কেন|কোথায়|কখন|কিভাবে|কোন)"
    r")",
    re.I | re.UNICODE,
)

_HR_IN_SCOPE_RE = re.compile(
    r"\b("
    r"leave|chuti|chhuti|holiday|pto|vacation|time\s*off|"
    r"expense|reimburs|claim|cost|kharcha|khoroch|taka|money|"
    r"salary|beton|payroll|overtime|"
    r"attendance|clock|punch|timesheet|"
    r"policy|policies|rule|rules|regulation|regulations|handbook|guideline|"
    r"wfh|remote|work\s+from\s+home|"
    r"status|track|request|ticket|reference|ref|"
    r"manager|supervisor|hr|approval|escalat|"
    r"sick|maternity|paternity|emergency|"
    r"dress\s*code|ppe|safety|"
    r"document|receipt|invoice|bill|"
    r"allowance|tada|ta\s*/\s*da|budget|cap|limit"
    r")\b",
    re.I,
)

_HR_IN_SCOPE_BN_RE = re.compile(
    r"(ছুটি|খরচ|বেতন|নিয়ম|বিধি|পলিসি|হ্যান্ডবুক|উপস্থিতি|"
    r"টাকা|রিইম্বার্স|মেডিকেল|অসুস্থ|অনুমোদন|ম্যানেজার|ভাতা|বাজেট)",
    re.UNICODE,
)

_PURE_CHITCHAT_RE = re.compile(
    r"^\s*("
    r"hi|hello|hey|hola|sup|yo|salam|thanks?|thank\s*you|bye|ok|okay|"
    r"হ্যালো|হাই|ধন্যবাদ|কেমন\s*আছ"
    r")\s*[!.?,…]*\s*$",
    re.I | re.UNICODE,
)

_COMPANY_POLICY_SCOPE_RE = re.compile(
    r"(?:"
    r"\b(?:policy|policies|rules?|regulation|handbook|niyom|niti|bidhi)\b|"
    r"(?:নিয়ম|বিধি|নীতি|পলিসি|হ্যান্ডবুক)|"
    r"(?:ছুটি|chuti|chhuti|leave).{0,40}(?:policy|niyom|niti|allowed|grant|apply|company|কোম্পানি)|"
    r"(?:policy|niyom|niti|company|কোম্পানি).{0,40}(?:ছুটি|chuti|chhuti|leave)"
    r")",
    re.I | re.UNICODE,
)

_LEAVE_WIZARD_MISROUTE_COMPLAINT_RE = re.compile(
    r"(?:"
    r"(?:ami\s+)?(?:ekta\s+)?question\s*korchi|প্রশ্ন\s*কর(?:ছি|েছিলাম)?|"
    r"(?:but|kintu|তবে).{0,100}(?:chuti|chhuti|ছুটি).{0,50}"
    r"(?:besoy|বিষয়|বেপার|ফর্ম|abedon|জানাই|janai).{0,40}(?:keno|কেন|why)|"
    r"(?:chuti|chhuti|ছুটি).{0,50}(?:besoy|বিষয়|বেপার|ফর্ম|abedon).{0,40}(?:keno|কেন)|"
    r"janai\s*dila\s*keno|জানাই\s*দিল.{0,20}কেন"
    r")",
    re.I | re.UNICODE,
)


def is_irrelevant_answer_complaint(message: str) -> bool:
    """User says the bot's previous reply did not match their question."""
    if not message:
        return False
    if _BAD_ANSWER_COMPLAINT_RE.search(message):
        return True
    return bool(_POLICY_HANDBOOK_COMPLAINT_RE.search(message))


def is_policy_handbook_complaint(message: str) -> bool:
    """User says the bot cited policy content that is not in their handbook."""
    if not message:
        return False
    return bool(_POLICY_HANDBOOK_COMPLAINT_RE.search(message))


def is_company_policy_about_occasion(message: str) -> bool:
    """Festival/date mention is tied to company leave or policy (in scope)."""
    if not message:
        return False
    if is_rules_query(message) or is_expense_entitlement_query(message):
        return True
    raw = message or ""
    if not _FESTIVAL_OR_OCCASION_RE.search(raw):
        return False
    return bool(_COMPANY_POLICY_SCOPE_RE.search(raw))


def is_hr_assistant_in_scope(message: str) -> bool:
    """True when the message clearly belongs to leave / expense / attendance / company policy."""
    if not message:
        return False
    raw = (message or "").strip()
    low = raw.lower()
    if is_rules_query(raw) or is_expense_entitlement_query(raw):
        return True
    if _HR_IN_SCOPE_RE.search(low) or _HR_IN_SCOPE_BN_RE.search(raw):
        return True
    if re.search(
        r"(ছুটি|chuti|chhuti).{0,40}(চাই|lagbe|lage|apply|request|নিতে|লাগবে)",
        low,
    ) or re.search(r"\b(apply|request)\s+(for\s+)?(a\s+)?leave\b", low):
        return True
    if re.search(r"(ছুটি|chuti|chhuti)", low) and re.search(
        r"(কত|koto|baki|remaining|balance)", raw, re.I
    ):
        return True
    try:
        from knowledge_base.services.sanitization import extract_policy_title_phrases

        if extract_policy_title_phrases(raw):
            return True
    except Exception:
        pass
    return False


def is_policy_kb_query(message: str) -> bool:
    """True only when uploaded-policy RAG is appropriate (not general trivia)."""
    if not message or is_policy_handbook_complaint(message):
        return False
    if is_rules_query(message) or is_expense_entitlement_query(message):
        return True
    try:
        from knowledge_base.services.sanitization import extract_policy_title_phrases

        if extract_policy_title_phrases(message):
            return True
    except Exception:
        pass
    low = (message or "").lower()
    if re.search(
        r"\b(?:company|office|employer|hr|কোম্পানি|অফিস)\b.{0,40}"
        r"\b(?:policy|policies|rules?|niyom|niti)\b",
        low,
    ):
        return True
    if re.search(
        r"\b(?:policy|policies|rules?|niyom|niti)\b.{0,40}"
        r"\b(?:company|office|employer|hr|কোম্পানি)\b",
        low,
    ):
        return True
    return False


def is_general_knowledge_out_of_scope(message: str) -> bool:
    """
    Calendar / national-holiday trivia (e.g. \"eid kobe\", \"25th december eta ki din\").
    Not company HR policy — the assistant should decline professionally.
    """
    if not message or is_company_policy_about_occasion(message):
        return False
    raw = (message or "").strip()
    low = raw.lower()
    festival = bool(_FESTIVAL_OR_OCCASION_RE.search(raw))
    calendar_q = bool(_CALENDAR_QUESTION_RE.search(raw) or _CALENDAR_QUESTION_RE.search(low))
    has_month = bool(_MONTH_NAME_RE.search(raw) or _MONTH_NAME_RE.search(low))
    has_ordinal = bool(_ORDINAL_DATE_RE.search(low))
    named_date = bool(
        has_month
        or has_ordinal
        or re.search(
            r"\b\d{1,2}\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|march)\b",
            low,
        )
        or re.search(r"(?:২৬\s*মার্চ|26\s*march)", raw, re.I)
    )
    if calendar_q and (festival or named_date):
        return True
    if has_ordinal and re.search(r"(?:eta\s*)?ki\s*din|what\s+day", low):
        return True
    if festival and _WHEN_WORD_RE.search(raw):
        return True
    words = re.findall(r"\S+", raw)
    if len(words) <= 6 and festival and _WHEN_WORD_RE.search(raw):
        return True
    return False


def is_off_topic_for_hr_assistant(
    message: str,
    *,
    wizard_active: bool = False,
) -> bool:
    """
    Dynamic out-of-scope: not every off-topic phrase is listed statically.
    Decline when the message is clearly not HR-assistant work.
    During an active wizard, only HR-relevant side questions stay in scope
    (general trivia / GK is still declined).
    """
    if not message or is_hr_assistant_in_scope(message):
        return False
    if is_general_knowledge_out_of_scope(message):
        return True
    raw = (message or "").strip()
    if _PURE_CHITCHAT_RE.match(raw):
        return False
    if wizard_active:
        try:
            from chat.services.intent_detector import looks_like_wizard_side_question

            if looks_like_wizard_side_question(message):
                if (
                    is_rules_query(message)
                    or is_expense_entitlement_query(message)
                    or is_hr_assistant_in_scope(message)
                ):
                    return False
        except Exception:
            pass
    if not _QUESTION_SHAPE_RE.search(raw):
        return False
    words = re.findall(r"\S+", raw)
    return len(words) >= 2


_OUT_OF_SCOPE_BN: tuple[str, ...] = (
    (
        "এ ধরনের সাধারণ প্রশ্ন (তারিখ, আবহাওয়া, trivia) **কোম্পানি HR**-এর বাইরে — "
        "আমি ছুটি, খরচ, attendance ও **আপলোড করা পলিসি** নিয়ে কাজ করি।\n"
        "পলিসি চাইলে বিষয় লিখুন (যেমন: Leave Policy)।"
    ),
    (
        "বুঝতে পারছি — তবে এটা আমার স্কোপের বাইরে; আমি **অফিস HR সহকারী** "
        "(leave, expense, attendance, company policy)।\n"
        "HR বিষয় হলে পলিসির নাম বা টপিক স্পষ্ট করে জিজ্ঞাসা করুন।"
    ),
    (
        "দুঃখিত, এই প্রশ্নের উত্তর দিতে পারব না — general knowledge আমি cover করি না।\n"
        "আমি **কোম্পানির HR**-এ ফোকাস করি; পলিসি জানতে চাইলে নাম/বিষয় লিখুন।"
    ),
    (
        "এটি HR বটের কাজ নয় — আমি শুধু **leave, expense, attendance** "
        "আর **uploaded policy** নিয়ে সাহায্য করি।\n"
        "ঈদ **কত দিন ছুটি** বা Leave Policy এর মতো বিষয় হলে স্পষ্ট করে লিখুন।"
    ),
)

_OUT_OF_SCOPE_EN: tuple[str, ...] = (
    (
        "That's general knowledge — outside **company HR** (leave, expenses, "
        "attendance, uploaded policies).\n"
        "For HR rules, ask with a **policy name or topic** (e.g. Leave Policy)."
    ),
    (
        "I can't help with that one — I'm your **workplace HR assistant**, not a general chatbot.\n"
        "Try leave, expenses, attendance, or a named **company policy**."
    ),
    (
        "Good question, but not in my scope — no travel trivia, weather, or calendar dates.\n"
        "I cover **HR at your company**; mention the policy title if you need rules."
    ),
    (
        "I'm not set up for that — only **leave, expenses, attendance**, and "
        "**uploaded HR policies**.\n"
        'Examples: "Leave Policy" or "how many Eid leave days".'
    ),
)

def _last_assistant_text(context_lines: list[str] | None) -> str:
    for line in reversed(context_lines or []):
        if line.startswith("Assistant:"):
            return line[len("Assistant:") :].strip()
    return ""


def _pick_out_of_scope_variant(
    pool: tuple[str, ...],
    context_lines: list[str] | None,
) -> str:
    """Rotate wording; avoid repeating the same template back-to-back in a thread."""
    last = _last_assistant_text(context_lines)
    if last:
        candidates = [v for v in pool if v not in last and last not in v]
        if candidates:
            return random.choice(candidates)
    return random.choice(pool)


def build_out_of_scope_message(
    message: str,
    *,
    lang: str | None = None,
    context_lines: list[str] | None = None,
    trace_id: str | None = None,
) -> str:
    """Professional decline for general-knowledge / out-of-handbook questions."""
    from chat.services.message_polish_llm import polish_template_message
    from chat.services.translator import detect_user_language

    user_lang = lang or detect_user_language(message)
    pool = _OUT_OF_SCOPE_BN if user_lang == "bn" else _OUT_OF_SCOPE_EN
    base = _pick_out_of_scope_variant(pool, context_lines)
    return polish_template_message(
        base,
        user_message=message,
        message_type="out_of_scope",
        trace_id=trace_id,
        user_lang=user_lang,
        min_length=50,
    )


def is_leave_wizard_misroute_complaint(message: str) -> bool:
    """User says the bot treated a general question as leave-form input."""
    if not message:
        return False
    return bool(_LEAVE_WIZARD_MISROUTE_COMPLAINT_RE.search(message))
