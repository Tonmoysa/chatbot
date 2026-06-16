"""
Unified HR query classification — rules-first with LLM fallback.

Handles expense meta/recall/summary, leave intent, and in-scope detection for
Bengali / Banglish / voice STT without growing regex lists indefinitely.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from chat.constants import (
    INTENT_APPROVAL_ESCALATION,
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_EXPENSE_STATUS,
    INTENT_HR_POLICY,
    INTENT_LEAVE_BALANCE,
    INTENT_LEAVE_REQUEST,
    INTENT_UNKNOWN,
)
from chat.services.llm_client import LLMClient
from chat.services.hr_signal import HR_KEYWORD_RE, message_has_hr_signal

logger = logging.getLogger("hr_chatbot")

CONFIDENCE_LLM_FALLBACK = 0.72
CONFIDENCE_RULES = 0.95

# Backward-compatible alias for scope.py and tests.
_HR_ADJACENT_RE = HR_KEYWORD_RE

QUERY_EXPENSE_DAY_SUMMARY = "expense_day_summary"
QUERY_EXPENSE_META = "expense_meta"
QUERY_EXPENSE_RECALL = "expense_recall"
QUERY_EXPENSE_STATUS = "expense_status"
QUERY_EXPENSE_CLAIM = "expense_claim"
QUERY_LEAVE_REQUEST = "leave_request"
QUERY_LEAVE_BALANCE = "leave_balance"
QUERY_HR_POLICY = "hr_policy"
QUERY_CHITCHAT = "chitchat"
QUERY_APPROVAL_ESCALATION = "approval_escalation"
QUERY_UNKNOWN = "unknown"

DATE_TODAY = "today"
DATE_YESTERDAY = "yesterday"
DATE_NONE = "none"

_HR_QUERY_LLM_SYSTEM = """You classify HR assistant user messages (leave, expense, attendance, company policy).

CONTEXT may describe an active leave or expense wizard and parked (suspended) drafts.
Side questions during a wizard are common — classify what the user wants NOW, not the active form step.

Return STRICT JSON only:
{
  "query_kind": "expense_day_summary" | "expense_meta" | "expense_recall" | "expense_status" |
    "expense_claim" | "leave_request" | "leave_balance" | "hr_policy" | "approval_escalation" |
    "chitchat" | "unknown",
  "date_reference": "today" | "yesterday" | "none",
  "confidence": 0.0 to 1.0,
  "in_hr_scope": true | false
}

RULES
- expense_meta: user asks what the bot added / current draft / "ki add korcho" / "কত কস্ট এড করছি"
- expense_recall: user asks what they submitted or added on a PAST day
  Examples: "লাস্ট দিনে কোন এক্সপেন্স দিছিলাম", "goto kal ki kharcha dilam", "what did I submit yesterday"
