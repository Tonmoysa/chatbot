"""Semantic clarify reply parser — always-on LLM during clarify (P1) + guardrails (P0)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from chat.services.expense.clarify import ClarificationIssue, _issue_context_row
from chat.services.expense.clarify_affirmatives import (
    is_clarify_affirmative_only,
    is_clarify_affirmative_token,
    is_implausible_clarify_location,
    is_invalid_clarify_location,
    looks_like_typo_acknowledgment,
)
from chat.services.expense_extraction import normalize_category, parse_category_token
from chat.services.llm_client import LLMClient

logger = logging.getLogger("hr_chatbot")

_VALID_ACTIONS = frozenset(
    {
        "confirm_typo",
        "set_location",
        "set_category",
        "confirm_category_typo",
    }
)

_CLARIFY_REPLY_LLM_SYSTEM = """You parse user replies to a batched expense clarification prompt.

Return ONLY JSON — no prose:

{
  "answers": [
    {
      "issue_index": 1,
      "action": "confirm_typo",
      "value": "motejheel"
    }
  ],
  "needs_disambiguation": false,
  "user_meant_affirmative_only": false,
  "user_sent_praise_or_meta": false
}

ACTIONS (issue_index is 1-based, must match OPEN ISSUES)
- confirm_typo: user agrees with suggested location fix (ha, hae, han, yes, thik, okay, perfectly okay)
  → value MUST be the suggestion from OPEN ISSUES, never "hae"/"yes" as location
- set_location: user gives explicit location name (mirpur, motejheel)
- set_category: user names expense category (Bus, Lunch, Metro Rail, …)
- confirm_category_typo: user confirms category suggestion

RULES
- Affirmatives (ha, hae, han, ji, yes, thik, thik ache, perfectly okay, hoy) → confirm_typo or confirm_category_typo with SUGGESTION value
- NEVER use ha/hae/yes/ok/thik as a location in set_location
- If user answers only ONE of several issues, include only that answer
- If ambiguous which issue, set needs_disambiguation true and answers []
- "submit daw" during clarify is NOT CRM submit — ignore unless clearly answering an issue
- Bangla, Banglish, English supported
- Do NOT invent amounts, routes, or new expense lines
- user_sent_praise_or_meta: true when user thanks/praises the bot or admits spelling mistake
  conversationally (awesome, valo analysis, banan vul diyechilam) — still apply confirm_typo if confirming
