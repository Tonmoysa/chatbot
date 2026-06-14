"""
Parse compound leave corrections at review (multi-day sick, reason swap, etc.).

Architecture (voice / free-form Banglish):
  1. Orchestrator entities (LeaveEntityPipeline + LLM) — semantic fields
  2. Review compound LLM — multi-field updates the pipeline may miss
  3. Rules — confirm/cancel chips, ISO dates, payment tokens, gap-fill only

Regex alone cannot cover every phrasing; LLM handles meaning, rules validate structure.
"""

from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Any

from chat.services.leave.normalization import (
    extract_leave_duration_days,
    infer_leave_type_from_text,
    message_explicitly_states_leave_date,
    message_mentions_leave_duration,
    normalize_leave_draft,
    parse_day_scope_answer,
)
from chat.services.leave.reason_value import (
    extract_compound_review_reason,
    is_boilerplate_leave_reason,
)
from chat.services.leave_slot_extraction import explicit_leave_type_from_message
from chat.services.leave.turn_schema import CONFIDENCE_LLM_FALLBACK
from chat.services.llm_client import LLMClient

logger = logging.getLogger("hr_chatbot")

_DURATION_RE = re.compile(
    r"\b(\d+)\s*(din|diner|days?|দিন)\b",
    re.I | re.UNICODE,
)

_REVIEW_COMPOUND_SIGNAL_RE = re.compile(
    r"(?:"
    r"\b\d+\s*(?:din|diner|days?|দিন)\b|"
    r"\b(sick|osusto|oshustho|অসুস্থ)\b|"
    r"\b(apply\s+korte|chacchi|chacci|chuti\s+lagbe|leave\s+apply)\b"
    r")",
    re.I | re.UNICODE,
)

_REVIEW_COMPOUND_LLM_SYSTEM = """You extract leave draft field updates from a compound review correction.

The user is editing an existing leave draft before submit. Return STRICT JSON only:
{
  "days": number or null,
  "reason": string or null,
  "leave_type": "sick" | "casual" | "annual" | null,
  "leave_payment_category": "paid" | "lwop" | null,
  "day_scope": "full" | "half" | null,
  "start_date": "YYYY-MM-DD" or null,
  "clear_dates": true | false,
  "confidence": 0.0 to 1.0
}

RULES
- reason: ONLY the cause (e.g. "onek osusto", "pet betha", "family program") — NEVER the full application sentence
- days: from "3 diner jonno", "tin din", "for 3 days" → 3
- clear_dates: true when user mentions duration/days but does NOT state which calendar date to start
- start_date: only when user clearly states a date (tomorrow/kalke/ISO/ajke with leave intent)
- Bengali/Banglish voice: osusto/অসুস্থ → sick; paid leave hobe → paid
- Ignore boilerplate: "apply korte chacchi", "ekta leave", "tumi apply kore daw"
"""


def _draft_reason_superseded(message: str, draft: dict[str, Any]) -> bool:
    """True when a new compound correction should replace the stored reason."""
    reason = str(draft.get("reason") or "").strip()
    if not reason:
        return True
    if is_boilerplate_leave_reason(reason):
        return True
    text = (message or "").strip()
    if len(text.split()) < 5:
        return False
    if reason.lower() in text.lower():
        return False
    return bool(
        re.search(
            r"\b(feel|osusto|oshustho|betha|weak|sick|medical|family|wedding|travel|"
            r"অসুস্থ|পরিবার|ব্যথা)\b",
            text,
            re.I | re.UNICODE,
        )
    )


def _draft_snapshot(draft: dict[str, Any]) -> tuple[Any, ...]:
    return (
        draft.get("leave_type"),
        draft.get("leave_payment_category"),
        draft.get("day_scope"),
        draft.get("start_date"),
        draft.get("end_date"),
        draft.get("days"),
        draft.get("reason"),
    )


