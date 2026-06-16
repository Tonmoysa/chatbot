"""
Post-mutation ingest guard — block compound re-ingest after travel remove, etc.
"""

from __future__ import annotations

from typing import Any

from chat.services.expense.expense_confirm import (
    is_confirmation_no,
    is_confirmation_yes,
    looks_like_compound_expense_claim,
    looks_like_expense_correction,
)
from chat.services.expense.expense_draft_snapshots import (
    KEY_RESTORE_PENDING,
    wants_restore_expense_version,
)

KEY_INGEST_LOCK = "ingest_lock"
KEY_INGEST_LOCK_REASON = "ingest_lock_reason"

REASON_TRAVEL_REMOVED = "travel_removed"
REASON_CATEGORY_REMOVED = "category_removed"


def set_ingest_lock(
    block: dict[str, Any],
    *,
    reason: str = REASON_TRAVEL_REMOVED,
) -> None:
    block[KEY_INGEST_LOCK] = True
    block[KEY_INGEST_LOCK_REASON] = str(reason or REASON_TRAVEL_REMOVED)


def clear_ingest_lock(block: dict[str, Any]) -> None:
    block.pop(KEY_INGEST_LOCK, None)
    block.pop(KEY_INGEST_LOCK_REASON, None)


def is_ingest_locked(block: dict[str, Any] | None) -> bool:
    return bool((block or {}).get(KEY_INGEST_LOCK))


def ingest_lock_reason(block: dict[str, Any] | None) -> str:
    return str((block or {}).get(KEY_INGEST_LOCK_REASON) or "")


def is_allowed_while_ingest_lock(message: str) -> bool:
    """Single-line adds and explicit corrections still allowed."""
    if wants_restore_expense_version(message):
        return True
    if is_confirmation_yes(message) or is_confirmation_no(message):
        return True
    if looks_like_compound_expense_claim(message):
        return False
    if looks_like_expense_correction(message):
        return True
    return True


def should_block_compound_reingest(
    block: dict[str, Any],
    message: str,
    items: list[dict[str, Any]],
) -> bool:
    """True when a compound expense message must not merge into the draft."""
    if block.get(KEY_RESTORE_PENDING):
        return False
    if is_ingest_locked(block) and not is_allowed_while_ingest_lock(message):
        return True
    return False


def ingest_lock_notice(
    block: dict[str, Any],
    *,
    lang: str | None = None,
) -> str:
    reason = ingest_lock_reason(block)
    if lang == "en":
        if reason == REASON_TRAVEL_REMOVED:
            return (
                "ℹ️ **Travel lines were removed** — I will not auto-add a long compound "
                "expense message again. Say one line at a time (e.g. `lunch 50`) or "
                "**ager thik chilo restore koro** to roll back."
            )
        return (
            "ℹ️ **Draft is locked** against compound re-ingest. "
            "Add one line at a time or use **restore**."
        )
    if lang == "banglish":
        if reason == REASON_TRAVEL_REMOVED:
            return (
                "ℹ️ **Travel remove** hoyeche — baro compound message theke auto-add **bandh**. "
                "Ek line din (e.g. `lunch 50`) ba **ager thik chilo restore koro**."
            )
        return (
            "ℹ️ Draft-e compound re-ingest **lock** ache — ek line kore din ba **restore** korun."
        )
    if reason == REASON_TRAVEL_REMOVED:
        return (
            "ℹ️ **Travel remove** হয়েছে — বড় compound message থেকে আর auto-add হবে না। "
            "এক লাইন করে লিখুন (যেমন: `lunch 50`) অথবা **ager thik chilo restore koro** বলুন।"
        )
    return (
        "ℹ️ Draft-এ compound re-ingest **lock** আছে — এক লাইন করে যোগ করুন বা **restore** করুন।"
    )
