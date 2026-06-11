"""
Confidence-based merge policy for parser (regex) and LLM entity layers.

Parser wins for structured high-confidence fields (dates, payment, scope).
LLM fills semantic gaps (reason, leave_type) when parser confidence is low.
"""

from __future__ import annotations

from typing import Any

from chat.services.leave_slot_extraction import LeaveSlotExtraction, SlotValue, _set

# Fields where regex/parser is authoritative when confidence is high.
PARSER_PRIORITY_FIELDS: frozenset[str] = frozenset(
    {
        "start_date",
        "end_date",
        "days",
        "leave_payment_category",
        "day_scope",
    }
)

# Fields where LLM semantic understanding is preferred when parser is weak.
SEMANTIC_FIELDS: frozenset[str] = frozenset(
    {
        "reason",
        "leave_type",
        "description",
    }
)


def _slot_source(sv: SlotValue) -> str:
    if sv.confidence != "high" or sv.value is None:
        return ""
    return str(sv.source or "parser")


def _sanitize_llm_entity_overlay(
    ext: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    """Drop ungrounded LLM reason/leave_type before parser merge."""
    from chat.services.leave.reason_value import strip_ungrounded_reason
    from chat.services.leave_slot_extraction import explicit_leave_type_from_message

    out = strip_ungrounded_reason(dict(ext or {}), message)
    lt = str(out.get("leave_type") or "").strip().lower()
    if not lt:
        return out
    explicit = explicit_leave_type_from_message(message)
    if explicit:
        out["leave_type"] = explicit
        return out
    from chat.services.leave.normalization import text_has_sick_signal

    if lt == "sick" and text_has_sick_signal(message):
        return out
    out.pop("leave_type", None)
    return out


def merge_parser_and_llm(
    parser: LeaveSlotExtraction,
    llm_entities: dict[str, Any],
    *,
    message: str = "",
) -> tuple[LeaveSlotExtraction, dict[str, str]]:
    """
    Overlay LLM entities onto parser extraction following documented priority.

    Returns merged extraction and per-field source map for tracing.
    """
    sources: dict[str, str] = {}
    from chat.services.leave.normalization import strip_ungrounded_leave_dates

    ext = _sanitize_llm_entity_overlay(
        strip_ungrounded_leave_dates(dict(llm_entities or {}), message),
        message,
    )

    # Do not let LLM override parser-high payment/scope.
    if parser.leave_payment_category.confidence == "high":
        ext.pop("leave_payment_category", None)
    if parser.day_scope.confidence == "high":
        ext.pop("day_scope", None)
    if parser.start_date.confidence == "high":
        ext.pop("start_date", None)
        ext.pop("date", None)
    if parser.end_date.confidence == "high":
        ext.pop("end_date", None)
    if parser.reason.confidence == "high":
        parser_reason_src = str(parser.reason.source or "")
        # Let LLM replace generic implied reasons with the user's own words.
        if not parser_reason_src.startswith("implied_"):
            ext.pop("reason", None)
            ext.pop("description", None)

    def _merge_slot(name: str, key: str | None = None) -> None:
        key = key or name
        v = ext.get(key)
        if v is None or v == "":
            return
        sv: SlotValue = getattr(parser, name)
        if name in PARSER_PRIORITY_FIELDS and sv.confidence == "high":
            return
        if sv.confidence == "high" and name not in SEMANTIC_FIELDS:
            return
        _set(sv, v, confidence="high", source="llm_entities")
        sources[name] = "llm_entities"

    _merge_slot("leave_type")
    _merge_slot("start_date")
    _merge_slot("end_date")
    if ext.get("date") and parser.start_date.confidence != "high":
        _set(
            parser.start_date,
            str(ext["date"]).split("T")[0],
            confidence="high",
            source="llm_date",
        )
        sources["start_date"] = "llm_date"
    _merge_slot("days")
    _merge_slot("reason")
    if ext.get("description") and parser.reason.confidence != "high":
        _set(parser.reason, ext["description"], confidence="high", source="llm_description")
        sources["reason"] = "llm_description"

    pay = ext.get("leave_payment_category")
    if pay and parser.leave_payment_category.confidence != "high":
        from chat.services.leave.normalization import (
            message_explicitly_states_payment_category,
        )

        if not message_explicitly_states_payment_category(message):
            pay = None
    if pay and parser.leave_payment_category.confidence != "high":
        p = str(pay).lower()
        if p in {"paid", "pto", "annual", "casual"}:
            parser.leave_payment_category.value = "paid"
            parser.leave_payment_category.confidence = "high"
            parser.leave_payment_category.source = "llm_entities"
            sources["leave_payment_category"] = "llm_entities"
        elif p in {"lwop", "unpaid"}:
            parser.leave_payment_category.value = "lwop"
            parser.leave_payment_category.confidence = "high"
            parser.leave_payment_category.source = "llm_entities"
            sources["leave_payment_category"] = "llm_entities"

    scope = ext.get("day_scope")
    if scope and parser.day_scope.confidence != "high":
        from chat.services.leave.normalization import message_explicitly_states_day_scope

        if message_explicitly_states_day_scope(message):
            s = str(scope).lower()
            if s in {"half", "half_day", "half-day"}:
                parser.day_scope.value = "half"
                parser.day_scope.confidence = "high"
                parser.day_scope.source = "llm_entities"
                sources["day_scope"] = "llm_entities"
            elif s in {"full", "full_day", "full-day"}:
                parser.day_scope.value = "full"
                parser.day_scope.confidence = "high"
                parser.day_scope.source = "llm_entities"
                sources["day_scope"] = "llm_entities"

    for name in (
        "leave_type",
        "start_date",
        "end_date",
        "days",
        "leave_payment_category",
        "day_scope",
        "reason",
    ):
        if name not in sources:
            src = _slot_source(getattr(parser, name))
            if src:
                sources[name] = src

    return parser, sources


def overlay_llm_semantic_fields(
    extraction: LeaveSlotExtraction,
    llm_entities: dict[str, Any],
    message: str,
    *,
    llm_used: bool,
) -> dict[str, str]:
    """
    LLM-first overlay for reason and leave_type (semantic fields).

    Parser regex reason is replaced when LLM returns a grounded reason.
    """
    sources: dict[str, str] = {}
    if not llm_used:
        return sources

    from chat.services.leave.reason_value import (
        extract_reason_value,
        is_boilerplate_leave_reason,
        reason_grounded_in_message,
    )

    reason = str(
        llm_entities.get("reason") or llm_entities.get("description") or ""
    ).strip()
    if reason and is_boilerplate_leave_reason(reason):
        reason = str(extract_reason_value(message) or "").strip()
    if reason and not reason_grounded_in_message(reason, message):
        reason = str(extract_reason_value(message) or "").strip()
    if (
        reason
        and len(reason) >= 3
        and not is_boilerplate_leave_reason(reason)
        and reason_grounded_in_message(reason, message)
    ):
        _set(extraction.reason, reason[:2000], confidence="high", source="llm_primary")
        sources["reason"] = "llm_primary"

    lt = str(llm_entities.get("leave_type") or "").strip().lower()
    if not lt:
        return sources

    from chat.services.leave.normalization import text_has_sick_signal
    from chat.services.leave_draft_utils import reason_indicates_non_sick_leave
    from chat.services.leave_slot_extraction import explicit_leave_type_from_message

    explicit = explicit_leave_type_from_message(message)
    if explicit:
        extraction.leave_type.value = explicit
        extraction.leave_type.confidence = "high"
        extraction.leave_type.source = "llm_primary"
        sources["leave_type"] = "llm_primary"
    elif reason_indicates_non_sick_leave(reason) or reason_indicates_non_sick_leave(message):
        pass
    elif lt == "sick" and (
        text_has_sick_signal(message) or text_has_sick_signal(reason)
    ):
        extraction.leave_type.value = "sick"
        extraction.leave_type.confidence = "high"
        extraction.leave_type.source = "llm_primary"
        sources["leave_type"] = "llm_primary"

    return sources


def extraction_to_entities(extraction: LeaveSlotExtraction) -> dict[str, Any]:
    """Flatten high-confidence LeaveSlotExtraction slots to an entities dict."""
    out: dict[str, Any] = {}
    for name in (
        "leave_type",
        "start_date",
        "end_date",
        "days",
        "leave_payment_category",
        "day_scope",
        "reason",
    ):
        sv: SlotValue = getattr(extraction, name)
        if sv.confidence == "high" and sv.value is not None:
            out[name] = sv.value
    if out.get("start_date") and not out.get("date"):
        out["date"] = out["start_date"]
    return out
