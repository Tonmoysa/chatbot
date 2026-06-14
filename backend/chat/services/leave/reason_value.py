"""
Extract leave reason *values* from natural Bangla/Banglish (including edit wrappers).

Used when the user embeds the real reason inside instructions like
``reason ta hobe amar ashole pet betha``.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.leave_slot_extraction import (
    _HEALTH_REASON_RE,
    _REASON_BN_BOLE_RE,
    _REASON_BN_JONNO_RE,
    _REASON_BN_TAI_INLINE_RE,
    _REASON_BN_TAI_RE,
    _REASON_CAUSE_RE,
    _REASON_FOR_RE,
    _REASON_KEYWORD_RE,
    _SIDE_QUESTION_IN_REASON_RE,
    _LEAVE_INTENT_RE,
    _normalize_reason_token,
    _trim_reason_fragment,
)

_REASON_TA_HOBE_RE = re.compile(
    r"(?:reason|reas[oi]n|karon|কারণ|kar[oa]n)\s+ta\s+"
    r"(?:hobe|habe|hoy|হবে|হয়)\s+"
    r"(?:(?:amar|my|me)\s+)?(?:ashole|actually|asole|ashol[e]?)\s+(.+)",
    re.I | re.UNICODE,
)

_REASON_EDIT_ASHOLE_RE = re.compile(
    r"(?:ashole|actually|asole|ashol[e]?)\s+(.+?)(?:\s*$|\s*\.{2,})",
    re.I | re.UNICODE,
)

_REASON_EDIT_WRAPPER_RE = re.compile(
    r"(?:reason|reas[oi]n|karon|কারণ).{0,55}"
    r"(?:change|update|kor[eo]|badlao|ঠিক|poriborto|substitute)"
    r".{0,55}"
    r"(?:hobe|habe|hoy|হবে)?\s*(?:(?:amar|my)\s+)?(?:ashole|actually|asole)?\s*(.+)",
    re.I | re.UNICODE,
)

_REASON_STRIP_PREFIX_RE = re.compile(
    r"^(?:"
    r"ac+ha+h?a?|ok+ay?|thik\s*ache|"
    r"(?:reason|reas[oi]n|karon|কারণ)\s+ta\s+"
    r"(?:tumi\s+)?(?:change|update|kor[eo]|badlao|ঠিক)\s*(?:koro|kor|dao|daw)?"
    r")\s*"
    r"(?:\.{2,}\s*)?",
    re.I | re.UNICODE,
)

_REASON_NEGATION_SWAP_BN_RE = re.compile(
    r"(.+?)\s+(?:হবে\s*না|হয়\s*না|হবেনা|হয়না)\s+(.+?)\s+(?:হবে|হয়|হবেন)",
    re.UNICODE,
)
_REASON_NEGATION_SWAP_EN_RE = re.compile(
    r"(.+?)\s+(?:hobe\s*na|habe\s*na|na|not)\s+(.+?)\s+(?:hobe|habe|will\s*be)",
    re.I | re.UNICODE,
)
_FAMILY_REASON_RE = re.compile(
    r"(?:"
    r"famil+y(?:\s+problem|\s+program|\s+issue)?|"
    r"ফ্যামিলি\s*প্রবলেম|পরিবার(?:\s*সমস্যা|\s*প্রবলেম)?|"
    r"family\s+problem|family\s+program|family\s+issue"
    r")",
    re.I | re.UNICODE,
)

_COMPOUND_LEAVE_APPLICATION_RE = re.compile(
    r"(?:"
    r"apply\s+korte|chacchi|chacci|chuti\s+lagbe|leave\s+lagbe|leave\s+apply|"
    r"leave\s+chai|chuti\s+chai|leave\s+nite|chuti\s+nite|"
    r"ekta\s+leave|"
    r"\d+\s*(?:din|diner|days?|দিন)\s+jonno"
    r")",
    re.I | re.UNICODE,
)

# Strip leave-request boilerplate accidentally captured by causal ``tai``/``bole`` regexes.
_LEAVE_REQUEST_PREFIX_RE = re.compile(
    r"^(?:"
    r"(?:(?:amar|ami|my|me)\s+)?"
    r"(?:ajke|aj\s*ke|today|kal|kalke|kalker|agamikal|agami\s*kal|tomorrow)\s+"
    r")?"
    r"(?:"
    r"(?:leave|chuti|chhuti|chhutti|ছুটি)\s*(?:lagbe|nite|chai|chacchi|chacci|apply)?"
    r"|"
    r"(?:lagbe|nite|chai)\s+(?:leave|chuti|chhuti)"
    r")\s*",
    re.I | re.UNICODE,
)

_DATE_OR_MONTH_IN_REASON_RE = re.compile(
    r"\b(?:"
    r"\d{1,2}(?:st|nd|rd|th)?|agami|tomorrow|kal|kalke|"
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec|"
    r"জানু|ফেব|মার্চ|এপ্রিল|মে|জুন|জুলাই|আগস্ট|সেপ্ট|অক্ট|নভে|ডিস"
    r")\b",
    re.I | re.UNICODE,
)

_REAL_LEAVE_CAUSE_RE = re.compile(
    r"(?:"
    r"famil|family|wedding|travel|tour|program|problem|osusto|sick|fever|medical|doctor|"
    r"অসুস্থ|পরিবার|marriage|funeral|emergency|personal|waz|matha|pet|ghur"
    r")",
    re.I | re.UNICODE,
)

_BARE_SLOT_LABEL_RE = re.compile(
    r"^(?:"
    r"reason|why|cause|kar[oa]n|karon|কারণ|"
    r"tarikh|tarik|date|dates|din|day|days|"
    r"paid|unpaid|payment|salary|lwop|select\s*leave|"
    r"full|half|scope|duration|"
    r"type|leave\s*type|sick|casual|annual|anual"
    r")$",
    re.I | re.UNICODE,
)

_WIZARD_SLOT_PHRASE_RE = re.compile(
    r"^(?:"
    r"(?:anual|anul|anuall?|annual|sick|casual|unpaid|lwop|medical)(?:\s+leave)?|"
    r"leave\s+without\s+pay|without\s+pay|"
    r"(?:full|half)(?:\s+day)?|"
    r"পুরো\s*দিন|হাফ\s*দিন"
    r")$",
    re.I | re.UNICODE,
)


def looks_like_wizard_slot_label(text: str) -> bool:
    """True when text is a wizard slot token — never a leave reason."""
    raw = (text or "").strip()
    if not raw:
        return False
    if _BARE_SLOT_LABEL_RE.match(raw) or _WIZARD_SLOT_PHRASE_RE.match(raw):
        return True
    # Compound application sentences may mention sick/unpaid — not slot labels.
    if len(raw.split()) > 5:
        return False
    # Short health/cause phrases are reasons (e.g. ``onek osusto``), not Select Leave.
    if looks_like_health_leave_reason(raw) and len(raw.split()) <= 4:
        return False
    try:
        from chat.services.leave.normalization import looks_like_wizard_leave_type_answer

        if looks_like_wizard_leave_type_answer(raw):
            return True
    except Exception:
        pass
    return False


def _clean_reason_candidate(text: str) -> str:
    s = (text or "").strip(" ,.-…")
    s = _REASON_STRIP_PREFIX_RE.sub("", s).strip()
    s = re.sub(
        r"^(?:reason|reas[oi]n|karon|কারণ|kar[oa]n)\s*[:,-]?\s*",
        "",
        s,
        flags=re.I,
    ).strip()
    if len(s) < 3:
        return ""
    return s[:2000]


def _normalize_extracted_reason(reason: str) -> str:
    """Normalize rule-extracted tokens; direct slot answers keep user spelling."""
    s = (reason or "").strip()
    if re.fullmatch(r"famil+y", s, re.I):
        return "family"
    return s


_BOILERPLATE_REASON_RE = re.compile(
    r"(?:"
    r"^amar\s+jonno$|^tumi\s+amar|"
    r"apply\s+kore|apply\s+korte|leave\s+apply|leave\s+nite|chuti\s+nite|chuti\s+lagbe|"
    r"ekta\s+leave|ekti\s+leave|"
    r"^jonno$|^for\s+me$|^for\s+my\b"
    r")",
    re.I | re.UNICODE,
)


def _health_reason_short_form(raw: str) -> str | None:
    """Normalize sickness / health tokens from a longer utterance (R02)."""
    text = (raw or "").strip()
    if not text:
        return None
    hm = _HEALTH_REASON_RE.search(text)
    if hm:
        token = hm.group(1).strip()
        if re.search(r"osusto|oshustho|অসুস্থ", token, re.I):
            return "onek osusto" if re.search(r"onek", token, re.I) else "অসুস্থতা"
        return token[:2000]
    if re.search(r"\bonek\s+osusto\b", text, re.I):
        return "onek osusto"
    if re.search(r"\b(osusto|oshustho)\b", text, re.I):
        return "অসুস্থতা"
    if re.search(r"\bsick\b", text, re.I):
        return "sick leave / অসুস্থতা"
    return None


def looks_like_health_leave_reason(message: str) -> bool:
    """Predicate: message carries a health/sickness signal usable as leave reason."""
    return _health_reason_short_form(message or "") is not None


def is_boilerplate_leave_reason(reason: str) -> bool:
    """True when a reason string is instruction filler, not a real cause."""
    raw = (reason or "").strip()
    if not raw:
        return True
    if looks_like_wizard_slot_label(raw):
        return True
    low = raw.lower()
    if _BOILERPLATE_REASON_RE.search(low):
        return True
    try:
        from chat.services.workflow_navigation import is_leave_application_message

        if is_leave_application_message(raw):
            return True
    except Exception:
        pass
    if len(raw) < 20 and _COMPOUND_LEAVE_APPLICATION_RE.search(raw):
        return True
    if len(raw) >= 20 and _COMPOUND_LEAVE_APPLICATION_RE.search(raw):
        return True
    # Mixed leave-application phrasing + health token — not a clean reason value.
    if _LEAVE_INTENT_RE.search(raw) and _HEALTH_REASON_RE.search(raw):
        return True
    if _LEAVE_INTENT_RE.search(raw) and len(raw.split()) > 8:
        return True
    if (
        _LEAVE_INTENT_RE.search(raw)
        and _DATE_OR_MONTH_IN_REASON_RE.search(raw)
        and not _REAL_LEAVE_CAUSE_RE.search(raw)
    ):
        return True
    return False


def _sanitize_causal_reason_capture(capture: str, full_message: str) -> str | None:
    """Clean ``tai``/``bole``/``for`` captures; fall back to health token when mixed."""
    reason = _clean_reason_candidate(_trim_reason_fragment(capture))
    reason = _LEAVE_REQUEST_PREFIX_RE.sub("", reason).strip()
    if len(reason) < 3:
        reason = ""
    if reason and not is_boilerplate_leave_reason(reason):
        return _normalize_extracted_reason(reason)
    health = _health_reason_short_form(full_message)
    if health:
        return health
    return None


def reason_grounded_in_message(reason: str, message: str) -> bool:
    """True when the reason text is supported by the user's message (no LLM invention)."""
    from chat.services.leave_draft_utils import canonicalize_leave_reason

    norm = canonicalize_leave_reason((reason or "").strip())
    msg = (message or "").strip()
    if not norm or not msg:
        return False
    msg_l = msg.lower()
    reason_l = norm.lower()
    if reason_l in msg_l:
        return True
    tokens = [
        t
        for t in re.findall(r"[a-zA-Z\u0980-\u09FF]+", reason_l)
        if len(t) >= 3
    ]
    if not tokens:
        return False
    return all(t in msg_l for t in tokens)


