"""
Turn Understanding Layer — resolve user intent before routing.

Step 1: HR scope (in-scope vs OOS), especially for long messages
Step 2: context lock (pending prompt)
Step 3: rules with confidence
Step 4: optional LLM for long / ambiguous messages
"""

from __future__ import annotations

from typing import Any

from chat.services.turn_understanding.clarify_copy import build_utterance_clarification
from chat.services.turn_understanding.llm_gate import try_llm_resolve
from chat.services.turn_understanding.rules_expense import probe_expense_act
from chat.services.turn_understanding.rules_leave import probe_leave_act
from chat.services.turn_understanding.rules_scope import (
    probe_balance_query,
    probe_out_of_scope,
    probe_policy_query,
)
from chat.services.turn_understanding.scope import (
    DOMAIN_BALANCE,
    DOMAIN_EXPENSE,
    DOMAIN_LEAVE,
    DOMAIN_POLICY,
    LONG_MESSAGE_CHARS,
    classify_message_scope,
)
from chat.services.turn_understanding.schemas import (
    ACT_CONTINUE,
    ACT_NEEDS_CLARIFY,
    ACT_OUT_OF_SCOPE,
    ACT_QUERY_POLICY,
    ACT_QUERY_STATUS,
    ACT_SLOT_ANSWER,
    UtteranceResolution,
)


def _resolution_from_scope(scope: Any) -> UtteranceResolution | None:
    if scope.in_scope:
        return None
    if float(scope.confidence or 0) < 0.85:
        return None
    return UtteranceResolution(
        primary_act=ACT_OUT_OF_SCOPE,
        confidence=float(scope.confidence),
        in_scope=False,
        oos_reason=str(scope.oos_reason or scope.reason or ""),
        reason=str(scope.reason or "out_of_scope"),
        source=str(getattr(scope, "source", "rules") or "rules"),
        entities={"hr_domains": list(scope.domains)},
    )


def _count_hr_domains(scope: Any) -> int:
    return len(getattr(scope, "domains", None) or ())


