"""Parse expense wizard turns: rules fast-path + LLM fallback (Phases A–D)."""

from __future__ import annotations

import logging
import re
from typing import Any

from chat.services.expense.command_llm_parser import llm_json_to_correction_plan
from chat.services.expense.command_parser import parse_correction_plan
from chat.services.expense.command_schema import CorrectionCommandPlan
from chat.services.expense.expense_confirm import (
    is_confirmation_no,
    is_confirmation_yes,
    looks_like_compound_expense_claim,
    looks_like_expense_correction,
)
from chat.services.expense.turn_schema import (
    CONFIDENCE_LLM_FALLBACK,
    TURN_ADD_LINES,
    TURN_CLARIFY_REPLY,
    TURN_CONFIRM,
    TURN_DENY,
    TURN_EDIT_DRAFT,
    TURN_FILL_SLOT,
    TURN_NAVIGATE,
    TURN_NONE,
    TURN_PRAISE,
    TURN_UNCLEAR,
    TurnDecision,
)
from chat.services.expense.wizard_commands import (
    wants_expense_done_command,
    wants_expense_submit_command,
)
from chat.services.expense_extraction import (
    _AMOUNT_RE,
    _CATEGORY_TOKEN,
    _looks_like_route_answer,
    _split_clauses,
    extract_expense_items,
    normalize_category,
)
from chat.services.expense.slots import STAGE_REVIEW, STAGE_SUBMIT_CONFIRM
from chat.services.llm_client import LLMClient

logger = logging.getLogger("hr_chatbot")

_EDIT_SIGNAL_RE = re.compile(
    r"(?:"
    r"poriborte|poriborto|instead|replace|change\s*kore|"
    r"er\s*jaygay|er\s*jagay|er\s*jaigai|er\s*jaigay|jaigai|jaigay|jaiga|"
    r"take|kore\s*daw|kore\s*de|kore\s*dao|banay|baniye|banao|"
    r"bad\s*daw|baad\s*daw|remove|delete|বাদ|"
    r"\bta\b|\bke\b|\bkoro\b|\bkor\b|\bkore\b|"
    r"hobe|hoy|হবে|update|badle|bodle|poriborto"
    r")",
    re.I | re.UNICODE,
)

_CATEGORY_SWAP_RE = re.compile(
    rf"\b({_CATEGORY_TOKEN}|rail)\s+(?:ta|ke)\s+({_CATEGORY_TOKEN}|rail)\b",
    re.I | re.UNICODE,
)

_EDIT_VERB_RE = re.compile(
    r"\b("
    r"koro|kor|kore|banay|baniye|banao|hobe|hoy|habe|"
    r"jaigai|jaigay|take|badle|bodle|poriborte|change|update|baad|bad"
    r")\b",
    re.I | re.UNICODE,
)

_TURN_LLM_SYSTEM = """You interpret expense WIZARD user messages into STRICT JSON only.

The user is inside an active expense draft wizard — NOT submitting a brand-new claim from scratch
unless they clearly list new costs with amounts.

Return ONLY JSON:
{
  "turn_type": "edit_draft" | "add_lines" | "fill_slot" | "clarify_reply" | "navigate" | "confirm" | "deny" | "unclear",
  "confidence": 0.0 to 1.0,
  "operations": {
    "remove_travel_group": false,
    "replacements": [{"from_category": "Lunch", "to_category": "Snack"}],
    "transfers": [],
    "partial_deducts": [],
    "remove_categories": [],
    "set_amounts": [],
    "add_amounts": []
  },
  "uncertain_note": ""
}

RULES
- turn_type edit_draft: user edits existing draft lines (category swap, amount change, remove line).
  Examples: "lunch er jaigai snack hobe", "bus ta bike hobe", "bus ke bike koro", "lunch take snack kore daw", "bus 70 hobe", "lunch baad daw"
- turn_type add_lines: user adds NEW expense lines with amounts (e.g. "bus 50 office to home", "lunch 100, snack 30")
- turn_type fill_slot: answers a pending from/to or category question
- turn_type clarify_reply: answers a batched typo/clarify prompt
- turn_type navigate: done / joma daw / submit / শেষ
- turn_type confirm / deny: yes / no at review
- Categories: Lunch, Snack, Bus, Rickshaw, Train, Bike, CNG, Metro Rail, Other
- replacements: change label only, keep amount (lunch → snack)
- remove_categories: delete those lines only — NEVER remove lines the user did not mention
- set_amounts: new absolute amount for a category
- Only include operations explicitly requested in the latest message
- If unsure, turn_type unclear and confidence below 0.7
"""