"""


@dataclass
class ClarifyLlmAnswer:
    issue_index: int
    action: str
    value: str


@dataclass
class ClarifyLlmReplyResult:
    answers: list[ClarifyLlmAnswer] = field(default_factory=list)
    needs_disambiguation: bool = False
    user_meant_affirmative_only: bool = False
    user_sent_praise_or_meta: bool = False


def clarify_llm_enabled(*, use_llm: bool = True) -> bool:
    """P1: semantic resolver runs on every clarify reply when LLM is configured."""
    if not use_llm:
        return False
    return LLMClient().is_configured()


def clarify_llm_should_use(
    message: str,
    issues: list[ClarificationIssue],
    *,
    rules_unresolved: list[ClarificationIssue],
    needs_disambiguation: bool,
    use_llm: bool = True,
) -> bool:
    """Backward-compatible gate — always-on when LLM configured."""
    del message, issues, rules_unresolved, needs_disambiguation
    return clarify_llm_enabled(use_llm=use_llm)


def _issues_context_block(
    issues: list[ClarificationIssue],
    items: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
) -> str:
    lines: list[str] = ["OPEN ISSUES:"]
    for i, issue in enumerate(issues, start=1):
        row = _issue_context_row(issue, items, pending_entries) or {}
        amt = float(issue.amount or row.get("amount") or 0)
        frm = str(row.get("from_location") or "").strip()
        to = str(row.get("to_location") or "").strip()
        route = f" route={frm}→{to}" if frm and to else ""
        if issue.kind == "location_typo":
            role = "to" if issue.field == "to_location" else "from"
            lines.append(
                f"{i}. location_typo · {issue.category} {amt:g} Tk{route} · "
                f"{role}_location **{issue.original}** → suggest **{issue.suggestion}**"
            )
        elif issue.kind == "missing_category":
            lines.append(f"{i}. missing_category · {amt:g} Tk{route}")
        elif issue.kind == "category_typo":
            lines.append(
                f"{i}. category_typo · {amt:g} Tk · "
                f"**{issue.original}** → suggest **{issue.suggestion}**"
            )
        else:
            lines.append(f"{i}. {issue.kind} · {amt:g} Tk")
    return "\n".join(lines)


def llm_json_to_clarify_reply(data: dict[str, Any] | None) -> ClarifyLlmReplyResult | None:
    if not isinstance(data, dict):
        return None
    result = ClarifyLlmReplyResult(
        needs_disambiguation=bool(data.get("needs_disambiguation")),
        user_meant_affirmative_only=bool(data.get("user_meant_affirmative_only")),
        user_sent_praise_or_meta=bool(data.get("user_sent_praise_or_meta")),
    )
    for row in data.get("answers") or []:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("issue_index"))
        except (TypeError, ValueError):
            continue
        action = str(row.get("action") or "set_category").strip().lower()
        if action not in _VALID_ACTIONS:
            action = _infer_action_from_kind(row, action)
        value = str(row.get("value") or "").strip()
        if idx >= 1 and value:
            result.answers.append(
                ClarifyLlmAnswer(issue_index=idx, action=action, value=value)
            )
    if result.needs_disambiguation and not result.answers:
        return result
    if result.answers:
        return result
    if result.user_sent_praise_or_meta:
        return result
    if result.needs_disambiguation:
        return result
    return None


def _infer_action_from_kind(row: dict[str, Any], fallback: str) -> str:
    kind = str(row.get("kind") or "").strip().lower()
    if kind == "location_typo":
        return "confirm_typo"
    if kind == "category_typo":
        return "confirm_category_typo"
    if fallback in _VALID_ACTIONS:
        return fallback
    return "set_category"


def parse_clarify_reply_llm(
    message: str,
    issues: list[ClarificationIssue],
    items: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
    *,
    trace_id: str = "",
    last_question: str = "",
    llm: LLMClient | None = None,
) -> ClarifyLlmReplyResult | None:
    """Call LLM to semantically parse a clarify reply."""
    text = (message or "").strip()
    if not text or not issues:
        return None
    client = llm or LLMClient()
    if not client.is_configured():
        return None

    from chat.services.expense.llm_context import build_wizard_llm_context

    ctx = build_wizard_llm_context(
        items,
        stage="collecting",
        pending_step="clarify",
        block={"clarification_issues": [i.to_dict() for i in issues]},
        last_question=last_question,
    )
    user_prompt = (
        f"{ctx}\n\n"
        f"{_issues_context_block(issues, items, pending_entries)}\n\n"
        f"User clarify reply:\n{text}\n\n"
        "Return JSON only."
    )
    out = client.chat_json(
        system_prompt=_CLARIFY_REPLY_LLM_SYSTEM,
        user_prompt=user_prompt,
        trace_id=trace_id or "expense-clarify-llm",
    )
    result = llm_json_to_clarify_reply(out)
    if result is None and out is not None:
        logger.info(
            "expense_clarify_llm_rejected trace_id=%s",
            trace_id or "expense-clarify-llm",
        )
    return result


def _resolve_location_value(
    value: str,
    issue: ClarificationIssue,
    *,
    action: str = "",
) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    low = raw.lower()
    act = (action or "").strip().lower()

    if act == "confirm_typo" or is_clarify_affirmative_token(raw):
        return issue.suggestion or None
    if looks_like_typo_acknowledgment(raw):
        return issue.suggestion or None
    if is_invalid_clarify_location(raw):
        return issue.suggestion if is_clarify_affirmative_only(raw) else None
    if is_implausible_clarify_location(raw, issue=issue):
        return issue.suggestion if looks_like_typo_acknowledgment(raw) else None
    if issue.suggestion and issue.suggestion.lower() in low:
        return issue.suggestion
    if parse_category_token(raw):
        return None
    if len(raw) >= 2 and not is_implausible_clarify_location(raw, issue=issue):
        return raw
    return None


def _resolve_category_value(
    value: str,
    issue: ClarificationIssue,
    *,
    action: str = "",
) -> str | None:
    raw = (value or "").strip()
    if not raw:
        return None
    act = (action or "").strip().lower()
    if act == "confirm_category_typo" or is_clarify_affirmative_token(raw):
        if issue.suggestion:
            return normalize_category(issue.suggestion)
    if is_clarify_affirmative_only(raw) and issue.suggestion:
        return normalize_category(issue.suggestion)
    cat = normalize_category(raw)
    if cat:
        return cat
    token = parse_category_token(raw)
    return normalize_category(token) if token else None


def apply_clarify_llm_result(
    result: ClarifyLlmReplyResult,
    issues: list[ClarificationIssue],
    out_items: list[dict[str, Any]],
    out_pending: list[dict[str, Any]],
    target_indices: set[int],
    *,
    message: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[ClarificationIssue], bool]:
    """Apply semantic LLM answers onto draft state."""
    if result.needs_disambiguation and not result.answers:
        return out_items, out_pending, list(issues), True

    from chat.services.expense.clarify import _looks_like_category_assignment

    cat_assign_msg = _looks_like_category_assignment(message)
    remaining = set(target_indices)
    for ans in result.answers:
        idx = ans.issue_index - 1
        if idx < 0 or idx >= len(issues):
            continue
        if idx not in remaining and remaining != set(range(len(issues))):
            continue
        issue = issues[idx]
        if cat_assign_msg and issue.kind == "location_typo":
            continue
        applied = False
        action = ans.action or "set_category"

        if issue.kind == "location_typo":
            loc = _resolve_location_value(ans.value, issue, action=action)
            if loc and not is_invalid_clarify_location(loc):
                if 0 <= issue.item_index < len(out_items):
                    out_items[issue.item_index][issue.field] = loc
                    applied = True
            elif action == "confirm_typo" and issue.suggestion:
                if 0 <= issue.item_index < len(out_items):
                    out_items[issue.item_index][issue.field] = issue.suggestion
                    applied = True
        elif issue.kind == "category_typo":
            cat = _resolve_category_value(ans.value, issue, action=action)
            if cat and 0 <= issue.pending_index < len(out_pending):
                out_pending[issue.pending_index]["category"] = cat
                applied = True
        elif issue.kind == "missing_category":
            cat = _resolve_category_value(ans.value, issue, action=action)
            if cat and 0 <= issue.pending_index < len(out_pending):
                out_pending[issue.pending_index]["category"] = cat
                applied = True
            elif cat and 0 <= issue.item_index < len(out_items):
                out_items[issue.item_index]["category"] = cat
                applied = True
        if applied:
            remaining.discard(idx)

    if result.needs_disambiguation and remaining:
        unresolved = [issues[i] for i in sorted(remaining)]
        return out_items, out_pending, unresolved, True

    unresolved = [issues[i] for i in sorted(remaining)]
    return out_items, out_pending, unresolved, False


def _fix_affirmative_misapplied_locations(
    message: str,
    out_items: list[dict[str, Any]],
    issues: list[ClarificationIssue],
) -> list[dict[str, Any]]:
    fixed = [dict(row) for row in out_items]
    for issue in issues:
        if issue.kind != "location_typo" or issue.item_index < 0:
            continue
        if issue.item_index >= len(fixed):
            continue
        val = str(fixed[issue.item_index].get(issue.field) or "").strip()
        if looks_like_typo_acknowledgment(message) and issue.suggestion:
            fixed[issue.item_index][issue.field] = issue.suggestion
            continue
        if is_clarify_affirmative_only(message) and is_invalid_clarify_location(val):
            if issue.suggestion:
                fixed[issue.item_index][issue.field] = issue.suggestion
            continue
        if is_implausible_clarify_location(val, issue=issue):
            fixed[issue.item_index][issue.field] = issue.original or val
    return fixed


def _issue_still_open(
    issue: ClarificationIssue,
    out_items: list[dict[str, Any]],
    out_pending: list[dict[str, Any]],
) -> bool:
    if issue.kind == "location_typo":
        if issue.item_index < 0 or issue.item_index >= len(out_items):
            return True
        val = str(out_items[issue.item_index].get(issue.field) or "").strip().lower()
        orig = (issue.original or "").strip().lower()
        if not val or val == orig or is_invalid_clarify_location(val):
            return True
        if is_implausible_clarify_location(val, issue=issue):
            return True
        return False
    if issue.kind == "missing_category":
        if 0 <= issue.pending_index < len(out_pending):
            return not str(out_pending[issue.pending_index].get("category") or "").strip()
        if 0 <= issue.item_index < len(out_items):
            return not str(out_items[issue.item_index].get("category") or "").strip()
        return True
    if issue.kind == "category_typo":
        if 0 <= issue.pending_index < len(out_pending):
            return not str(out_pending[issue.pending_index].get("category") or "").strip()
        return True
    return True


def _rules_fallback_for_affirmative(
    message: str,
    issues: list[ClarificationIssue],
    out_items: list[dict[str, Any]],
    out_pending: list[dict[str, Any]],
    open_indices: set[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[int]]:
    """When LLM unavailable, affirmatives / typo ack confirm the first open typo issue."""
    if not is_clarify_affirmative_only(message) and not looks_like_typo_acknowledgment(message):
        return out_items, out_pending, open_indices
    items = [dict(r) for r in out_items]
    pending = [dict(r) for r in out_pending]
    remaining = set(open_indices)
    for idx in sorted(open_indices):
        issue = issues[idx]
        if issue.kind != "location_typo" or not issue.suggestion:
            continue
        if 0 <= issue.item_index < len(items):
            items[issue.item_index][issue.field] = issue.suggestion
            remaining.discard(idx)
        break
    return items, pending, remaining


def reconcile_clarify_rules_and_llm(
    message: str,
    issues: list[ClarificationIssue],
    out_items: list[dict[str, Any]],
    out_pending: list[dict[str, Any]],
    rules_unresolved: list[ClarificationIssue],
    rules_needs_disambig: bool,
    llm_result: ClarifyLlmReplyResult | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[ClarificationIssue], bool]:
    """Merge rules output with always-on semantic LLM (P1)."""
    open_keys = {
        (u.kind, u.item_index, u.pending_index, u.field) for u in rules_unresolved
    }
    open_indices = {
        idx
        for idx, issue in enumerate(issues)
        if (issue.kind, issue.item_index, issue.pending_index, issue.field) in open_keys
    }
    if rules_needs_disambig:
        open_indices = set(range(len(issues)))

    for idx, issue in enumerate(issues):
        if issue.kind != "location_typo" or issue.item_index < 0:
            continue
        val = str(out_items[issue.item_index].get(issue.field) or "").strip()
        if is_invalid_clarify_location(val) or is_implausible_clarify_location(
            val, issue=issue
        ):
            open_indices.add(idx)

    items = [dict(r) for r in out_items]
    pending = [dict(r) for r in out_pending]

    if not open_indices and not rules_needs_disambig:
        items = _fix_affirmative_misapplied_locations(message, items, issues)
        final_open = [
            issue
            for issue in issues
            if _issue_still_open(issue, items, pending)
        ]
        return items, pending, final_open, False

    if not llm_result:
        items, pending, open_indices = _rules_fallback_for_affirmative(
            message, issues, items, pending, open_indices
        )
        items = _fix_affirmative_misapplied_locations(message, items, issues)
        unresolved = [
            issues[i]
            for i in sorted(open_indices)
            if _issue_still_open(issues[i], items, pending)
        ]
        if rules_needs_disambig and unresolved:
            return items, pending, unresolved, True
        return items, pending, unresolved, False

    if llm_result.needs_disambiguation and not llm_result.answers:
        return items, pending, rules_unresolved or list(issues), True

    target = open_indices if open_indices else set(range(len(issues)))
    items, pending, _, _ = apply_clarify_llm_result(
        llm_result, issues, items, pending, target, message=message
    )
    items = _fix_affirmative_misapplied_locations(message, items, issues)

    final_open = [
        issue
        for idx, issue in enumerate(issues)
        if _issue_still_open(issue, items, pending)
    ]
    if llm_result.needs_disambiguation and final_open:
        return items, pending, final_open, True
    return items, pending, final_open, False
