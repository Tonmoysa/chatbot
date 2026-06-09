"""Clarify resolver observability (P3)."""

from __future__ import annotations

from typing import Any

from chat.services.observability import log_step


def log_clarify_resolver(
    trace_id: str,
    *,
    user_message: str = "",
    rules_unresolved: int = 0,
    final_unresolved: int = 0,
    total_issues: int = 0,
    needs_disambiguation: bool = False,
    llm_invoked: bool = False,
    llm_answer_count: int = 0,
    path: str = "rules",
    open_kinds: list[str] | None = None,
) -> None:
    """Structured log for clarify reply resolution (rules / LLM / merge)."""
    if not trace_id:
        return
    log_step(
        trace_id,
        "expense_clarify_resolver",
        {
            "path": path,
            "total_issues": total_issues,
            "rules_unresolved": rules_unresolved,
            "final_unresolved": final_unresolved,
            "needs_disambiguation": needs_disambiguation,
            "llm_invoked": llm_invoked,
            "llm_answer_count": llm_answer_count,
            "open_kinds": open_kinds or [],
            "user_message": user_message,
        },
    )


def resolver_path_label(
    *,
    llm_invoked: bool,
    rules_had_partial: bool,
) -> str:
    if llm_invoked and rules_had_partial:
        return "rules+llm"
    if llm_invoked:
        return "llm"
    return "rules"