def draft_context_lines(
    items: list[dict[str, Any]],
    *,
    stage: str = "",
    pending_step: str = "",
    pending_line: dict[str, Any] | None = None,
    block: dict[str, Any] | None = None,
    last_question: str = "",
) -> str:
    from chat.services.expense.llm_context import build_wizard_llm_context

    return build_wizard_llm_context(
        items,
        stage=stage,
        pending_step=pending_step,
        pending_line=pending_line,
        block=block,
        last_question=last_question,
    )


def _should_supersede_pending_slot(message: str, *, pending_step: str = "") -> bool:
    """Multi-line / multi-amount input overrides an open category or route slot."""
    step = (pending_step or "").strip().lower()
    if step == "from_to" and _looks_like_route_answer(message):
        return False
    if step == "clarify":
        return False
    text = (message or "").strip()
    if len(_split_clauses(text)) > 1:
        return True
    if len(list(_AMOUNT_RE.finditer(text))) >= 2:
        return True
    ext = extract_expense_items(text)
    return len(ext.items) + len(ext.malformed) > 1


def _message_looks_like_category_edit(
    message: str,
    items: list[dict[str, Any]],
    block: dict[str, Any] | None = None,
) -> bool:
    """Detect category swap attempts regex may miss (e.g. ``bus ke bike koro``)."""
    from chat.services.expense.command_executor import draft_category_names

    low = (message or "").lower().strip()
    draft_cats = draft_category_names(items, block)
    if not low or not draft_cats:
        return False
    if _CATEGORY_SWAP_RE.search(low):
        return True
    tokens = [
        normalize_category(c)
        for c in re.findall(rf"\b({_CATEGORY_TOKEN}|rail)\b", low, re.I)
    ]
    if not tokens or not _EDIT_VERB_RE.search(low):
        return False
    mentioned_draft = [t for t in tokens if t.lower() in draft_cats]
    if mentioned_draft and len(set(tokens)) >= 2:
        return True
    if mentioned_draft and _EDIT_VERB_RE.search(low):
        return True
    return False


def looks_like_draft_edit_signal(
    message: str,
    items: list[dict[str, Any]],
    block: dict[str, Any] | None = None,
) -> bool:
    """Heuristic: message likely edits draft (regex or LLM should run)."""
    from chat.services.expense.command_executor import draft_category_names

    if not draft_category_names(items, block):
        return False
    if looks_like_expense_correction(message):
        return True
    if _message_looks_like_category_edit(message, items, block):
        return True
    low = (message or "").lower().strip()
    if not low:
        return False
    if looks_like_compound_expense_claim(message):
        return False
    cat_tokens = re.findall(rf"\b({_CATEGORY_TOKEN}|rail)\b", low, re.I)
    unique_cats = {normalize_category(c) for c in cat_tokens if c}
    amounts = re.findall(r"\d+(?:[.,]\d{1,2})?", low)
    if not _EDIT_SIGNAL_RE.search(low):
        return False
    if len(unique_cats) >= 2 and not amounts:
        return True
    if unique_cats and len(amounts) <= 1:
        return True
    return False


def _plan_from_llm_turn(data: dict[str, Any] | None) -> CorrectionCommandPlan | None:
    if not isinstance(data, dict):
        return None
    ops = data.get("operations")
    if not isinstance(ops, dict):
        ops = data
    return llm_json_to_correction_plan(ops)


def parse_turn_llm(
    message: str,
    items: list[dict[str, Any]],
    *,
    stage: str = "",
    pending_step: str = "",
    pending_line: dict[str, Any] | None = None,
    block: dict[str, Any] | None = None,
    last_question: str = "",
    trace_id: str = "",
    llm: LLMClient | None = None,
) -> TurnDecision | None:
    text = (message or "").strip()
    if not text:
        return None
    client = llm or LLMClient()
    if not client.is_configured():
        return None

    user_prompt = (
        f"{draft_context_lines(items, stage=stage, pending_step=pending_step, pending_line=pending_line, block=block, last_question=last_question)}\n\n"
        f"User message:\n{text}\n\n"
        "Return JSON only."
    )
    out = client.chat_json(
        system_prompt=_TURN_LLM_SYSTEM,
        user_prompt=user_prompt,
        trace_id=trace_id or "expense-turn-llm",
    )
    if not isinstance(out, dict):
        return None

    turn_type = str(out.get("turn_type") or TURN_UNCLEAR).strip().lower()
    confidence = float(out.get("confidence") or 0.0)
    plan = _plan_from_llm_turn(out) or CorrectionCommandPlan()

    if turn_type == TURN_EDIT_DRAFT and not plan.has_any_correction():
        turn_type = TURN_UNCLEAR
        confidence = min(confidence, 0.5)

    return TurnDecision(
        turn_type=turn_type,
        confidence=confidence,
        plan=plan,
        source="llm",
        uncertain_note=str(out.get("uncertain_note") or "").strip(),
    )


