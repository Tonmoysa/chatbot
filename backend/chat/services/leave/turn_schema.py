"""Draft-aware leave wizard turn schema."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

TURN_NONE = "none"
TURN_EDIT_FIELD = "edit_field"
TURN_FILL_SLOT = "fill_slot"
TURN_CONFIRM = "confirm"
TURN_DENY = "deny"
TURN_NAVIGATE = "navigate"
TURN_UNCLEAR = "unclear"

CONFIDENCE_LLM_FALLBACK = 0.72


@dataclass
class LeaveFieldUpdate:
    slot: str
    value: Any = None
    raw_value: str = ""


@dataclass
class LeaveTurnDecision:
    turn_type: str = TURN_NONE
    confidence: float = 1.0
    field_update: LeaveFieldUpdate | None = None
    target_slot: str = ""
    source: str = "rules"
    uncertain_note: str = ""

    def is_handled(self) -> bool:
        return self.turn_type != TURN_NONE
