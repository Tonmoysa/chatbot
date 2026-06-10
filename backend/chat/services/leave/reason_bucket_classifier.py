"""
Sick vs non-sick leave bucket — rules first, LLM fallback for typos / ambiguity.

Drives doctor-document requirements and leave_type reconciliation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from chat.services.leave.turn_schema import CONFIDENCE_LLM_FALLBACK
from chat.services.leave_draft_utils import (
    canonicalize_leave_reason,
    reason_indicates_non_sick_leave,
)
from chat.services.llm_client import LLMClient

logger = logging.getLogger("hr_chatbot")

_BUCKET_LLM_SYSTEM = """You classify a leave request reason for HR workflow.

Return STRICT JSON only:
{
  "bucket": "sick" | "other",
  "normalized_reason": string or null,
  "confidence": 0.0 to 1.0,
  "clarify_question_bn": string or null
}

RULES
- sick: illness, fever, injury, medical, hospital, doctor — may need doctor note (3+ days)
- other: family program/event, travel, wedding, funeral, personal — NO doctor note
- Fix typos in normalized_reason (fmly progrm → family program)
- clarify_question_bn: short Bangla question ONLY if truly unclear (medical vs personal)
- If family/personal event even with typos → bucket "other"
- If clearly medical/ill even with typos → bucket "sick"
"""


@dataclass
class LeaveBucketResult:
    bucket: str = "other"
    confidence: float = 0.0
    source: str = "rules"
    normalized_reason: str = ""
    clarify_question_bn: str = ""


def clear_leave_bucket_cache(draft: dict[str, Any]) -> None:
    draft.pop("_leave_bucket", None)
    draft.pop("_leave_bucket_confidence", None)


def _rules_bucket(draft: dict[str, Any]) -> LeaveBucketResult:
    reason = canonicalize_leave_reason(str(draft.get("reason") or ""))
    lt = str(draft.get("leave_type") or "").strip().lower()

    if reason_indicates_non_sick_leave(reason):
        return LeaveBucketResult(
            bucket="other",
            confidence=0.95,
            source="rules",
            normalized_reason=reason,
        )

    try:
        from chat.services.leave.normalization import text_has_sick_signal
    except ImportError:
        text_has_sick_signal = lambda _x: False  # noqa: E731

    if reason and text_has_sick_signal(reason):
        return LeaveBucketResult(
            bucket="sick",
            confidence=0.9,
            source="rules",
            normalized_reason=reason,
        )

    non_sick_types = {
        "casual",
        "annual",
        "maternity",
        "paternity",
        "bereavement",
        "compensatory",
        "emergency",
        "wedding",
        "travel",
    }
    sick_types = {"sick", "medical", "health"}

    if lt in non_sick_types:
        return LeaveBucketResult(bucket="other", confidence=0.85, source="rules")

    if lt in sick_types:
        if draft.get("_reason_implied"):
            return LeaveBucketResult(bucket="sick", confidence=0.88, source="rules")
        if reason and not reason_indicates_non_sick_leave(reason):
            # Stale sick type after a reason change regex did not recognize.
            return LeaveBucketResult(
                bucket="sick",
                confidence=0.55,
                source="rules_low",
                normalized_reason=reason,
            )
        return LeaveBucketResult(bucket="sick", confidence=0.8, source="rules")

    reason_l = reason.lower()
    if any(w in reason_l for w in ("sick", "ill", "fever", "medical", "doctor", "অসুস্থ")):
        return LeaveBucketResult(bucket="sick", confidence=0.75, source="rules")

    if reason and len(reason) >= 4:
        return LeaveBucketResult(
            bucket="other",
            confidence=0.55,
            source="rules_low",
            normalized_reason=reason,
        )

    return LeaveBucketResult(bucket="other", confidence=0.5, source="rules_default")


def _llm_bucket(
    draft: dict[str, Any],
    *,
    message: str,
    trace_id: str,
    llm: LLMClient | None = None,
) -> LeaveBucketResult | None:
    client = llm or LLMClient()
    if not client.is_configured():
        return None

    user_prompt = (
        f"Draft reason: {draft.get('reason') or '(empty)'}\n"
        f"Draft leave_type: {draft.get('leave_type') or '?'}\n"
        f"Days: {draft.get('days') or '?'}\n"
        f"Dates: {draft.get('start_date') or '?'} → {draft.get('end_date') or '?'}\n\n"
        f"Latest user message:\n{(message or '').strip()}\n\n"
        "Return JSON only."
    )
    out = client.chat_json(
        system_prompt=_BUCKET_LLM_SYSTEM,
        user_prompt=user_prompt,
        trace_id=trace_id or "leave-bucket-llm",
    )
    if not isinstance(out, dict):
        return None

    bucket = str(out.get("bucket") or "").strip().lower()
    if bucket not in ("sick", "other"):
        return None

    confidence = float(out.get("confidence") or 0.0)
    if confidence < CONFIDENCE_LLM_FALLBACK:
        return None

    norm = canonicalize_leave_reason(str(out.get("normalized_reason") or "").strip())
    clarify = str(out.get("clarify_question_bn") or "").strip()

    logger.info(
        "leave_bucket_llm trace_id=%s bucket=%s confidence=%.2f",
        trace_id,
        bucket,
        confidence,
    )
    return LeaveBucketResult(
        bucket=bucket,
        confidence=confidence,
        source="llm",
        normalized_reason=norm,
        clarify_question_bn=clarify,
    )


def _apply_bucket_to_draft(draft: dict[str, Any], result: LeaveBucketResult) -> None:
    if result.normalized_reason and len(result.normalized_reason) >= 3:
        draft["reason"] = result.normalized_reason[:2000]

    if result.bucket == "other":
        lt = str(draft.get("leave_type") or "").lower()
        if lt in ("sick", "medical", "health"):
            from chat.services.leave_draft_utils import invalidate_leave_type_for_reselect

            invalidate_leave_type_for_reselect(draft)
        else:
            draft.pop("_reason_implied", None)
            from chat.services.leave_draft_utils import clear_supporting_document_if_unneeded

            clear_supporting_document_if_unneeded(draft)
    elif result.bucket == "sick":
        from chat.services.leave.normalization import infer_leave_type_from_text

        if not draft.get("leave_type"):
            inferred = infer_leave_type_from_text(str(draft.get("reason") or ""))
            if inferred == "sick":
                draft["leave_type"] = inferred

    draft["_leave_bucket"] = result.bucket
    draft["_leave_bucket_confidence"] = result.confidence

    if result.clarify_question_bn and result.confidence < 0.82:
        draft["_pending_reason_clarify"] = result.clarify_question_bn
    else:
        draft.pop("_pending_reason_clarify", None)


def classify_leave_bucket(
    draft: dict[str, Any],
    *,
    message: str = "",
    trace_id: str = "",
    use_llm: bool = True,
) -> LeaveBucketResult:
    """Rules-first bucket; LLM when confidence is low or signals conflict."""
    cached = draft.get("_leave_bucket")
    cached_conf = float(draft.get("_leave_bucket_confidence") or 0.0)
    if cached in ("sick", "other") and cached_conf >= CONFIDENCE_LLM_FALLBACK:
        return LeaveBucketResult(
            bucket=str(cached),
            confidence=cached_conf,
            source="cache",
        )

    rules = _rules_bucket(draft)
    needs_llm = rules.confidence < CONFIDENCE_LLM_FALLBACK or rules.source == "rules_low"

    if use_llm and needs_llm and trace_id:
        llm = _llm_bucket(draft, message=message, trace_id=trace_id)
        if llm:
            _apply_bucket_to_draft(draft, llm)
            return llm

    _apply_bucket_to_draft(draft, rules)
    return rules


def apply_leave_semantic_reconcile(
    draft: dict[str, Any],
    *,
    message: str = "",
    trace_id: str = "",
    use_llm: bool = True,
) -> LeaveBucketResult:
    """Reconcile leave_type + bucket after reason normalization."""
    from chat.services.leave_draft_utils import reconcile_leave_type_from_reason

    reconcile_leave_type_from_reason(draft)
    result = classify_leave_bucket(
        draft,
        message=message,
        trace_id=trace_id,
        use_llm=use_llm,
    )
    from chat.services.leave_draft_utils import clear_supporting_document_if_unneeded

    clear_supporting_document_if_unneeded(draft)
    return result
