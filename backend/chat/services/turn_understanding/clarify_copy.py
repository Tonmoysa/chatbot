"""Context-aware clarification when utterance is in-scope but ambiguous."""

from __future__ import annotations

from typing import Any

from chat.services.turn_understanding.schemas import UtteranceResolution


def build_utterance_clarification(
    message: str,
    resolution: UtteranceResolution,
    *,
    snapshot: Any = None,
    lang: str | None = None,
) -> str:
    from chat.services.session_expected_answer import build_slot_aware_clarification

    if snapshot is not None:
        slot_msg = build_slot_aware_clarification(message, snapshot, lang=lang)
        if slot_msg:
            return slot_msg

    kind = resolution.clarify_kind or ""
    if kind == "multi_intent":
        return (
            "আপনি একসাথে কয়েকটা বিষয় বলেছেন — একটু আলাদা করে বলবেন?\n\n"
            "উদাহরণ:\n"
            "• leave balance\n"
            "• expense submit\n"
            "• company policy"
        )

    from chat.services.message_context_clarity import build_context_clarification_message

    ctx = tuple(getattr(snapshot, "context_lines", None) or ())
    return build_context_clarification_message(message, list(ctx), lang=lang)