def should_try_compound_review_update(message: str) -> bool:
    """Broad gate: any review-stage message that may update leave slots."""
    text = (message or "").strip()
    if len(text) < 8:
        return False
    from chat.services.leave.normalization import parse_day_scope_answer

    # Single-slot scope/payment edits use inline turn parser — not compound merge.
    if parse_day_scope_answer(text) and len(text.split()) <= 4:
        return False
    from chat.services.wizard_turn_gate import looks_like_leave_review_update

    return looks_like_leave_review_update(text) or is_review_compound_correction(text)


def is_review_compound_correction(message: str) -> bool:
    """True when a review-stage message likely updates several leave fields at once."""
    text = (message or "").strip()
    if len(text) < 12:
        return False
    if not _REVIEW_COMPOUND_SIGNAL_RE.search(text):
        return False
    signals = 0
    if _DURATION_RE.search(text):
        signals += 1
    if re.search(r"\b(sick|osusto|oshustho|অসুস্থ)\b", text, re.I | re.UNICODE):
        signals += 1
    if re.search(
        r"\b(apply|chacchi|chacci|lagbe|chuti|leave)\b",
        text,
        re.I | re.UNICODE,
    ):
        signals += 1
    if re.search(r"\b(paid|unpaid|lwop|hobe|habe)\b", text, re.I):
        signals += 1
    return signals >= 2


def _needs_review_compound_llm(
    message: str,
    entities: dict[str, Any] | None,
    draft: dict[str, Any],
) -> bool:
    """Call review LLM when free-form text is not fully covered by pipeline entities."""
    text = (message or "").strip()
    if len(text) < 15:
        return False

    ent = dict(entities or {})
    pipeline_reason = str(ent.get("reason") or ent.get("description") or "").strip()
    has_pipeline_reason = bool(pipeline_reason) and not is_boilerplate_leave_reason(
        pipeline_reason
    )
    has_pipeline_days = bool(ent.get("days"))

    if has_pipeline_reason and has_pipeline_days:
        return False

    if is_review_compound_correction(text):
        return not (has_pipeline_reason and has_pipeline_days)

    if should_try_compound_review_update(text) and len(text.split()) >= 6:
        return not has_pipeline_reason

    return False


def _apply_duration_without_date(draft: dict[str, Any], message: str) -> bool:
    """When user states N days but no start date, drop stale calendar dates."""
    text = (message or "").strip()
    if not message_mentions_leave_duration(text):
        return False
    if message_explicitly_states_leave_date(text):
        return False

    days = extract_leave_duration_days(text)
    changed = False
    if days is not None:
        draft["days"] = days
        changed = True
    if draft.pop("start_date", None) is not None:
        changed = True
    if draft.pop("end_date", None) is not None:
        changed = True
    return changed


def _overlay_pipeline_entities(
    draft: dict[str, Any],
    entities: dict[str, Any],
    message: str,
    *,
    fill_gaps_only: bool = False,
) -> bool:
    """Apply orchestrator LeaveEntityPipeline output (LLM-primary semantic layer)."""
    ent = dict(entities or {})
    if not ent:
        return False

    changed = False
    text = (message or "").strip()

    reason = str(ent.get("reason") or ent.get("description") or "").strip()
    if reason and len(reason) >= 3 and not is_boilerplate_leave_reason(reason):
        if not fill_gaps_only or not draft.get("reason"):
            draft["reason"] = reason[:2000]
            draft.pop("_reason_implied", None)
            changed = True

    days = ent.get("days")
    if days is not None:
        try:
            n = int(float(days))
            if n > 0 and (not fill_gaps_only or not draft.get("days")):
                draft["days"] = n
                changed = True
        except (TypeError, ValueError):
            pass

    lt = str(ent.get("leave_type") or "").strip().lower()
    if lt and (not fill_gaps_only or not draft.get("leave_type")):
        draft["leave_type"] = lt
        changed = True

    from chat.services.leave.normalization import (
        message_explicitly_states_payment_category,
    )

    pay = str(ent.get("leave_payment_category") or "").strip().lower()
    if (
        pay in ("paid", "lwop")
        and message_explicitly_states_payment_category(text)
        and (not fill_gaps_only or not draft.get("leave_payment_category"))
    ):
        draft["leave_payment_category"] = pay
        changed = True

    scope = str(ent.get("day_scope") or "").strip().lower()
    if scope in ("full", "half") and (not fill_gaps_only or not draft.get("day_scope")):
        draft["day_scope"] = scope
        changed = True

    if message_explicitly_states_leave_date(text):
        for key, target in (("start_date", "start_date"), ("end_date", "end_date")):
            val = ent.get(key) or ent.get("date") if key == "start_date" else ent.get(key)
            if val and (not fill_gaps_only or not draft.get(target)):
                draft[target] = str(val).split("T")[0]
                changed = True

    if _apply_duration_without_date(draft, text):
        changed = True

    return changed


