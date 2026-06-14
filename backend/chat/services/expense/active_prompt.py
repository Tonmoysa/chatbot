"""
Unified expense wizard prompt state (one active prompt at a time).

Legacy flags (delete_disambiguation_pending, etc.) are synced for backward compat.
"""

from __future__ import annotations

from typing import Any

_KEY = "active_prompt"

# Prompt kinds
KIND_DELETE_PICK = "delete_pick"
KIND_DELETE_CONFIRM = "delete_confirm"
KIND_ADD_MODIFY_CHOICE = "add_modify_choice"
KIND_MODIFY_TARGET = "modify_target"
KIND_AMOUNT_TARGET = "amount_target"
KIND_FROM_TO = "from_to"
KIND_SUBMIT_CONFIRM = "submit_confirm"
KIND_CATEGORY = "category"


def read_active_prompt(block: dict[str, Any] | None) -> dict[str, Any] | None:
    raw = (block or {}).get(_KEY)
    if isinstance(raw, dict) and raw.get("kind"):
        return dict(raw)
    return None


def active_prompt_kind(block: dict[str, Any] | None) -> str | None:
    p = read_active_prompt(block)
    return str(p.get("kind") or "").strip() or None if p else None


def set_active_prompt(block: dict[str, Any], kind: str, **payload: Any) -> dict[str, Any]:
    block[_KEY] = {"kind": kind, **payload}
    _sync_legacy_flags(block)
    return block


def clear_active_prompt(block: dict[str, Any]) -> dict[str, Any]:
    block.pop(_KEY, None)
    _sync_legacy_flags(block)
    return block


def _sync_legacy_flags(block: dict[str, Any]) -> None:
    """Keep legacy readers working during migration."""
    from chat.services.expense.delete_disambiguation_pending import (
        clear_delete_disambiguation_pending,
        mark_delete_disambiguation_pending,
    )

    kind = active_prompt_kind(block)
    if kind == KIND_DELETE_PICK:
        mark_delete_disambiguation_pending(block)
    else:
        clear_delete_disambiguation_pending(block)


def has_active_expense_prompt(block: dict[str, Any] | None) -> bool:
    return read_active_prompt(block) is not None
