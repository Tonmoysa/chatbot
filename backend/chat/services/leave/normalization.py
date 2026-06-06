"""
Canonical synonym normalization for leave draft fields.

Maps informal Bangla/Banglish/English phrases to stable workflow values.
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.leave_draft_utils import (
    DAY_SCOPE_FULL,
    DAY_SCOPE_HALF,
    LEAVE_PAYMENT_LWOP,
    LEAVE_PAYMENT_PAID,
)

# Sick / medical signals used for leave_type inference and document rules.
SICK_SIGNALS: tuple[str, ...] = (
    "matha betha",
    "mathar betha",
    "pet betha",
    "stomach pain",
    "stomach ache",
    "stomach hurt",
    "fever",
    "headache",
    "পেট ব্যথা",
    "মাথা ব্যথা",
    "জ্বর",
    "অসুস্থ",
    "doctor",
    "ডাক্তার",
    "medical",
    "illness",
    "sick",
)

_PAYMENT_LWOP_RE = re.compile(
    r"\b(lwop|unpaid|without\s+pay|no\s+pay|salary\s+cut|"
    r"বেতন\s*ছাড়া|বেতন\s*ছাড়া|বিনা\s*বেতন|বেতন\s*কাটা)\b",
    re.I,
)
_PAYMENT_PAID_RE = re.compile(
    r"\b(paid|with\s+pay|betonsokh|বেতনসহ|বেতন\s*সহ)\b",
    re.I,
)
_SCOPE_HALF_RE = re.compile(r"\b(half[- ]?day|half|হাফ|অর্ধ\s*দিন)\b", re.I)
_SCOPE_FULL_RE = re.compile(r"\b(full[- ]?day|full|পুরো\s*দিন|সম্পূর্ণ\s*দিন)\b", re.I)


def message_explicitly_states_day_scope(message: str) -> bool:
    """True only when the user clearly said full/half day in this message."""
    text = (message or "").strip()
    if not text:
        return False
    return bool(_SCOPE_HALF_RE.search(text) or _SCOPE_FULL_RE.search(text))


def strip_ungrounded_day_scope(
    entities: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    """Remove invented day_scope unless the message explicitly states full/half."""
    if not entities or message_explicitly_states_day_scope(message):
        return entities
    out = dict(entities)
    out.pop("day_scope", None)
    return out


def _low(text: str) -> str:
    return (text or "").lower()


def text_has_sick_signal(text: str) -> bool:
    low = _low(text)
    return any(sig in low for sig in SICK_SIGNALS)


def normalize_payment_category(value: Any) -> str | None:
    if value is None or value == "":
        return None
    raw = str(value).strip().lower()
    if raw in {LEAVE_PAYMENT_PAID, "pto", "with pay"}:
        return LEAVE_PAYMENT_PAID
    if raw in {LEAVE_PAYMENT_LWOP, "unpaid", "without pay", "no pay"}:
        return LEAVE_PAYMENT_LWOP
    if _PAYMENT_LWOP_RE.search(raw):
        return LEAVE_PAYMENT_LWOP
    if _PAYMENT_PAID_RE.search(raw):
        return LEAVE_PAYMENT_PAID
    return None


def normalize_day_scope(value: Any) -> str | None:
    if value is None or value == "":
        return None
    raw = str(value).strip().lower()
    if raw in {DAY_SCOPE_HALF, "half_day", "half-day"}:
        return DAY_SCOPE_HALF
    if raw in {DAY_SCOPE_FULL, "full_day", "full-day"}:
        return DAY_SCOPE_FULL
    if _SCOPE_HALF_RE.search(raw):
        return DAY_SCOPE_HALF
    if _SCOPE_FULL_RE.search(raw):
        return DAY_SCOPE_FULL
    return None


def infer_leave_type_from_text(*texts: str) -> str | None:
    """Infer sick leave type from reason or message text when not explicitly set."""
    combined = " ".join(t for t in texts if t).strip()
    if not combined:
        return None
    low = _low(combined)
    if text_has_sick_signal(low):
        return "sick"
    if re.search(
        r"\b(casual)\b|ক্যাজুয়াল|নৈমিত্তিক",
        low,
    ):
        return "casual"
    if re.search(r"\b(annual|vacation|pto)\b|বার্ষিক", low):
        return "annual"
    return None


def normalize_reason_text(reason: Any) -> str | None:
    if reason is None:
        return None
    s = str(reason).strip()
    if len(s) < 3:
        return None
    if re.fullmatch(r"famil+y", s, re.I):
        return "family"
    return s[:2000]


def normalize_leave_draft(draft: dict[str, Any]) -> None:
    """
    Apply canonical values to a leave draft in place.

    Never clears existing fields; only normalizes or fills gaps.
    """
    pay = normalize_payment_category(draft.get("leave_payment_category"))
    if pay:
        draft["leave_payment_category"] = pay

    scope = normalize_day_scope(draft.get("day_scope"))
    if scope:
        draft["day_scope"] = scope

    reason = normalize_reason_text(draft.get("reason"))
    if reason:
        draft["reason"] = reason

    last_msg = str(draft.get("_last_user_message") or "")
    combined = " ".join(
        x
        for x in (
            str(draft.get("reason") or ""),
            last_msg,
        )
        if x
    )
    if not draft.get("leave_type"):
        inferred = infer_leave_type_from_text(combined)
        if inferred:
            draft["leave_type"] = inferred

    # Sick type with no explicit reason → implied reason (matches legacy behavior).
    lt = str(draft.get("leave_type") or "").lower()
    if lt in ("sick", "medical") and not draft.get("reason") and draft.get("start_date"):
        draft.setdefault("reason", "অসুস্থতা / sick leave")
        draft["_reason_implied"] = True
