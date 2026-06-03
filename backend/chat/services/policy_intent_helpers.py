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
    Decline when the message is clearly not HR-assistant work (unless a wizard
    is collecting slots and the user asked a side question).
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
                return False
        except Exception:
            pass
    if not _QUESTION_SHAPE_RE.search(raw):
        return False
    words = re.findall(r"\S+", raw)
    if len(words) < 3:
        return False
    return True


_OUT_OF_SCOPE_BN: tuple[str, ...] = (
    (
        "এ ধরনের বিষয়ে আমি সাহায্য করতে পারি না — এটা আমার কাজের বাইরে "
        "(যেমন আকাশে ওড়া, সাধারণ তারিখ, আবহাওয়া, বিনোদন)।\n\n"
        "আমি শুধু **কোম্পানির HR** নিয়ে কাজ করি: ছুটি আবেদন ও ব্যালান্স, খরচ, "
        "attendance, আর **আপলোড করা পলিসি/নিয়ম**।\n\n"
        "ছুটি বা পলিসি জানতে চাইলে বিষয়টা স্পষ্ট লিখুন — যেমন \"Leave Policy\" "
        "বা \"ঈদ ছুটি কত দিন\"।"
    ),
    (
        "বুঝতে পারছি, কিন্তু এই প্রশ্নটা আমার এলাকার না — ট্রাভেল, তাড়াতাড়ি তারিখ, "
        "আবহাওয়া বা এ ধরনের সাধারণ বিষয়ে আমি উত্তর দিই না।\n\n"
        "আমার কাজ **অফিসের HR**: leave, expense, attendance, আর কোম্পানির "
        "**নিয়ম-পলিসি** (আপনার আপলোড করা ডকুমেন্ট থেকে)।\n\n"
        "HR-সংক্রান্ত কিছু লাগলে পলিসির নাম বা টপিক লিখে জিজ্ঞাসা করুন।"
    ),
    (
        "এটা দুঃখিত, এইটা আমি হ্যান্ডেল করি না — এটা HR বটের স্কোপের বাইরে।\n\n"
        "আমি যেখানে সাহায্য করি: **ছুটি**, **খরচ**, **attendance**, এবং "
        "**কোম্পানির পলিসি** (যেমন ছুটির নীতি, আবেদনের নিয়ম)।\n\n"
        "পলিসি সম্পর্কে জানতে চাইলে নাম বা বিষয় উল্লেখ করে আবার লিখুন।"
    ),
    (
        "আপনার প্রশ্নটা মজার, তবে এটা আমার দায়িত্বের বাইরে — আমি সাধারণ জ্ঞান "
        "বা ব্যক্তিগত পরামর্শ দিই না।\n\n"
        "আমি **কোম্পানির কর্মী HR সহকারী**: ছুটি, খরচ, উপস্থিতি, uploaded policy — "
        "এইগুলোতেই ফোকাস।\n\n"
        "ঈদ ছুটি কত দিন বা Leave Policy এর মতো বিষয় হলে স্পষ্ট করে জিজ্ঞাসা করুন।"
    ),
)

_OUT_OF_SCOPE_EN: tuple[str, ...] = (
    (
        "I can't really help with that one — it's outside what I'm set up for "
        "(travel tips, general trivia, weather, entertainment, and the like).\n\n"
        "I'm here for **company HR**: leave requests and balance, expenses, "
        "attendance, and **your uploaded policies**.\n\n"
        "If it's about leave rules or a policy, ask with the **policy name or topic** "
        '(e.g. "Leave Policy" or "how many Eid leave days").'
    ),
    (
        "Good question, but not one I can answer here — I'm not a general assistant.\n\n"
        "My lane is **HR at your company**: leave, expenses, attendance, and "
        "official **policy documents** you've uploaded.\n\n"
        "For policy topics, mention the policy name or subject so I can look it up."
    ),
    (
        "That's a bit outside my scope — I don't handle personal travel or general "
        "knowledge questions.\n\n"
        "What I do cover: **leave**, **expenses**, **attendance**, and **company "
        "HR policies** from your knowledge base.\n\n"
        "Try asking with a clear policy title or HR topic if you need company rules."
    ),
    (
        "I'm not able to help with that — it isn't something our HR assistant covers.\n\n"
        "I focus on **workplace HR**: applying for leave, checking balance, expenses, "
        "attendance, and summarizing **uploaded policies**.\n\n"
        'Examples that work: "Leave Policy" or "Eid leave — how many days?"'
    ),
)

_OUT_OF_SCOPE_PARAPHRASE_SYSTEM = """You rephrase an HR assistant's polite decline message.

RULES
- Keep the EXACT same boundaries: cannot answer the user's off-topic question; only company HR (leave, expense, attendance, uploaded policies).
- Same language as REPLY_LANGUAGE (Bangla script for bn, English for en).
- 2–3 short paragraphs, warm human colleague tone — not robotic, not preachy.
- Do NOT add new facts, examples beyond leave/policy, or bullet lists.
- Do NOT mention being an AI or bot.
- Output ONLY the rephrased message text."""


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


def _maybe_paraphrase_out_of_scope(
    base: str,
    *,
    user_message: str,
    user_lang: str,
    trace_id: str | None,
) -> str:
    if not trace_id:
        return base
    try:
        from chat.services.llm_client import LLMClient

        client = LLMClient()
        if not client.is_configured():
            return base
        lang_line = (
            "REPLY_LANGUAGE: Bangla (Bengali script)."
            if user_lang == "bn"
            else "REPLY_LANGUAGE: English."
        )
        out = client.chat_text(
            system_prompt=_OUT_OF_SCOPE_PARAPHRASE_SYSTEM,
            user_prompt=(
                f"{lang_line}\n\n"
                f"User asked (off-topic):\n{user_message}\n\n"
                f"REFERENCE decline (same meaning, rephrase naturally):\n{base}"
            ),
            trace_id=trace_id,
        )
        cleaned = (out or "").strip()
        if len(cleaned) >= 80:
            return cleaned
    except Exception:
        pass
    return base


def build_out_of_scope_message(
    message: str,
    *,
    lang: str | None = None,
    context_lines: list[str] | None = None,
    trace_id: str | None = None,
) -> str:
    """Professional decline for general-knowledge / out-of-handbook questions."""
    from chat.services.translator import detect_user_language

    user_lang = lang or detect_user_language(message)
    pool = _OUT_OF_SCOPE_BN if user_lang == "bn" else _OUT_OF_SCOPE_EN
    base = _pick_out_of_scope_variant(pool, context_lines)
    return _maybe_paraphrase_out_of_scope(
        base,
        user_message=message,
        user_lang=user_lang,
        trace_id=trace_id,
    )


def is_leave_wizard_misroute_complaint(message: str) -> bool:
    """User says the bot treated a general question as leave-form input."""
    if not message:
        return False
    return bool(_LEAVE_WIZARD_MISROUTE_COMPLAINT_RE.search(message))