- expense_day_summary: today's spend total / list / summary (not submitting new lines); also pending draft in session — "pending kono expense ache?", "pending expense ta daw", "amar kache pending kharcha ache ki?"
- expense_status: submit done? / reference / tracking (not a new claim line with amounts)
- expense_claim: user lists NEW costs with amounts (lunch 100, bus 50)
- leave_request: apply for leave (not policy question)
- leave_balance: how many leave days remaining
- hr_policy: company rules, entitlement, handbook
- approval_escalation: manager approval pending / escalate approval
- chitchat: greetings, jokes, general knowledge (eid kobe, weather, python ki)
- unknown: unclear
- date_reference yesterday for: last day, গত দিন, লাস্ট দিন, goto kal, yesterday
- date_reference today for: ajke, today, আজ
- in_hr_scope true for all HR kinds; false for chitchat/unknown/general trivia
- NEVER classify "lunch 100 taka" as meta — that is expense_claim
"""

_turn_cache: dict[str, HrQueryDecision] = {}


@dataclass
class HrQueryContext:
    expense_active: bool = False
    leave_active: bool = False
    expense_stage: str = ""
    leave_pending_step: str = ""
    has_expense_draft: bool = False
    has_expense_submissions: bool = False
    session_expense_summary: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class HrQueryDecision:
    query_kind: str = QUERY_UNKNOWN
    confidence: float = 0.0
    source: str = "none"
    in_hr_scope: bool = False
    date_reference: str = DATE_NONE
    maps_to_intent: str | None = None

    def date_iso(self, *, today: date | None = None) -> str | None:
        today_d = today or date.today()
        if self.date_reference == DATE_TODAY:
            return today_d.isoformat()
        if self.date_reference == DATE_YESTERDAY:
            return (today_d - timedelta(days=1)).isoformat()
        return None


def _map_kind_to_intent(kind: str) -> str | None:
    if kind == QUERY_EXPENSE_DAY_SUMMARY or kind == QUERY_EXPENSE_RECALL:
        return INTENT_EXPENSE_DAY_SUMMARY
    if kind == QUERY_EXPENSE_META or kind == QUERY_EXPENSE_STATUS:
        return INTENT_EXPENSE_STATUS
    if kind == QUERY_EXPENSE_CLAIM:
        return INTENT_EXPENSE_CLAIM
    if kind == QUERY_LEAVE_REQUEST:
        return INTENT_LEAVE_REQUEST
    if kind == QUERY_LEAVE_BALANCE:
        return INTENT_LEAVE_BALANCE
    if kind == QUERY_HR_POLICY:
        return INTENT_HR_POLICY
    if kind == QUERY_APPROVAL_ESCALATION:
        return INTENT_APPROVAL_ESCALATION
    if kind == QUERY_CHITCHAT:
        return INTENT_UNKNOWN
    return None


def _finish_decision(
    kind: str,
    *,
    confidence: float,
    source: str,
    date_reference: str = DATE_NONE,
) -> HrQueryDecision:
    in_scope = kind not in (QUERY_UNKNOWN, QUERY_CHITCHAT)
    return HrQueryDecision(
        query_kind=kind,
        confidence=confidence,
        source=source,
        in_hr_scope=in_scope,
        date_reference=date_reference,
        maps_to_intent=_map_kind_to_intent(kind),
    )


def rules_classify_hr_query(
    message: str,
    *,
    context: HrQueryContext | None = None,
) -> HrQueryDecision:
    """Deterministic HR query classification — no LLM."""
    ctx = context or HrQueryContext()
    raw = (message or "").strip()
    if not raw:
        return HrQueryDecision()

    if ctx.expense_active:
        from chat.services.expense.expense_confirm import (
            is_confirmation_no,
            is_confirmation_yes,
            looks_like_expense_correction,
        )
        from chat.services.expense.wizard_commands import (
            wants_expense_done_command_rules_only,
            wants_expense_submit_command,
        )

        if (
            wants_expense_submit_command(raw)
            or wants_expense_done_command_rules_only(raw)
            or is_confirmation_yes(raw)
            or is_confirmation_no(raw)
            or looks_like_expense_correction(raw)
        ):
            return _finish_decision(
                QUERY_EXPENSE_CLAIM,
                confidence=CONFIDENCE_RULES,
                source="rules_expense_wizard_command",
            )

    from chat.services.expense.expense_total_dispute import is_expense_total_check_query
    from chat.services.expense.session_action_memory import wants_expense_meta_question
    from chat.services.expense.session_ledger import (
        wants_pending_expense_query,
        wants_recent_expense_recall_query,
        wants_session_expense_ledger_query,
    )
    from chat.services.expense_workflow import message_mentions_expense_spend, wants_expense_spend_recap_query
    from chat.services.intent_detector import (
        _strong_expense_claim,
        _strong_expense_day_summary,
        _strong_hr_policy,
        _looks_like_chitchat,
    )
    from chat.services.leave_balance_intent import is_leave_balance_query
    from chat.services.policy_intent_helpers import (
        is_expense_entitlement_query,
        is_general_knowledge_out_of_scope,
        is_rules_query,
    )
    from chat.services.workflow_navigation import is_leave_application_message

    if is_expense_entitlement_query(raw) or (
        _strong_hr_policy(raw) and is_rules_query(raw)
    ):
        return _finish_decision(
            QUERY_HR_POLICY, confidence=CONFIDENCE_RULES, source="rules_policy"
        )

    from chat.services.policy_intent_helpers import is_rules_query as _is_rules_query

    if _strong_hr_policy(raw) and _is_rules_query(raw) and re.search(
        r"annual\s+leave|sick\s+leave|casual\s+leave",
        raw,
        re.I,
    ):
        return _finish_decision(
            QUERY_HR_POLICY, confidence=CONFIDENCE_RULES, source="rules_named_policy"
        )

    from chat.services.leave_meta_queries import wants_leave_session_summary

    if wants_leave_session_summary(raw):
        return _finish_decision(
            QUERY_LEAVE_REQUEST,
            confidence=CONFIDENCE_RULES,
            source="rules_leave_summary",
        )

    if is_leave_balance_query(raw):
        return _finish_decision(
            QUERY_LEAVE_BALANCE, confidence=CONFIDENCE_RULES, source="rules_balance"
        )

    from chat.services.hr_signal import message_looks_like_approval_query

    if message_looks_like_approval_query(raw):
        return _finish_decision(
            QUERY_APPROVAL_ESCALATION,
            confidence=CONFIDENCE_RULES,
            source="rules_approval",
        )

    if wants_recent_expense_recall_query(raw):
        return _finish_decision(
            QUERY_EXPENSE_RECALL,
            confidence=CONFIDENCE_RULES,
            source="rules_expense_recall",
            date_reference=DATE_YESTERDAY,
        )

    if wants_pending_expense_query(raw):
        return _finish_decision(
            QUERY_EXPENSE_DAY_SUMMARY,
            confidence=CONFIDENCE_RULES,
            source="rules_pending_expense_query",
            date_reference=DATE_TODAY,
        )

    if wants_expense_meta_question(raw):
        return _finish_decision(
            QUERY_EXPENSE_META, confidence=CONFIDENCE_RULES, source="rules_expense_meta"
        )

    try:
        from chat.services.expense_extraction import message_contains_expense_claim_lines
    except Exception:
        message_contains_expense_claim_lines = None  # type: ignore[assignment]

    if message_contains_expense_claim_lines and message_contains_expense_claim_lines(raw):
        return _finish_decision(
            QUERY_EXPENSE_CLAIM, confidence=CONFIDENCE_RULES, source="rules_expense_claim_lines"
        )

    if is_expense_total_check_query(raw):
        return _finish_decision(
            QUERY_EXPENSE_STATUS,
            confidence=CONFIDENCE_RULES,
            source="rules_expense_total_check",
        )

    if (
        _strong_expense_day_summary(raw)
        or wants_session_expense_ledger_query(raw)
        or wants_expense_spend_recap_query(raw)
    ) and not (
        message_contains_expense_claim_lines and message_contains_expense_claim_lines(raw)
    ):
        date_ref = DATE_NONE
        if re.search(
            r"(?:লাস্ট\s*দিন|গত\s*দিন|yesterday|goto\s*kal|গতকাল|কালকে)",
            raw,
            re.I | re.UNICODE,
        ):
            date_ref = DATE_YESTERDAY
        elif re.search(r"(?:আজ|আজকে|ajke|today)", raw, re.I):
            date_ref = DATE_TODAY
        return _finish_decision(
            QUERY_EXPENSE_DAY_SUMMARY,
            confidence=CONFIDENCE_RULES,
            source="rules_expense_summary",
            date_reference=date_ref,
        )

    if is_leave_application_message(raw):
        return _finish_decision(
            QUERY_LEAVE_REQUEST, confidence=CONFIDENCE_RULES, source="rules_leave_apply"
        )

    if _strong_expense_claim(raw):
        return _finish_decision(
            QUERY_EXPENSE_CLAIM, confidence=CONFIDENCE_RULES, source="rules_expense_claim"
        )

    if is_general_knowledge_out_of_scope(raw):
        return _finish_decision(
            QUERY_CHITCHAT, confidence=0.9, source="rules_general_knowledge"
        )

    wizard_active = ctx.expense_active or ctx.leave_active
    if _looks_like_chitchat(raw, strict=wizard_active):
        return _finish_decision(QUERY_CHITCHAT, confidence=0.85, source="rules_chitchat")

    if message_mentions_expense_spend(raw) and re.search(
        r"(?:\?|কি|কোন|কত|keno|why|how)",
        raw,
        re.I | re.UNICODE,
    ):
        return HrQueryDecision(
            query_kind=QUERY_UNKNOWN,
            confidence=0.4,
            source="rules_expense_ambiguous",
            in_hr_scope=True,
        )

    return HrQueryDecision()


def _context_block(context: HrQueryContext) -> str:
    lines: list[str] = []
    if context.expense_active:
        lines.append(f"Expense wizard active (stage={context.expense_stage or '?'})")
    if context.leave_active:
        lines.append(f"Leave wizard active (step={context.leave_pending_step or '?'})")
    if context.has_expense_draft:
        lines.append("Session has expense draft lines")
    if context.has_expense_submissions:
        lines.append("Session has submitted expense batch(es)")
    if context.session_expense_summary:
        lines.append(f"Session expense: {context.session_expense_summary}")
    if not lines:
        lines.append("No active wizard")
    return "\n".join(lines)


def _llm_classify_hr_query(
    message: str,
    *,
    context: HrQueryContext,
    trace_id: str,
    llm: LLMClient | None = None,
) -> HrQueryDecision | None:
    client = llm or LLMClient()
    if not client.is_configured():
        return None

    user_prompt = (
        f"{_context_block(context)}\n\n"
        f"User message:\n{(message or '').strip()}\n\n"
        "Return JSON only."
    )
    out = client.chat_json(
        system_prompt=_HR_QUERY_LLM_SYSTEM,
        user_prompt=user_prompt,
        trace_id=trace_id or "hr-query-llm",
    )
    if not isinstance(out, dict):
        return None

    kind = str(out.get("query_kind") or QUERY_UNKNOWN).strip().lower()
    confidence = float(out.get("confidence") or 0.0)
    date_ref = str(out.get("date_reference") or DATE_NONE).strip().lower()
    in_scope = bool(out.get("in_hr_scope"))

    if kind not in {
        QUERY_EXPENSE_DAY_SUMMARY,
        QUERY_EXPENSE_META,
        QUERY_EXPENSE_RECALL,
        QUERY_EXPENSE_STATUS,
        QUERY_EXPENSE_CLAIM,
        QUERY_LEAVE_REQUEST,
        QUERY_LEAVE_BALANCE,
        QUERY_HR_POLICY,
        QUERY_APPROVAL_ESCALATION,
        QUERY_CHITCHAT,
        QUERY_UNKNOWN,
    }:
        kind = QUERY_UNKNOWN

    if date_ref not in (DATE_TODAY, DATE_YESTERDAY, DATE_NONE):
        date_ref = DATE_NONE

    if confidence < CONFIDENCE_LLM_FALLBACK:
        return HrQueryDecision(
            query_kind=QUERY_UNKNOWN,
            confidence=confidence,
            source="llm_low_confidence",
            in_hr_scope=in_scope,
        )

    logger.info(
        "hr_query_llm trace_id=%s kind=%s confidence=%s date=%s",
        trace_id,
        kind,
        confidence,
        date_ref,
    )
    decision = _finish_decision(
        kind,
        confidence=confidence,
        source="llm",
        date_reference=date_ref if kind in (
            QUERY_EXPENSE_DAY_SUMMARY,
            QUERY_EXPENSE_RECALL,
        ) else DATE_NONE,
    )
    decision.in_hr_scope = in_scope or decision.in_hr_scope
    return decision


def hr_query_llm_allowed_during_wizard(
    message: str,
    workflow_state: dict[str, Any] | None = None,
) -> bool:
    """
    Side questions during an active wizard still need LLM when regex is unsure.
    Facts/decisions stay rule-bound; LLM only classifies intent.
    """
    raw = (message or "").strip()
    if not raw or len(raw) < 5:
        return False
    try:
        from chat.services.expense_workflow import wants_expense_spend_recap_query
        from chat.services.leave_confirm import wants_defer_expense_for_leave_submit

        from chat.services.expense.session_ledger import wants_pending_expense_query

        if (
            wants_expense_spend_recap_query(raw)
            or wants_pending_expense_query(raw)
            or wants_defer_expense_for_leave_submit(raw)
        ):
            return True
    except Exception:
        pass
    if _HR_ADJACENT_RE.search(raw):
        return True
    wf = workflow_state or {}
    if wf.get("suspended_leave") or wf.get("suspended_expense"):
        return True
    return False


def _should_try_llm(message: str, rules: HrQueryDecision) -> bool:
    raw = (message or "").strip()
    if not raw or len(raw) < 5:
        return False
    if rules.query_kind not in (QUERY_UNKNOWN, QUERY_CHITCHAT):
        return False
    if rules.in_hr_scope and rules.source == "rules_expense_ambiguous":
        return True
    if message_has_hr_signal(raw):
        return True
    if re.search(r"(?:\?|কি|কোন|কত|ki\s|koto)", raw, re.I | re.UNICODE):
        return True
    return False


def classify_hr_query(
    message: str,
    *,
    context: HrQueryContext | None = None,
    trace_id: str = "",
    use_llm: bool = True,
    llm: LLMClient | None = None,
    wizard_side_llm: bool = False,
) -> HrQueryDecision:
    """
    Rules-first HR query classification with optional LLM fallback.

    Cached per (trace_id, message) for the duration of one orchestrator turn.
    """
    cache_key = f"{trace_id}:{(message or '').strip()}"
    if cache_key in _turn_cache:
        return _turn_cache[cache_key]

    ctx = context or HrQueryContext()
    rules = rules_classify_hr_query(message, context=ctx)
    if rules.query_kind not in (QUERY_UNKNOWN, QUERY_CHITCHAT) and rules.confidence >= CONFIDENCE_LLM_FALLBACK:
        _turn_cache[cache_key] = rules
        return rules

    if use_llm and (_should_try_llm(message, rules) or wizard_side_llm):
        llm_decision = _llm_classify_hr_query(
            message, context=ctx, trace_id=trace_id, llm=llm
        )
        if llm_decision and llm_decision.query_kind != QUERY_UNKNOWN:
            _turn_cache[cache_key] = llm_decision
            return llm_decision
        if llm_decision and llm_decision.in_hr_scope:
            _turn_cache[cache_key] = llm_decision
            return llm_decision

    if rules.in_hr_scope:
        _turn_cache[cache_key] = rules
        return rules

    _turn_cache[cache_key] = rules
    return rules


def clear_hr_query_cache() -> None:
    """Test helper — drop per-turn cache."""
    _turn_cache.clear()


def build_hr_query_context(workflow_state: dict[str, Any] | None) -> HrQueryContext:
    from chat.services.expense.expense_fsm import read_expense_block
    from chat.services.expense.session_ledger import (
        draft_line_rows_for_block,
        session_has_expense_draft_data,
    )
    from chat.services.leave_workflow import is_leave_in_progress, pending_step

    wf = workflow_state or {}
    block = read_expense_block(wf)
    items = draft_line_rows_for_block(block)
    summary_parts: list[str] = []
    for row in items[:4]:
        summary_parts.append(
            f"{row.get('category') or '?'} {row.get('amount', '?')} Tk"
        )
    hist = wf.get("expense_submissions_history") or []
    return HrQueryContext(
        expense_active=bool(block.get("active")),
        leave_active=is_leave_in_progress(wf),
        expense_stage=str(block.get("stage") or ""),
        leave_pending_step=str(pending_step(wf) or ""),
        has_expense_draft=session_has_expense_draft_data(wf),
        has_expense_submissions=bool(hist),
        session_expense_summary=", ".join(summary_parts) if summary_parts else "",
    )


def decision_suppresses_out_of_scope(decision: HrQueryDecision) -> bool:
    """True when an HR query must not be declined as general out-of-scope."""
    return bool(
        decision.in_hr_scope
        and decision.query_kind
        not in (QUERY_UNKNOWN, QUERY_CHITCHAT)
    )


def apply_hr_query_to_intent(
    intent: str,
    intent_result: dict[str, Any],
    decision: HrQueryDecision,
    *,
    message: str = "",
    router_locked: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Upgrade weak/unknown intent when the query classifier is confident.

    When the session router decisively locked the intent (``router_locked``),
    only the HR_POLICY upgrade may still apply (spec §6 / invariant I5:
    policy beats expense claim); all other overrides are skipped so the
    classifier cannot re-introduce parallel-layer conflicts.
    """
    if not decision.maps_to_intent:
        return intent, intent_result
    if decision.confidence < CONFIDENCE_LLM_FALLBACK:
        return intent, intent_result
    if router_locked and decision.maps_to_intent != INTENT_HR_POLICY:
        return intent, intent_result
    if decision.maps_to_intent == INTENT_HR_POLICY:
        if intent != INTENT_HR_POLICY:
            return decision.maps_to_intent, {
                **intent_result,
                "intent": decision.maps_to_intent,
                "confidence": decision.confidence,
                "source": (intent_result.get("source") or "intent")
                + f"+hr_query_{decision.source}",
            }
        return intent, intent_result
    from chat.services.expense.wizard_commands import (
        is_expense_wizard_command,
        wants_expense_submit_command,
    )

    if intent == INTENT_EXPENSE_CLAIM and (
        wants_expense_submit_command(message)
        or is_expense_wizard_command(message)
    ):
        return intent, intent_result

    weak = intent == INTENT_UNKNOWN or float(intent_result.get("confidence") or 0) < 0.75
    override_claim = (
        intent == INTENT_EXPENSE_CLAIM
        and decision.query_kind
        in (QUERY_EXPENSE_META, QUERY_EXPENSE_RECALL, QUERY_EXPENSE_DAY_SUMMARY, QUERY_EXPENSE_STATUS)
    )
    if weak or override_claim:
        return decision.maps_to_intent, {
            **intent_result,
            "intent": decision.maps_to_intent,
            "confidence": decision.confidence,
            "source": (intent_result.get("source") or "intent")
            + f"+hr_query_{decision.source}",
        }
    return intent, intent_result