def parse_turn_rules(
    message: str,
    *,
    items: list[dict[str, Any]],
    stage: str,
    pending_step: str,
    has_pending_line: bool,
    pending_line: dict[str, Any] | None = None,
    block: dict[str, Any] | None = None,
) -> TurnDecision:
    text = (message or "").strip()
    if not text:
        return TurnDecision()

    from chat.services.expense.wizard_commands import wants_expense_done_command_rules_only

    if stage in (STAGE_REVIEW, STAGE_SUBMIT_CONFIRM):
        done_cmd = wants_expense_done_command_rules_only(text)
        if wants_expense_submit_command(text) or done_cmd:
            return TurnDecision(
                turn_type=TURN_NAVIGATE,
                confidence=1.0,
                finish_collecting=done_cmd,
                submit_draft=wants_expense_submit_command(text),
                source="rules",
            )
        if is_confirmation_yes(text):
            return TurnDecision(turn_type=TURN_CONFIRM, confidence=1.0, source="rules")
        if is_confirmation_no(text):
            return TurnDecision(turn_type=TURN_DENY, confidence=1.0, source="rules")

    done_cmd = wants_expense_done_command_rules_only(text)
    if wants_expense_submit_command(text) or done_cmd:
        return TurnDecision(
            turn_type=TURN_NAVIGATE,
            confidence=1.0,
            finish_collecting=done_cmd,
            submit_draft=wants_expense_submit_command(text),
            source="rules",
        )

    if stage in (STAGE_REVIEW, STAGE_SUBMIT_CONFIRM):
        from chat.services.expense.clarify_praise import looks_like_wizard_praise_message

        if looks_like_wizard_praise_message(text):
            return TurnDecision(
                turn_type=TURN_PRAISE,
                confidence=0.95,
                source="rules",
            )

    if pending_step == "clarify":
        return TurnDecision(
            turn_type=TURN_CLARIFY_REPLY,
            confidence=0.95,
            source="rules",
        )

    if has_pending_line and pending_step in ("category", "from_to"):
        from chat.services.expense.expense_confirm import looks_like_new_expense_during_pending_slot

        pending = pending_line if isinstance(pending_line, dict) else {}
        if not pending and isinstance(block, dict):
            raw_pending = block.get("pending_line")
            pending = raw_pending if isinstance(raw_pending, dict) else {}
        if looks_like_new_expense_during_pending_slot(
            text, pending, items, block, pending_step=pending_step
        ):
            return TurnDecision(
                turn_type=TURN_FILL_SLOT,
                confidence=0.92,
                source="rules",
            )
        if looks_like_draft_edit_signal(text, items, block) and items is not None:
            plan = parse_correction_plan(text, item_count=len(items or []))
            if plan.has_any_correction():
                return TurnDecision(
                    turn_type=TURN_EDIT_DRAFT,
                    confidence=0.9,
                    plan=plan,
                    source="rules",
                )
            # Edit phrasing while a route/category slot is open — don't treat as slot answer.
            return TurnDecision(
                turn_type=TURN_EDIT_DRAFT,
                confidence=0.55,
                plan=plan,
                source="rules_heuristic",
            )
        if _should_supersede_pending_slot(text, pending_step=pending_step):
            ext = extract_expense_items(text)
            if looks_like_compound_expense_claim(text) or ext.items or ext.malformed:
                return TurnDecision(
                    turn_type=TURN_ADD_LINES,
                    confidence=0.88,
                    source="rules",
                )
            return TurnDecision()
        return TurnDecision(
            turn_type=TURN_FILL_SLOT,
            confidence=0.9,
            source="rules",
        )

    plan = parse_correction_plan(text, item_count=len(items or []))
    if plan.has_any_correction():
        existing_cats = {
            str(row.get("category") or "").lower() for row in items
        }
        # "rickshaw 10 taka, office to road 7" — new line, not amount edit.
        if (
            plan.set_amounts
            and not plan.replacements
            and not plan.remove_verb_first
            and not plan.remove_loose
            and not plan.remove_one
            and not plan.remove_category_suffix
            and all(cat.lower() not in existing_cats for cat, _ in plan.set_amounts)
        ):
            ext = extract_expense_items(text)
            if ext.items:
                return TurnDecision(
                    turn_type=TURN_ADD_LINES,
                    confidence=0.9,
                    source="rules",
                )
        return TurnDecision(
            turn_type=TURN_EDIT_DRAFT,
            confidence=0.95,
            plan=plan,
            source="rules",
        )

    if looks_like_compound_expense_claim(text):
        ext = extract_expense_items(text)
        if ext.items or ext.malformed:
            return TurnDecision(
                turn_type=TURN_ADD_LINES,
                confidence=0.9,
                source="rules",
            )

    if items and re.search(_AMOUNT_RE, text):
        ext = extract_expense_items(text)
        if ext.items and not looks_like_draft_edit_signal(text, items, block):
            return TurnDecision(
                turn_type=TURN_ADD_LINES,
                confidence=0.85,
                source="rules",
            )

    if looks_like_draft_edit_signal(text, items, block):
        return TurnDecision(
            turn_type=TURN_EDIT_DRAFT,
            confidence=0.55,
            plan=plan,
            source="rules_heuristic",
        )

    return TurnDecision()


