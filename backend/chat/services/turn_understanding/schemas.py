"""Structured output from Turn Understanding Layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

ACT_SLOT_ANSWER = "slot_answer"
ACT_ADD = "add"
ACT_MODIFY = "modify"
ACT_DELETE = "delete"
ACT_SUBMIT = "submit"
ACT_CANCEL = "cancel"
ACT_SUMMARY = "summary"
ACT_QUERY_POLICY = "query_policy"
ACT_QUERY_STATUS = "query_status"
ACT_WORKFLOW_SWITCH = "workflow_switch"
ACT_CHITCHAT = "chitchat"
ACT_OUT_OF_SCOPE = "out_of_scope"
ACT_NEEDS_CLARIFY = "needs_clarify"
ACT_CONTINUE = "continue_wizard"

IN_SCOPE_ACTS = frozenset(
    {
        ACT_SLOT_ANSWER,
        ACT_ADD,
        ACT_MODIFY,
        ACT_DELETE,
        ACT_SUBMIT,
        ACT_CANCEL,
        ACT_SUMMARY,
        ACT_QUERY_POLICY,
        ACT_QUERY_STATUS,
        ACT_WORKFLOW_SWITCH,
        ACT_CHITCHAT,
        ACT_CONTINUE,
        ACT_NEEDS_CLARIFY,
    }
)


@dataclass
class UtteranceResolution:
    primary_act: str = ACT_CONTINUE
    domain: str | None = None
    confidence: float = 0.5
    in_scope: bool = True
    answers_prompt: bool = False
    needs_clarify: bool = False
    clarify_kind: str = ""
    entities: dict[str, Any] = field(default_factory=dict)
    oos_reason: str = ""
    reason: str = ""
    source: str = "rules"

    def is_high_confidence(self) -> bool:
        return self.confidence >= 0.85
