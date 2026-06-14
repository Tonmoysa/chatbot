"""
Map high-confidence UtteranceResolution → SessionTurnDecision (Tier U02).

Routing decisions stay in session_turn_router; this module only exports predicates
and mapping helpers — no side effects.
"""

from __future__ import annotations

from typing import Any

from chat.services.turn_understanding.schemas import (
    ACT_QUERY_POLICY,
    ACT_QUERY_STATUS,
    ACT_SUMMARY,
    UtteranceResolution,
)

U02_MIN_CONFIDENCE = 0.82


def utterance_maps_to_router_decision(utterance: UtteranceResolution) -> bool:
    if utterance.needs_clarify:
        return False
    if not utterance.in_scope:
        return False
    if float(utterance.confidence or 0) < U02_MIN_CONFIDENCE:
        return False
    return utterance.primary_act in (
        ACT_QUERY_POLICY,
        ACT_QUERY_STATUS,
        ACT_SUMMARY,
    )


def utterance_router_act(utterance: UtteranceResolution) -> str | None:
    """Return TurnKind value string for U02, or None to fall through to P-rules."""
    if not utterance_maps_to_router_decision(utterance):
        return None
    act = str(utterance.primary_act or "")
    if act == ACT_QUERY_POLICY:
        return "policy_query"
    if act == ACT_QUERY_STATUS:
        return "balance_query"
    if act == ACT_SUMMARY and (utterance.domain or "") == "expense":
        return "summary"
    return None
