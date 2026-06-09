"""LLM → CorrectionCommandPlan with schema validation (Phase 2.5)."""

from __future__ import annotations

import logging
from typing import Any

from chat.services.expense.command_schema import CorrectionCommandPlan
from chat.services.expense_extraction import normalize_category
from chat.services.llm_client import LLMClient

logger = logging.getLogger("hr_chatbot")

_MAX_OPS = 12
_MAX_AMOUNT = 500_000.0

_CORRECTION_LLM_SYSTEM = """You parse expense REVIEW corrections into STRICT JSON only.

The user is editing an existing expense draft (not submitting a brand-new compound claim).
Return ONLY JSON matching this schema — no prose:

{
  "remove_travel_group": false,
  "replacements": [{"from_category": "Bike", "to_category": "Train"}],
  "transfers": [{"from_category": "Bus", "to_category": "Bike", "amount": 50}],
  "partial_deducts": [{"category": "Lunch", "amount": 20}],
  "remove_categories": ["Train"],
  "set_amounts": [{"category": "Bus", "amount": 50}],
  "add_amounts": [{"category": "Snack", "amount": 30}]
}

RULES
- Categories MUST be one of: Lunch, Snack, Bus, Rickshaw, Train, Bike, CNG, Metro Rail, Other
- Only include operations the user explicitly asked for in the latest message
- remove_travel_group: true only when user removes all travel/transport lines
- replacements: change category label (bike → train), keep amount
- transfers: move amount from one category to another
- partial_deducts: subtract amount from a category without deleting the line
- remove_categories: delete entire line(s) for those categories
- set_amounts: set absolute new amount for a category
- add_amounts: add amount to existing category OR new line if category missing
- Do NOT invent categories or amounts not implied by the user message
- Bangla/Banglish: "bad daw", "remove", "hobe", "na 70" → map to remove/set/transfer
- If the message is not a correction, return empty arrays and remove_travel_group false
"""


def _draft_context_lines(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(empty draft)"
    lines = []
    for row in items:
        cat = str(row.get("category") or "?")
        amt = row.get("amount", "?")
        route = ""
        fr = row.get("from_location")
        to = row.get("to_location")
        if fr or to:
            route = f" ({fr or '?'} → {to or '?'})"
        lines.append(f"- {cat}: {amt} Tk{route}")
    return "\n".join(lines)


def _safe_amount(raw: Any) -> float | None:
    try:
        val = float(str(raw).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None
    if val < 0 or val > _MAX_AMOUNT:
        return None
    return round(val, 2)


def _safe_category(raw: Any) -> str | None:
    if not raw or not str(raw).strip():
        return None
    return normalize_category(str(raw).strip())


def _count_ops(plan: CorrectionCommandPlan) -> int:
    return (
        len(plan.replacements)
        + len(plan.transfers)
        + len(plan.partial_deducts)
        + len(plan.remove_one)
        + len(plan.remove_loose)
        + len(plan.remove_verb_first)
        + len(plan.remove_category_suffix)
        + len(plan.update_amounts)
        + len(plan.set_amounts)
        + len(plan.cat_er_amounts)
        + len(plan.add_amounts)
        + (1 if plan.remove_travel_group else 0)
    )


def llm_json_to_correction_plan(data: dict[str, Any] | None) -> CorrectionCommandPlan | None:
    """Validate LLM JSON and build a CorrectionCommandPlan."""
    if not isinstance(data, dict):
        return None
    plan = CorrectionCommandPlan()

    plan.remove_travel_group = bool(data.get("remove_travel_group"))

    for row in data.get("replacements") or []:
        if not isinstance(row, dict):
            continue
        fr = _safe_category(row.get("from_category") or row.get("from"))
        to = _safe_category(row.get("to_category") or row.get("to"))
        if fr and to and fr.lower() != to.lower():
            plan.replacements.append((fr, to))

    for row in data.get("transfers") or []:
        if not isinstance(row, dict):
            continue
        fr = _safe_category(row.get("from_category") or row.get("from"))
        to = _safe_category(row.get("to_category") or row.get("to"))
        amt = _safe_amount(row.get("amount"))
        if fr and to and amt is not None and fr.lower() != to.lower():
            plan.transfers.append((fr, to, amt))
    plan.has_transfer_pattern = bool(plan.transfers)

    for row in data.get("partial_deducts") or []:
        if not isinstance(row, dict):
            continue
        cat = _safe_category(row.get("category"))
        amt = _safe_amount(row.get("amount"))
        if cat and amt is not None:
            plan.partial_deducts.append((cat, amt))
    plan.has_partial_deduct_pattern = bool(plan.partial_deducts)

    for raw in data.get("remove_categories") or []:
        cat = _safe_category(raw)
        if cat:
            plan.remove_verb_first.append(cat)

    for row in data.get("set_amounts") or []:
        if not isinstance(row, dict):
            continue
        cat = _safe_category(row.get("category"))
        amt = _safe_amount(row.get("amount"))
        if cat and amt is not None:
            plan.set_amounts.append((cat, amt))

    for row in data.get("update_amounts") or []:
        if not isinstance(row, dict):
            continue
        cat = _safe_category(row.get("category"))
        amt = _safe_amount(row.get("amount"))
        if cat and amt is not None:
            plan.update_amounts.append((cat, amt))
    plan.has_update_amount_pattern = bool(plan.update_amounts)

    for row in data.get("add_amounts") or []:
        if not isinstance(row, dict):
            continue
        cat = _safe_category(row.get("category"))
        amt = _safe_amount(row.get("amount"))
        if cat and amt is not None:
            plan.add_amounts.append((cat, amt))

    if not plan.has_any_correction():
        return None
    if _count_ops(plan) > _MAX_OPS:
        return None
    return plan


def parse_correction_plan_llm(
    message: str,
    items: list[dict[str, Any]],
    trace_id: str = "",
    *,
    llm: LLMClient | None = None,
    stage: str = "",
    pending_step: str = "",
    pending_line: dict[str, Any] | None = None,
    block: dict[str, Any] | None = None,
    last_question: str = "",
) -> CorrectionCommandPlan | None:
    """Call LLM to parse a correction plan; None if unavailable or invalid."""
    text = (message or "").strip()
    if not text:
        return None
    client = llm or LLMClient()
    if not client.is_configured():
        return None

    from chat.services.expense.llm_context import build_wizard_llm_context

    user_prompt = (
        f"{build_wizard_llm_context(items, stage=stage, pending_step=pending_step, pending_line=pending_line, block=block, last_question=last_question)}\n\n"
        f"User correction message:\n{text}\n\n"
        "Return JSON only."
    )
    out = client.chat_json(
        system_prompt=_CORRECTION_LLM_SYSTEM,
        user_prompt=user_prompt,
        trace_id=trace_id or "expense-correction-llm",
    )
    plan = llm_json_to_correction_plan(out)
    if plan is None and out is not None:
        logger.info(
            "expense_correction_llm_rejected trace_id=%s",
            trace_id or "expense-correction-llm",
        )
    return plan
