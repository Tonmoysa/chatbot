"""
HR scope classification — in-scope (leave / expense / policy / balance) vs out-of-scope.

Used by ``resolve_utterance`` before domain act probes. Long messages without HR
signals should not fall into wizard traps or get guessed slot answers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from chat.services.turn_understanding.rules_scope import (
    probe_balance_query,
    probe_out_of_scope,
    probe_policy_query,
)

LONG_MESSAGE_CHARS = 72

DOMAIN_LEAVE = "leave"
DOMAIN_EXPENSE = "expense"
DOMAIN_POLICY = "policy"
DOMAIN_BALANCE = "balance"


@dataclass(frozen=True)
class HrScopeResult:
    in_scope: bool
    confidence: float
    reason: str
    domains: frozenset[str] = field(default_factory=frozenset)
    oos_reason: str = ""
    source: str = "rules"


def _snapshot_wizard_active(snapshot: Any) -> bool:
    return bool(
        getattr(snapshot, "leave_active", False)
        or getattr(snapshot, "expense_active", False)
        or getattr(snapshot, "leave_domain_active", False)
        or getattr(snapshot, "expense_domain_active", False)
    )


def _probe_domain_signals(message: str) -> dict[str, float]:
    """Per-domain confidence from lightweight predicates (no routing)."""
    msg = (message or "").strip()
    scores: dict[str, float] = {}

    policy_hit, policy_conf = probe_policy_query(msg)
    if policy_hit:
        scores[DOMAIN_POLICY] = policy_conf

    balance_hit, balance_conf = probe_balance_query(msg)
    if balance_hit:
        scores[DOMAIN_BALANCE] = balance_conf

    try:
        from chat.services.workflow_navigation import is_leave_application_message

        if is_leave_application_message(msg):
            scores[DOMAIN_LEAVE] = max(scores.get(DOMAIN_LEAVE, 0.0), 0.88)
    except Exception:
        pass

    try:
        from chat.services.leave_balance_intent import is_leave_balance_query

        if is_leave_balance_query(msg):
            scores[DOMAIN_BALANCE] = max(scores.get(DOMAIN_BALANCE, 0.0), 0.92)
    except Exception:
        pass

    try:
        from chat.services.intent_detector import _strong_expense_claim
        from chat.services.expense_workflow import wants_expense_summary
        from chat.services.expense_extraction import message_contains_expense_claim_lines

        if message_contains_expense_claim_lines(msg):
            scores[DOMAIN_EXPENSE] = max(scores.get(DOMAIN_EXPENSE, 0.0), 0.9)
        elif wants_expense_summary(msg):
            scores[DOMAIN_EXPENSE] = max(scores.get(DOMAIN_EXPENSE, 0.0), 0.88)
        elif _strong_expense_claim(msg):
            scores[DOMAIN_EXPENSE] = max(scores.get(DOMAIN_EXPENSE, 0.0), 0.82)
    except Exception:
        pass

    try:
        from chat.services.hr_query_classifier import HrQueryContext, rules_classify_hr_query

        hr = rules_classify_hr_query(msg, context=HrQueryContext())
        if hr.in_hr_scope and hr.query_kind not in ("unknown", "chitchat"):
            kind = str(hr.query_kind or "")
            if kind in ("leave_request",):
                scores[DOMAIN_LEAVE] = max(scores.get(DOMAIN_LEAVE, 0.0), 0.9)
            elif kind in ("leave_balance",):
                scores[DOMAIN_BALANCE] = max(scores.get(DOMAIN_BALANCE, 0.0), 0.92)
            elif kind in ("hr_policy",):
                scores[DOMAIN_POLICY] = max(scores.get(DOMAIN_POLICY, 0.0), 0.9)
            elif kind.startswith("expense"):
                scores[DOMAIN_EXPENSE] = max(scores.get(DOMAIN_EXPENSE, 0.0), 0.88)
    except Exception:
        pass

    return scores


def _domains_from_scores(scores: dict[str, float], *, min_conf: float = 0.75) -> frozenset[str]:
    return frozenset(d for d, c in scores.items() if c >= min_conf)


def classify_message_scope(message: str, snapshot: Any) -> HrScopeResult:
    """
    Decide whether *message* is HR-assistant scope and which domains apply.

    Long off-topic messages → ``in_scope=False`` (router Tier U00).
    """
    msg = (message or "").strip()
    if not msg:
        return HrScopeResult(in_scope=False, confidence=0.0, reason="empty")

    wizard_active = _snapshot_wizard_active(snapshot)

    oos, oos_conf, oos_reason = probe_out_of_scope(msg)
    if oos and oos_conf >= 0.88:
        return HrScopeResult(
            in_scope=False,
            confidence=oos_conf,
            reason=f"oos:{oos_reason}",
            oos_reason=oos_reason,
        )

    domain_scores = _probe_domain_signals(msg)
    domains = _domains_from_scores(domain_scores)

    try:
        from chat.services.policy_intent_helpers import is_hr_assistant_in_scope

        if is_hr_assistant_in_scope(msg):
            if not domains:
                domains = frozenset({DOMAIN_LEAVE, DOMAIN_EXPENSE, DOMAIN_POLICY})
            return HrScopeResult(
                in_scope=True,
                confidence=0.92,
                reason="is_hr_assistant_in_scope",
                domains=domains,
            )
    except Exception:
        pass

    if domains:
        top = max(domain_scores.values())
        return HrScopeResult(
            in_scope=True,
            confidence=top,
            reason="hr_domain_signals",
            domains=domains,
            source="rules",
        )

  # Active wizard: short off-topic side statements still OOS; long unrelated → OOS
    if len(msg) >= LONG_MESSAGE_CHARS:
        try:
            from chat.services.policy_intent_helpers import is_off_topic_for_hr_assistant

            if is_off_topic_for_hr_assistant(msg, wizard_active=wizard_active):
                return HrScopeResult(
                    in_scope=False,
                    confidence=0.9,
                    reason="long_off_topic",
                    oos_reason="off_topic",
                )
        except Exception:
            pass

        # No HR keyword signal in a long message → likely general chat / trivia
        try:
            from chat.services.hr_query_classifier import _HR_ADJACENT_RE

            if not _HR_ADJACENT_RE.search(msg):
                return HrScopeResult(
                    in_scope=False,
                    confidence=0.86,
                    reason="long_no_hr_adjacent",
                    oos_reason="no_hr_signal",
                )
        except Exception:
            pass

    if wizard_active and len(msg) < LONG_MESSAGE_CHARS:
        return HrScopeResult(
            in_scope=True,
            confidence=0.55,
            reason="wizard_active_short",
            domains=frozenset(),
        )

    return HrScopeResult(
        in_scope=False,
        confidence=0.5,
        reason="no_hr_scope_match",
        oos_reason="unknown",
    )
