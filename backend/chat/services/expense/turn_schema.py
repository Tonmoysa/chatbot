"""Draft-aware expense wizard turn decision schema (Phases A–D)."""

from __future__ import annotations

from dataclasses import dataclass, field

from chat.services.expense.command_schema import CorrectionCommandPlan

# Turn types — single vocabulary for routing
TURN_NONE = "none"
TURN_EDIT_DRAFT = "edit_draft"
TURN_ADD_LINES = "add_lines"
TURN_FILL_SLOT = "fill_slot"
TURN_CLARIFY_REPLY = "clarify_reply"
TURN_NAVIGATE = "navigate"
TURN_CONFIRM = "confirm"
TURN_DENY = "deny"
TURN_UNCLEAR = "unclear"

CONFIDENCE_LLM_FALLBACK = 0.72


@dataclass
class TurnDecision:
    """One user message interpreted against the current draft + stage."""

    turn_type: str = TURN_NONE
    confidence: float = 1.0
    plan: CorrectionCommandPlan = field(default_factory=CorrectionCommandPlan)
    finish_collecting: bool = False
    submit_draft: bool = False
    source: str = "rules"
    uncertain_note: str = ""

    def is_handled(self) -> bool:
        return self.turn_type != TURN_NONE

    def is_edit(self) -> bool:
        return self.turn_type == TURN_EDIT_DRAFT and self.plan.has_any_correction()


@dataclass
class TurnRouteResult:
    """When handled=True, workflow should return this pack."""

    handled: bool = False
    pack: dict | None = None