def resolve_expense_turn(
    message: str,
    *,
    items: list[dict[str, Any]],
    stage: str,
    pending_step: str = "",
    pending_line: dict[str, Any] | None = None,
    has_pending_line: bool = False,
    block: dict[str, Any] | None = None,
    last_question: str = "",
    trace_id: str = "",
    use_llm: bool = True,
) -> TurnDecision:
    """
    Rules-first turn parse; LLM when rules/heuristic suggest edit but plan is empty.
    """
    rules = parse_turn_rules(
        message,
        items=items,
        stage=stage,
        pending_step=pending_step,
        has_pending_line=has_pending_line,
        pending_line=pending_line,
        block=block,
    )

    # Route slot open but user is correcting the draft — try edit/LLM before fill_slot.
    if (
        rules.turn_type == TURN_FILL_SLOT
        and has_pending_line
        and pending_step in ("category", "from_to")
        and looks_like_draft_edit_signal(message, items, block)
    ):
        plan = parse_correction_plan(message, item_count=len(items or []))
        if plan.has_any_correction():
            return TurnDecision(
                turn_type=TURN_EDIT_DRAFT,
                confidence=0.9,
                plan=plan,
                source="rules",
            )
        rules = TurnDecision(
            turn_type=TURN_EDIT_DRAFT,
            confidence=0.55,
            plan=plan,
            source="rules_heuristic",
        )

    if rules.turn_type in (
        TURN_CONFIRM,
        TURN_DENY,
        TURN_NAVIGATE,
        TURN_CLARIFY_REPLY,
        TURN_FILL_SLOT,
        TURN_PRAISE,
    ):
        return rules

    if rules.turn_type == TURN_EDIT_DRAFT and rules.plan.has_any_correction():
        return rules

    if rules.turn_type == TURN_ADD_LINES:
        return rules

    needs_llm = (
        use_llm
        and items
        and (
            rules.turn_type == TURN_EDIT_DRAFT
            or looks_like_draft_edit_signal(message, items, block)
        )
        and not looks_like_compound_expense_claim(message)
    )

    if needs_llm:
        llm_decision = parse_turn_llm(
            message,
            items,
            stage=stage,
            pending_step=pending_step,
            pending_line=pending_line,
            block=block,
            last_question=last_question,
            trace_id=trace_id,
        )
        if llm_decision and llm_decision.turn_type == TURN_EDIT_DRAFT:
            if llm_decision.plan.has_any_correction():
                if llm_decision.confidence >= CONFIDENCE_LLM_FALLBACK:
                    logger.info(
                        "expense_turn_llm_edit trace_id=%s confidence=%s",
                        trace_id,
                        llm_decision.confidence,
                    )
                    return llm_decision
            elif llm_decision.confidence < CONFIDENCE_LLM_FALLBACK:
                llm_decision.turn_type = TURN_UNCLEAR
                return llm_decision

    if rules.is_handled():
        return rules

    return TurnDecision()
