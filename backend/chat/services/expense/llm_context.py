"""Shared wizard context for expense LLM parsers (P2)."""

from __future__ import annotations

from typing import Any


def build_wizard_llm_context(
    items: list[dict[str, Any]],
    *,
    stage: str = "",
    pending_step: str = "",
    pending_line: dict[str, Any] | None = None,
    block: dict[str, Any] | None = None,
    last_question: str = "",
) -> str:
    """
    Rich context for correction / turn LLM calls.

    Includes stage, pending slot, draft lines, queue, and last assistant prompt.
    """
    lines: list[str] = []
    if stage:
        lines.append(f"Stage: {stage}")
    if pending_step:
        lines.append(f"Pending step: {pending_step}")
    if pending_line and pending_line.get("amount"):
        cat = pending_line.get("category") or "?"
        frm = str(pending_line.get("from_location") or "").strip()
        to = str(pending_line.get("to_location") or "").strip()
        route = f" ({frm} → {to})" if frm or to else ""
        lines.append(
            f"Pending line: {cat} {pending_line.get('amount')} Tk{route}"
        )
    if not items:
        lines.append("Draft lines: (empty)")
    else:
        lines.append("Draft lines:")
        for row in items:
            cat = str(row.get("category") or "?")
            amt = row.get("amount", "?")
            route = ""
            frm, to = row.get("from_location"), row.get("to_location")
            if frm or to:
                route = f" ({frm or '?'} → {to or '?'})"
            lines.append(f"  - {cat}: {amt} Tk{route}")
    if block:
        queue = block.get("pending_queue") or []
        if queue:
            lines.append("Queued lines:")
            for row in queue:
                if not isinstance(row, dict):
                    continue
                lines.append(
                    f"  - {row.get('category') or '?'}: {row.get('amount')} Tk (queued)"
                )
    if last_question:
        q = (last_question or "").strip()
        if q:
            lines.append(f"Last assistant question:\n{q[:1200]}")
    return "\n".join(lines)