def strip_ungrounded_reason(
    entities: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    """Drop LLM-invented or boilerplate reasons not supported by the user message."""
    if not entities:
        return entities
    reason = str(entities.get("reason") or entities.get("description") or "").strip()
    if not reason:
        return entities
    if is_boilerplate_leave_reason(reason) or not reason_grounded_in_message(
        reason, message
    ):
        out = dict(entities)
        out.pop("reason", None)
        out.pop("description", None)
        return out
    return entities


def extract_compound_review_reason(message: str) -> str | None:
    """
    Pull a short sickness/cause reason from a compound leave-application sentence.

    Used at review when the user re-states the whole request (duration + sick + paid)
    instead of a bare reason phrase.
    """
    raw = (message or "").strip()
    if len(raw) < 8:
        return None
    if not _COMPOUND_LEAVE_APPLICATION_RE.search(raw) and not re.search(
        r"\b\d+\s*(?:din|diner|days?|দিন)\b", raw, re.I
    ):
        return None

    health = _health_reason_short_form(raw)
    if health:
        return health

    m = _REASON_KEYWORD_RE.search(raw)
    if m:
        return _normalize_extracted_reason(m.group(1).strip())

    return None


_REASON_HOBE_SLOT_RE = re.compile(
    r"^(.+?)\s+(?:hobe|habe|hoy|হবে|হয়)\s+(?:reason|reas[oi]n|karon|কারণ)\s*$",
    re.I | re.UNICODE,
)

_REASON_TA_HOBE_SIMPLE_RE = re.compile(
    r"^(?:reason|reas[oi]n|reaon|karon|কারণ|kar[oa]n)\s+ta\s+"
    r"(.+?)\s+(?:hobe|habe|hoy|হবে|হয়)\s*$",
    re.I | re.UNICODE,
)

_REASON_TA_HOBE_NOT_SICK_RE = re.compile(
    r"(?:reason|reas[oi]n|reaon|karon|কারণ|kar[oa]n)\s+ta\s+"
    r"(.+?)\s+hobe\s+"
    r"(?:osusto|oshustho|sick|medical|ill|অসুস্থ)"
    r".{0,24}?"
    r"(?:nah|na|noy|not|না)\b",
    re.I | re.UNICODE,
)

_REASON_X_HOBE_NOT_SICK_RE = re.compile(
    r"^(.+?)\s+hobe\s+"
    r"(?:osusto|oshustho|sick|medical|ill|অসুস্থ)"
    r".{0,24}?"
    r"(?:nah|na|noy|not|না)\b",
    re.I | re.UNICODE,
)

_REASON_INSTRUCTION_WRAPPER_RE = re.compile(
    r"(?:"
    r"(?:reason|reas[oi]n|reaon|karon|কারণ)\s+ta|"
    r"\b(?:hobe|habe|hoy|হবে|হয়)\b.*\b(?:nah|na|noy|not|না)\b|"
    r"\b(?:nah|na|noy|not|না)\b.*\b(?:hobe|habe|hoy|হবে|হয়)\b"
    r")",
    re.I | re.UNICODE,
)


def is_reason_instruction_wrapper(reason: str) -> bool:
    """True when text is a correction instruction, not a bare cause."""
    raw = (reason or "").strip()
    if not raw or len(raw) < 6:
        return False
    if _REASON_INSTRUCTION_WRAPPER_RE.search(raw):
        return True
    words = raw.split()
    if len(words) > 5 and re.search(
        r"\b(?:reason|reas[oi]n|reaon|karon|hobe|habe|nah|na)\b", raw, re.I
    ):
        return True
    return False


def extract_reason_replacement(message: str) -> str | None:
    """
    Detect reason corrections like ``শরীর খারাপ হবে না ফ্যামিলি প্রবলেম হবে``.

    Returns the new reason text when the user negates the old one and supplies a replacement.
    """
    raw = (message or "").strip()
    if len(raw) < 4:
        return None
    m_slot = _REASON_HOBE_SLOT_RE.match(raw)
    if m_slot:
        candidate = _clean_reason_candidate(_trim_reason_fragment(m_slot.group(1)))
        if len(candidate) >= 3:
            return _normalize_extracted_reason(candidate)

    m_ta_hobe = _REASON_TA_HOBE_SIMPLE_RE.match(raw)
    if m_ta_hobe:
        candidate = _clean_reason_candidate(_trim_reason_fragment(m_ta_hobe.group(1)))
        if len(candidate) >= 3:
            return _normalize_extracted_reason(candidate)

    m_not_sick = _REASON_TA_HOBE_NOT_SICK_RE.search(raw)
    if m_not_sick:
        candidate = _clean_reason_candidate(_trim_reason_fragment(m_not_sick.group(1)))
        if len(candidate) >= 2:
            return _normalize_extracted_reason(candidate)

    m_x_not = _REASON_X_HOBE_NOT_SICK_RE.match(raw)
    if m_x_not:
        candidate = _clean_reason_candidate(_trim_reason_fragment(m_x_not.group(1)))
        if len(candidate) >= 2 and not re.search(
            r"\b(?:reason|reas[oi]n|reaon|karon)\s+ta\b", candidate, re.I
        ):
            return _normalize_extracted_reason(candidate)

    if len(raw) < 8:
        return None
    for pattern in (_REASON_NEGATION_SWAP_BN_RE, _REASON_NEGATION_SWAP_EN_RE):
        m = pattern.search(raw)
        if not m:
            continue
        candidate = _clean_reason_candidate(_trim_reason_fragment(m.group(2)))
        if len(candidate) >= 3:
            return _normalize_extracted_reason(candidate)
    if _FAMILY_REASON_RE.search(raw) and re.search(
        r"(?:হবে\s*না|hobe\s*na|na|not|change|poriborto|বদল|ঠিক)",
        raw,
        re.I | re.UNICODE,
    ):
        fm = _FAMILY_REASON_RE.search(raw)
        if fm:
            return _normalize_extracted_reason(fm.group(0).strip())
    return None


def extract_reason_value(message: str, *, edit_context: bool = False) -> str | None:
    """
    Pull a leave reason string from free-form or edit-wrapped user text.

    edit_context: True during review/slot edits — enables health + wrapper patterns
    without requiring a leave-intent keyword in the message.
    """
    raw = (message or "").strip()
    if len(raw) < 3:
        return None
    try:
        from chat.services.workflow_navigation import is_leave_navigation_phrase

        if is_leave_navigation_phrase(raw):
            return None
    except Exception:
        pass
    try:
        from chat.services.leave.date_correction import looks_like_date_only_message

        if looks_like_date_only_message(raw):
            return None
    except Exception:
        pass
    if _BARE_SLOT_LABEL_RE.match(raw) or looks_like_wizard_slot_label(raw):
        return None
    if _SIDE_QUESTION_IN_REASON_RE.search(raw):
        return None
    if re.match(r"^(paid|unpaid|lwop|full|half)\b", raw, re.I) and len(raw.split()) <= 3:
        return None

    replacement = extract_reason_replacement(raw)
    if replacement:
        return replacement

    if _LEAVE_INTENT_RE.search(raw) or _COMPOUND_LEAVE_APPLICATION_RE.search(raw):
        compound = extract_compound_review_reason(raw)
        if compound:
            return compound

    if edit_context:
        for pattern in (
            _REASON_TA_HOBE_RE,
            _REASON_EDIT_WRAPPER_RE,
        ):
            m = pattern.search(raw)
            if m:
                reason = _clean_reason_candidate(_trim_reason_fragment(m.group(1)))
                if len(reason) >= 3:
                    return _normalize_extracted_reason(reason)

        health = _health_reason_short_form(raw)
        if health:
            return health

        m = _REASON_EDIT_ASHOLE_RE.search(raw)
        if m:
            reason = _clean_reason_candidate(_trim_reason_fragment(m.group(1)))
            if len(reason) >= 3:
                return _normalize_extracted_reason(reason)

    # R02 — health signal beats causal ``tai``/``bole`` when leave intent is present.
    if _LEAVE_INTENT_RE.search(raw):
        health = _health_reason_short_form(raw)
        if health:
            return health

    for pattern in (
        _REASON_FOR_RE,
        _REASON_CAUSE_RE,
        _REASON_BN_JONNO_RE,
        _REASON_BN_TAI_INLINE_RE,
        _REASON_BN_TAI_RE,
        _REASON_BN_BOLE_RE,
    ):
        m = pattern.search(raw)
        if m:
            reason = _sanitize_causal_reason_capture(m.group(1), raw)
            if reason:
                return reason

    if _LEAVE_INTENT_RE.search(raw):
        health = _health_reason_short_form(raw)
        if health:
            return health

    m = _REASON_KEYWORD_RE.search(raw)
    if m:
        return _normalize_extracted_reason(m.group(1).strip()[:2000])

    if _LEAVE_INTENT_RE.search(raw) and re.search(r"\bfamil+y\b", raw, re.I):
        m_fam = re.search(r"\bfamil+y\b", raw, re.I)
        if m_fam:
            return _normalize_reason_token(m_fam.group(0))

    if edit_context:
        if _COMPOUND_LEAVE_APPLICATION_RE.search(raw):
            return None

        cleaned = _clean_reason_candidate(raw)
        if len(cleaned) >= 4 and not re.search(
            r"\b(change|update|kor[eo]|badlao|ঠিক|tumi|you)\b", cleaned, re.I
        ):
            return _normalize_extracted_reason(cleaned)

    if len(raw.split()) <= 8 and not _LEAVE_INTENT_RE.search(raw):
        if not re.search(
            r"\b(tomorrow|today|kal|agamikal|unpaid|paid|lwop|full|half|change|update)\b",
            raw,
            re.I,
        ):
            cleaned = _clean_reason_candidate(raw) or ""
            if cleaned and not is_reason_instruction_wrapper(cleaned):
                return _normalize_extracted_reason(cleaned)

    return None