def resolve_utterance(
    message: str,
    snapshot: Any,
    *,
    last_question: str = "",
    trace_id: str = "",
) -> UtteranceResolution:
    msg = (message or "").strip()
    if not msg:
        return UtteranceResolution(primary_act=ACT_CONTINUE, confidence=0.0, reason="empty")

    scope = classify_message_scope(msg, snapshot)
    scope_oos = _resolution_from_scope(scope)
    if scope_oos is not None:
        return scope_oos

    policy_hit, policy_conf = probe_policy_query(msg)
    balance_hit, balance_conf = probe_balance_query(msg)
    oos, oos_conf, oos_reason = probe_out_of_scope(msg)

    if policy_hit and balance_hit:
        return UtteranceResolution(
            primary_act=ACT_NEEDS_CLARIFY,
            confidence=0.75,
            needs_clarify=True,
            clarify_kind="multi_intent",
            in_scope=True,
            reason="policy_and_balance_mixed",
            entities={"hr_domains": [DOMAIN_POLICY, DOMAIN_BALANCE]},
        )

    if balance_hit and oos and oos_conf >= 0.75:
        return UtteranceResolution(
            primary_act=ACT_QUERY_STATUS,
            domain="leave",
            confidence=balance_conf,
            in_scope=True,
            entities={"oos_tail": True, "oos_reason": oos_reason, "hr_domains": [DOMAIN_BALANCE]},
            reason="balance_with_oos_tail",
        )

    if oos and oos_conf >= 0.88 and not policy_hit and not balance_hit and not scope.in_scope:
        return UtteranceResolution(
            primary_act=ACT_OUT_OF_SCOPE,
            confidence=oos_conf,
            in_scope=False,
            oos_reason=oos_reason,
            reason=f"oos:{oos_reason}",
        )

    if getattr(snapshot, "has_pending_prompt", False):
        from chat.services.session_expected_answer import message_plausibly_answers_prompt

        if message_plausibly_answers_prompt(msg, snapshot):
            domain = getattr(snapshot, "active_prompt_domain", None)
            return UtteranceResolution(
                primary_act=ACT_SLOT_ANSWER,
                domain=domain,
                confidence=0.92,
                in_scope=True,
                answers_prompt=True,
                reason="answers_active_prompt",
            )

    if policy_hit:
        return UtteranceResolution(
            primary_act=ACT_QUERY_POLICY,
            domain="policy",
            confidence=policy_conf,
            in_scope=True,
            reason="policy_query",
            entities={"hr_domains": [DOMAIN_POLICY]},
        )

    if balance_hit:
        return UtteranceResolution(
            primary_act=ACT_QUERY_STATUS,
            domain="leave",
            confidence=balance_conf,
            in_scope=True,
            reason="balance_query",
            entities={"hr_domains": [DOMAIN_BALANCE]},
        )

    best_act: str | None = None
    best_conf = 0.0
    best_reason = ""
    best_domain: str | None = None

    if getattr(snapshot, "expense_domain_active", False):
        act, conf, reason = probe_expense_act(msg, snapshot)
        if act and conf > best_conf:
            best_act, best_conf, best_reason, best_domain = act, conf, reason, DOMAIN_EXPENSE

    if getattr(snapshot, "leave_domain_active", False):
        act, conf, reason = probe_leave_act(msg, snapshot)
        if act and conf > best_conf:
            best_act, best_conf, best_reason, best_domain = act, conf, reason, DOMAIN_LEAVE

    if not best_act:
        act, conf, reason = probe_expense_act(msg, snapshot)
        if act and conf > best_conf:
            best_act, best_conf, best_reason, best_domain = act, conf, reason, DOMAIN_EXPENSE
        act, conf, reason = probe_leave_act(msg, snapshot)
        if act and conf > best_conf:
            best_act, best_conf, best_reason, best_domain = act, conf, reason, DOMAIN_LEAVE

    hr_domains = list(scope.domains) if scope.in_scope and scope.domains else []
    if best_domain and best_domain not in hr_domains:
        hr_domains.append(best_domain)

    if best_act and best_conf >= 0.8:
        return UtteranceResolution(
            primary_act=best_act,
            domain=best_domain,
            confidence=best_conf,
            in_scope=True,
            reason=best_reason,
            entities={"hr_domains": hr_domains} if hr_domains else {},
        )

    llm_res = try_llm_resolve(
        msg, snapshot=snapshot, last_question=last_question, trace_id=trace_id
    )
    if llm_res:
        if not llm_res.in_scope and float(llm_res.confidence or 0) >= 0.8:
            return UtteranceResolution(
                primary_act=ACT_OUT_OF_SCOPE,
                confidence=float(llm_res.confidence),
                in_scope=False,
                oos_reason=str(llm_res.oos_reason or "llm_out_of_scope"),
                reason="llm_out_of_scope",
                source="llm",
            )
        if llm_res.is_high_confidence() and llm_res.in_scope:
            if not llm_res.entities.get("hr_domains") and hr_domains:
                llm_res.entities["hr_domains"] = hr_domains
            llm_res.in_scope = True
            return llm_res

    if best_act and best_conf >= 0.5:
        return UtteranceResolution(
            primary_act=best_act,
            domain=best_domain,
            confidence=best_conf,
            in_scope=True,
            reason=best_reason,
            entities={"hr_domains": hr_domains} if hr_domains else {},
        )

    if (
        len(msg) >= LONG_MESSAGE_CHARS
        and scope.in_scope
        and _count_hr_domains(scope) >= 2
    ):
        return UtteranceResolution(
            primary_act=ACT_NEEDS_CLARIFY,
            confidence=0.72,
            needs_clarify=True,
            clarify_kind="multi_intent",
            in_scope=True,
            reason="long_multi_hr_domain",
            entities={"hr_domains": list(scope.domains)},
        )

    if len(msg) > 120 and getattr(snapshot, "has_pending_prompt", False):
        return UtteranceResolution(
            primary_act=ACT_NEEDS_CLARIFY,
            confidence=0.7,
            needs_clarify=True,
            clarify_kind="slot_mismatch",
            in_scope=scope.in_scope,
            reason="long_message_does_not_answer_prompt",
        )

    if len(msg) >= LONG_MESSAGE_CHARS and not scope.in_scope:
        return UtteranceResolution(
            primary_act=ACT_OUT_OF_SCOPE,
            confidence=max(0.85, float(scope.confidence or 0.85)),
            in_scope=False,
            oos_reason=str(scope.oos_reason or "no_hr_scope"),
            reason=str(scope.reason or "long_out_of_scope"),
        )

    return UtteranceResolution(
        primary_act=ACT_CONTINUE,
        confidence=0.3,
        in_scope=scope.in_scope,
        reason="no_rule_match",
        entities={"hr_domains": hr_domains} if hr_domains else {},
    )


def resolution_clarification_message(
    message: str,
    resolution: UtteranceResolution,
    *,
    snapshot: Any = None,
    lang: str | None = None,
) -> str:
    return build_utterance_clarification(message, resolution, snapshot=snapshot, lang=lang)
