"""
Mid-wizard leave reason corrections — rules first, LLM for typos / Banglish.

Users often correct reason on any step (payment, dates) with misspellings
that regex cannot cover reliably.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from chat.services.leave.reason_value import (
    extract_reason_replacement,
    extract_reason_value,
    is_reason_instruction_wrapper,
)
from chat.services.leave.turn_schema import CONFIDENCE_LLM_FALLBACK
from chat.services.leave.reason_bucket_classifier import clear_leave_bucket_cache
from chat.services.leave_draft_utils import (
    canonicalize_leave_reason,
    reason_indicates_non_sick_leave,
)
from chat.services.leave_slot_extraction import is_payment_only_message
from chat.services.llm_client import LLMClient

logger = logging.getLogger("hr_chatbot")

_REASON_CORRECTION_HINT_RE = re.compile(
    r"(?:"
    r"reason|reas[oi]n|karon|কারণ|"
    r"hobe\s+(?:reason|reas[oi]n|karon|কারণ)|"
    r"(?:reason|reas[oi]n|karon|কারণ).{0,24}(?:hobe|habe|hoy|হবে|হয়)|"
    r"famil|family|fmly|fmli|poribar|পরিবার|"
    r"progrm|program|programme|প্রোগ্রাম|"
    r"wedding|travel|tour|trip|osusto|oshustho|sick|medical|"
    r"\b(?:nah|na|not)\b|না|"
    r"change|badl|poriborto|ঠিক"
    r")",
    re.I | re.UNICODE,
)

_SLOT_ONLY_RE = re.compile(
    r"^(?:paid|unpaid|lwop|full|half|full\s*day|half\s*day|"
    r"agamikal|agamkal|kalke|tomorrow|today|skip|yes|no)$",
    re.I | re.UNICODE,
)

_REASON_LLM_SYSTEM = """You extract or correct a leave REASON from the user's message.

Return STRICT JSON only:
{
  "reason": string or null,
  "confidence": 0.0 to 1.0
}

RULES
- reason: ONLY the cause (e.g. "family program", "onek osusto", "travel", "tour")
- NOT the full application sentence; strip "reason ta", "hobe", "nah", negation wrappers
- Negation corrections: "reason ta tour hobe osusto nah" → "tour" or "travel"
- "reaon ta tour hobe sick na" → "tour" (fix reason/reaon typo)
- Fix Banglish typos: fmly progrm → family program
- If the message is only paid/unpaid/full/half/date — return reason null
- If no leave reason intent, return reason null
"""


@dataclass
class ReasonCorrectionResult:
    reason: str = ""
    source: str = "none"
    confidence: float = 0.0


def _rules_reason_confident(reason: str) -> bool:
    """True when regex normalization clearly classified sick vs non-sick."""
    if not reason or len(reason) < 2:
        return False
    if is_reason_instruction_wrapper(reason):
        return False
    if reason_indicates_non_sick_leave(reason):
        return True
    try:
        from chat.services.leave.normalization import text_has_sick_signal

        if text_has_sick_signal(reason):
            return True
    except ImportError:
        pass
    if reason.startswith("অসুস্থতা"):
        return True
    return False


def _rules_reason_correction(message: str) -> ReasonCorrectionResult | None:
    text = (message or "").strip()
    if len(text) < 3:
        return None
    if _SLOT_ONLY_RE.match(text) or is_payment_only_message(text):
        return None

    raw = extract_reason_replacement(text) or extract_reason_value(text)
    if not raw or len(raw.strip()) < 3:
        return None

    reason = canonicalize_leave_reason(raw)
    if len(reason) < 3:
        return None
    if not _rules_reason_confident(reason) and not _rules_reason_confident(raw):
        return None
    return ReasonCorrectionResult(reason=reason, source="rules", confidence=0.92)


def _llm_reason_correction(
    message: str,
    *,
    draft: dict[str, Any],
    trace_id: str,
    llm: LLMClient | None = None,
) -> ReasonCorrectionResult | None:
    client = llm or LLMClient()
    if not client.is_configured():
        return None

    user_prompt = (
        f"Current draft reason: {draft.get('reason') or '(empty)'}\n"
        f"Current leave_type: {draft.get('leave_type') or '?'}\n\n"
        f"User message:\n{(message or '').strip()}\n\n"
        "Return JSON only."
    )
    out = client.chat_json(
        system_prompt=_REASON_LLM_SYSTEM,
        user_prompt=user_prompt,
        trace_id=trace_id or "leave-reason-correction-llm",
    )
    if not isinstance(out, dict):
        return None

    reason = canonicalize_leave_reason(str(out.get("reason") or "").strip())
    confidence = float(out.get("confidence") or 0.0)
    if not reason or len(reason) < 3 or confidence < CONFIDENCE_LLM_FALLBACK:
        return None

    logger.info(
        "leave_reason_correction_llm trace_id=%s reason=%r",
        trace_id,
        reason[:80],
    )
    return ReasonCorrectionResult(
        reason=reason,
        source="llm",
        confidence=confidence,
    )


def looks_like_reason_correction(message: str) -> bool:
    text = (message or "").strip()
    if len(text) < 4 or _SLOT_ONLY_RE.match(text) or is_payment_only_message(text):
        return False
    from chat.services.leave.normalization import parse_day_scope_answer
    from chat.services.leave_draft_utils import is_supporting_document_skip_message

    if is_supporting_document_skip_message(text):
        return False
    if parse_day_scope_answer(text):
        return False
    return bool(_REASON_CORRECTION_HINT_RE.search(text))


def try_apply_reason_correction(
    draft: dict[str, Any],
    message: str,
    *,
    trace_id: str = "",
    use_llm: bool = True,
) -> bool:
    """
    Apply a reason correction from any wizard step.

    Rules first; LLM when rules miss but the message looks like a reason update.
    """
    text = (message or "").strip()
    if not text:
        return False

    from chat.services.leave.normalization import parse_day_scope_answer

    if parse_day_scope_answer(text):
        return False

    rules = _rules_reason_correction(text)
    if rules:
        draft["reason"] = rules.reason[:2000]
        draft.pop("_reason_implied", None)
        draft.pop("_pending_reason_clarify", None)
        clear_leave_bucket_cache(draft)
        from chat.services.leave_draft_utils import reconcile_leave_type_from_reason

        reconcile_leave_type_from_reason(draft)
        return True

    if not use_llm or not looks_like_reason_correction(text):
        return False

    llm = _llm_reason_correction(text, draft=draft, trace_id=trace_id)
    if not llm:
        return False

    draft["reason"] = llm.reason[:2000]
    draft.pop("_reason_implied", None)
    draft.pop("_pending_reason_clarify", None)
    clear_leave_bucket_cache(draft)
    from chat.services.leave_draft_utils import reconcile_leave_type_from_reason

    reconcile_leave_type_from_reason(draft)
    return True
