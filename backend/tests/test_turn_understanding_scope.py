"""Turn Understanding — HR scope vs OOS for long messages (Tier U)."""

from __future__ import annotations

from chat.constants import INTENT_HR_POLICY, INTENT_LEAVE_BALANCE
from chat.services.session_snapshot import build_session_snapshot
from chat.services.session_turn_router import TurnKind, route_session_turn
from chat.services.turn_understanding import classify_message_scope, resolve_utterance
from chat.services.turn_understanding.schemas import (
    ACT_NEEDS_CLARIFY,
    ACT_OUT_OF_SCOPE,
    ACT_QUERY_POLICY,
    ACT_QUERY_STATUS,
)


def _snap(message: str, wf: dict | None = None):
    return build_session_snapshot(message, workflow_state=wf or {})


def test_long_cricket_message_out_of_scope():
    msg = (
        "ami ajke onek bored feel korchi ar brazil vs argentina match ta "
        "kemon cholche seta bolo ar match er score ki ekhon"
    )
    scope = classify_message_scope(msg, _snap(msg))
    assert scope.in_scope is False
    assert scope.confidence >= 0.85

    utterance = resolve_utterance(msg, _snap(msg))
    assert utterance.primary_act == ACT_OUT_OF_SCOPE
    assert utterance.in_scope is False

    decision = route_session_turn(_snap(msg), utterance=utterance)
    assert decision.turn_kind == TurnKind.OUT_OF_SCOPE
    assert decision.reason == "U00_out_of_scope"


def test_long_leave_expense_policy_mixed_clarifies():
    msg = (
        "amar kalke family program e jabo tai full day chuti lagbe paid, "
        "ar ajke lunch 150 bus 80 snack 50, ar leave policy ta o bolo"
    )
    utterance = resolve_utterance(msg, _snap(msg))
    assert utterance.in_scope is True
    assert utterance.primary_act in (ACT_NEEDS_CLARIFY, ACT_QUERY_POLICY)


def test_long_policy_only_routes_u02():
    msg = (
        "amake detail e bolo amader company er leave policy ki ki rules ache "
        "paid unpaid chuti niye ar advance notice koto din lagbe"
    )
    snap = _snap(msg)
    utterance = resolve_utterance(msg, snap)
    assert utterance.in_scope is True
    assert utterance.primary_act == ACT_QUERY_POLICY

    decision = route_session_turn(snap, utterance=utterance)
    assert decision.turn_kind == TurnKind.POLICY_QUERY
    assert decision.intent == INTENT_HR_POLICY
    assert decision.reason == "U02_policy_from_utterance"


def test_long_balance_routes_u02():
    msg = "amar annual leave ar casual leave er balance koto ache ekhon amar kache"
    snap = _snap(msg)
    utterance = resolve_utterance(msg, snap)
    assert utterance.in_scope is True
    assert utterance.primary_act == ACT_QUERY_STATUS

    decision = route_session_turn(snap, utterance=utterance)
    assert decision.turn_kind == TurnKind.BALANCE_QUERY
    assert decision.intent == INTENT_LEAVE_BALANCE
    assert decision.reason == "U02_balance_from_utterance"


def test_long_expense_claim_in_scope_not_oos():
    msg = (
        "ajke office theke baire meeting chilo tai ajke amar lunch 200 taka "
        "hoyeche snack 50 taka ar bus 80 taka — egulo expense e add koro"
    )
    utterance = resolve_utterance(msg, _snap(msg))
    assert utterance.in_scope is True
    assert utterance.primary_act != ACT_OUT_OF_SCOPE