def _apply_rules_compound_update(
    draft: dict[str, Any],
    message: str,
    *,
    fill_gaps_only: bool = False,
) -> bool:
    """Rules gap-fill: dates, payment chips, duration regex — not full-sentence reasons."""
    text = (message or "").strip()
    if not text:
        return False

    changed = False
    has_explicit_date = message_explicitly_states_leave_date(text)

    if not fill_gaps_only or not draft.get("days"):
        days = extract_leave_duration_days(text)
        if days is not None:
            draft["days"] = days
            changed = True
            if not has_explicit_date:
                if draft.pop("start_date", None) is not None:
                    changed = True
                if draft.pop("end_date", None) is not None:
                    changed = True

    current_reason = str(draft.get("reason") or "")
    reason_stale = not current_reason or is_boilerplate_leave_reason(current_reason)
    if not fill_gaps_only or reason_stale:
        reason = extract_compound_review_reason(text)
        if reason:
            draft["reason"] = reason[:2000]
            draft.pop("_reason_implied", None)
            changed = True

    if not fill_gaps_only or not draft.get("leave_type"):
        lt = explicit_leave_type_from_message(text) or infer_leave_type_from_text(
            text, str(draft.get("reason") or "")
        )
        if lt:
            draft["leave_type"] = lt
            changed = True

    from chat.services.leave_workflow import _infer_payment_category

    before_pay = draft.get("leave_payment_category")
    _infer_payment_category(text, draft, force=not fill_gaps_only)
    if draft.get("leave_payment_category") != before_pay:
        changed = True

    scope = parse_day_scope_answer(text)
    if scope and (
        not fill_gaps_only
        or not draft.get("day_scope")
        or scope != draft.get("day_scope")
    ):
        draft["day_scope"] = scope
        changed = True

    if has_explicit_date:
        from chat.services.leave_slot_extraction import extract_leave_slots
        from chat.services.leave_slots import prefill_draft_from_extraction

        ex = extract_leave_slots(text, skip_leave_phrase_gate=True)
        before = (draft.get("start_date"), draft.get("end_date"))
        prefill_draft_from_extraction(draft, ex, overwrite=True)
        after = (draft.get("start_date"), draft.get("end_date"))
        if before != after:
            changed = True
        days = draft.get("days")
        if days and draft.get("start_date") and not draft.get("end_date"):
            s = date.fromisoformat(str(draft["start_date"]))
            draft["end_date"] = (s + timedelta(days=int(days) - 1)).isoformat()
            changed = True

    return changed


def _apply_llm_compound_update(
    draft: dict[str, Any],
    message: str,
    *,
    trace_id: str,
    llm: LLMClient | None = None,
    merge: bool = True,
) -> bool:
    client = llm or LLMClient()
    if not client.is_configured():
        return False

    today = date.today().isoformat()
    user_prompt = (
        f"Today: {today}\n"
        f"Current draft:\n"
        f"- days: {draft.get('days') or '?'}\n"
        f"- start: {draft.get('start_date') or '?'}\n"
        f"- end: {draft.get('end_date') or '?'}\n"
        f"- reason: {draft.get('reason') or '(empty)'}\n"
        f"- payment: {draft.get('leave_payment_category') or '?'}\n"
        f"- scope: {draft.get('day_scope') or '?'}\n"
        f"- leave_type: {draft.get('leave_type') or '?'}\n\n"
        f"User correction:\n{(message or '').strip()}\n\n"
        "Return JSON only."
    )
    out = client.chat_json(
        system_prompt=_REVIEW_COMPOUND_LLM_SYSTEM,
        user_prompt=user_prompt,
        trace_id=trace_id or "leave-review-compound-llm",
    )
    if not isinstance(out, dict):
        return False
    if float(out.get("confidence") or 0.0) < CONFIDENCE_LLM_FALLBACK:
        return False

    changed = False
    if out.get("clear_dates"):
        if draft.pop("start_date", None) is not None:
            changed = True
        if draft.pop("end_date", None) is not None:
            changed = True

    for key in ("days", "reason", "leave_type", "leave_payment_category", "day_scope"):
        val = out.get(key)
        if val is None or val == "":
            continue
        if merge and draft.get(key) and key == "reason":
            if not _draft_reason_superseded(message, draft):
                continue
        if key == "reason" and is_boilerplate_leave_reason(str(val)):
            continue
        if key == "leave_payment_category" and str(val).lower() not in ("paid", "lwop"):
            continue
        if key == "day_scope" and str(val).lower() not in ("full", "half"):
            continue
        if merge and draft.get(key) and key != "reason":
            continue
        draft[key] = val
        changed = True

    start = out.get("start_date")
    if start and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(start).strip()):
        if not merge or not draft.get("start_date"):
            draft["start_date"] = str(start).strip()
            changed = True

    if changed:
        logger.info("leave_review_compound_llm trace_id=%s", trace_id)
    return changed


def try_apply_review_compound_update(
    draft: dict[str, Any],
    message: str,
    *,
    entities: dict[str, Any] | None = None,
    trace_id: str = "",
    use_llm: bool = True,
) -> bool:
    """
    Apply a multi-field review correction.

    LLM-primary for free-form voice/chat; rules validate structure and fill gaps.
    When duration is mentioned without a start date, clears stale dates so the
    wizard asks SLOT_DATES instead of reusing an old calendar day.
    """
    text = (message or "").strip()
    if not should_try_compound_review_update(text):
        return False

    from chat.services.leave.normalization import normalize_leave_draft

    normalize_leave_draft(draft)
    before = _draft_snapshot(draft)
    client = LLMClient() if use_llm else None
    llm_configured = bool(client and client.is_configured())

    if entities:
        _overlay_pipeline_entities(draft, entities, text, fill_gaps_only=False)

    if use_llm and llm_configured and _needs_review_compound_llm(text, entities, draft):
        _apply_llm_compound_update(
            draft, text, trace_id=trace_id, llm=client, merge=True
        )

    _apply_rules_compound_update(draft, text, fill_gaps_only=True)

    if message_mentions_leave_duration(text) and not message_explicitly_states_leave_date(
        text
    ):
        _apply_duration_without_date(draft, text)

    current_reason = str(draft.get("reason") or "")
    if is_boilerplate_leave_reason(current_reason):
        better = extract_compound_review_reason(text)
        if better:
            draft["reason"] = better[:2000]
            draft.pop("_reason_implied", None)
        elif pipeline_reason := str(
            (entities or {}).get("reason") or (entities or {}).get("description") or ""
        ).strip():
            if not is_boilerplate_leave_reason(pipeline_reason):
                draft["reason"] = pipeline_reason[:2000]
                draft.pop("_reason_implied", None)

    normalize_leave_draft(draft)
    return _draft_snapshot(draft) != before


def review_update_needs_date_question(draft: dict[str, Any]) -> bool:
    """True when user set a duration but we still need a calendar start date."""
    if draft.get("start_date"):
        return False
    return bool(draft.get("days")) or message_mentions_leave_duration(
        str(draft.get("_last_user_message") or "")
    )
