"""
Expense draft version history — snapshots + restore menu (undo).
"""

from __future__ import annotations

import re
from typing import Any

from chat.services.expense.expense_fsm import read_expense_block
from chat.services.expense.slots import STAGE_COLLECTING, STAGE_REVIEW
from chat.services.expense_extraction import is_travel_category

KEY_EXPENSE_DRAFT_SNAPSHOTS = "expense_draft_snapshots"
KEY_RESTORE_PENDING = "expense_restore_pending"

_MAX_SNAPSHOTS = 12

_ACTION_LABELS_EN = {
    "initial_review": "Original claim",
    "after_line_add": "After new lines",
    "before_correction": "Before correction",
    "after_correction": "After update",
    "after_travel_remove": "After travel remove",
    "after_transfer": "After amount transfer",
    "current_before_restore": "Current (before restore)",
}

_ACTION_LABELS_BN = {
    "initial_review": "শুরুর claim",
    "after_line_add": "নতুন line যোগের পর",
    "before_correction": "সংশোধনের আগে",
    "after_correction": "আপডেটের পর",
    "after_travel_remove": "travel remove-এর পর",
    "after_transfer": "amount transfer-এর পর",
    "current_before_restore": "বর্তমান (restore-এর আগে)",
}


def items_fingerprint(items: list[dict[str, Any]]) -> str:
    parts = []
    for row in sorted(items, key=lambda x: str(x.get("category") or "")):
        cat = str(row.get("category") or "").lower()
        try:
            amt = round(float(row.get("amount") or 0), 2)
        except (TypeError, ValueError):
            amt = 0.0
        parts.append(f"{cat}:{amt:g}")
    return "|".join(parts)


def _batch_total(items: list[dict[str, Any]]) -> float:
    return sum(float(x.get("amount") or 0) for x in items)


def _brief_items_label(items: list[dict[str, Any]], *, max_parts: int = 4) -> str:
    parts = []
    for row in items[:max_parts]:
        cat = str(row.get("category") or "Other")
        amt = float(row.get("amount") or 0)
        parts.append(f"{cat} {amt:g}")
    if len(items) > max_parts:
        parts.append(f"+{len(items) - max_parts} more")
    return ", ".join(parts) if parts else "—"


def _auto_label(
    items: list[dict[str, Any]],
    action_type: str,
    *,
    lang: str | None = None,
) -> str:
    brief = _brief_items_label(items)
    total = _batch_total(items)
    if lang == "en":
        action = _ACTION_LABELS_EN.get(action_type, action_type.replace("_", " "))
    else:
        action = _ACTION_LABELS_BN.get(action_type, action_type.replace("_", " "))
    return f"{action} — {brief} · {total:g} Tk"


def read_snapshots(workflow_state: dict[str, Any] | None) -> list[dict[str, Any]]:
    raw = (workflow_state or {}).get(KEY_EXPENSE_DRAFT_SNAPSHOTS)
    return [dict(x) for x in raw] if isinstance(raw, list) else []


def is_awaiting_restore_selection(block: dict[str, Any]) -> bool:
    return bool(block.get(KEY_RESTORE_PENDING))


def clear_restore_pending(block: dict[str, Any]) -> None:
    block.pop(KEY_RESTORE_PENDING, None)


def wants_restore_expense_version(message: str) -> bool:
    """User wants to roll back to a previous expense draft — not resume where they left off."""
    raw = (message or "").strip()
    if not raw:
        return False
    low = raw.lower()
    if re.search(
        r"(?:"
        r"ager\s+(?:ta|data|information|info|tothyo|তথ্য).{0,40}(?:thik|sothik|correct|ঠিক)"
        r"|(?:thik|sothik|correct|ঠিক).{0,30}ager"
        r"|otatei\s+back|ager\s+ta\s+thik|ager\s+ei\s+ta"
        r"|previous\s+version|undo|restore"
        r"|আগেরটা|আগের\s*টা|আগের\s*ভার্সন"
        r"|ager\s+(?:version|v|stage)"
        r")",
        low,
        re.I | re.UNICODE,
    ):
        return True
    if re.search(r"\b(ager|আগের|previous)\b", low) and re.search(
        r"\b(back|ferot|restore|undo|thik|ঠিক|sothik|chilo|chil)\b", low, re.I
    ):
        return True
    return False


def parse_restore_selection(
    message: str,
    snapshots: list[dict[str, Any]],
    *,
    current_fingerprint: str = "",
) -> int | None:
    """Return 1-based index into snapshots list, or None."""
    raw = (message or "").strip()
    if not raw or not snapshots:
        return None
    low = raw.lower()

    if re.search(r"\b(cancel|বাতিল|thik ache|এখনকার|current|keep)\b", low):
        return -1

    m = re.match(r"^(?:option\s*)?(\d+)\s*\.?$", low)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= len(snapshots):
            return idx

    if re.search(r"\b(original|prothom|first|শুরু|initial)\b", low):
        return 1

    if re.search(r"\b(last|previous|shesh|আগের)\b", low) and len(snapshots) >= 2:
        return len(snapshots)

    for i, snap in enumerate(snapshots, start=1):
        label = str(snap.get("label") or "").lower()
        if label and label in low:
            return i
        fp = str(snap.get("fingerprint") or "")
        if fp and fp == current_fingerprint:
            continue
        for token in re.findall(r"[a-zA-Z\u0980-\u09FF]+", label):
            if len(token) >= 4 and token.lower() in low:
                return i
    return None


def push_expense_snapshot(
    workflow_state: dict[str, Any],
    *,
    items: list[dict[str, Any]],
    stage: str,
    action_type: str,
    incurred_date_iso: str = "",
    lang: str | None = None,
    label: str = "",
) -> dict[str, Any]:
    """Append a draft snapshot if it differs from the latest."""
    wf = dict(workflow_state or {})
    items_copy = [dict(x) for x in items]
    fp = items_fingerprint(items_copy)
    snaps = read_snapshots(wf)
    if snaps and str(snaps[-1].get("fingerprint") or "") == fp:
        return wf

    seq = len(snaps) + 1
    entry: dict[str, Any] = {
        "id": f"snap-{seq}",
        "seq": seq,
        "items": items_copy,
        "stage": str(stage or STAGE_COLLECTING),
        "total": _batch_total(items_copy),
        "fingerprint": fp,
        "action_type": str(action_type or "snapshot"),
        "label": label or _auto_label(items_copy, action_type, lang=lang),
        "incurred_date_iso": str(incurred_date_iso or "").strip(),
    }
    snaps.append(entry)
    wf[KEY_EXPENSE_DRAFT_SNAPSHOTS] = snaps[-_MAX_SNAPSHOTS:]
    return wf


def format_restore_menu(
    snapshots: list[dict[str, Any]],
    *,
    lang: str | None = None,
    current_items: list[dict[str, Any]] | None = None,
) -> str:
    cur_fp = items_fingerprint(list(current_items or []))
    if lang == "en":
        head = (
            "**Which expense draft version should I restore?**\n"
            "Reply with the **number** (e.g. `1`), or `cancel` to keep the current draft.\n"
        )
        current_line = " _(current)_"
    elif lang == "banglish":
        head = (
            "**Kon expense draft version restore korbo?**\n"
            "**Number** din (e.g. `1`), na `cancel` dile ekhonkar draft thakbe.\n"
        )
        current_line = " _(current)_"
    else:
        head = (
            "**কোন expense draft version-এ ফিরে যাব?**\n"
            "**নম্বর** লিখুন (যেমন: `1`), নাহলে `cancel` দিলে বর্তমান draft থাকবে।\n"
        )
        current_line = " _(বর্তমান)_"

    lines = [head, ""]
    for i, snap in enumerate(snapshots, start=1):
        label = str(snap.get("label") or f"Version {i}")
        total = float(snap.get("total") or 0)
        mark = current_line if str(snap.get("fingerprint") or "") == cur_fp else ""
        lines.append(f"**{i})** {label} — **{total:g} Tk**{mark}")
    return "\n".join(lines)


def restore_cancel_notice(*, lang: str | None = None) -> str:
    if lang == "en":
        return "Keeping your **current** expense draft unchanged."
    if lang == "banglish":
        return "Apnar **current** expense draft same rekhechi."
    return "আপনার **বর্তমান** expense draft **আগের মতোই** রেখেছি।"


def restore_applied_notice(
    snapshot: dict[str, Any], *, lang: str | None = None
) -> str:
    label = str(snapshot.get("label") or "")
    total = float(snapshot.get("total") or 0)
    if lang == "en":
        return f"Restored expense draft: **{label}** (total **{total:g} Tk**)."
    if lang == "banglish":
        return f"Expense draft restore kora hoyeche: **{label}** (mot **{total:g} Tk**)."
    return f"Expense draft **restore** করা হয়েছে: **{label}** (মোট **{total:g} Tk**)."


def restore_unavailable_notice(*, lang: str | None = None) -> str:
    if lang == "en":
        return (
            "I do not have an earlier expense draft version saved in this session yet. "
            "Make a change first (or complete a review), then you can restore."
        )
    if lang == "banglish":
        return (
            "Session-e ager expense draft version save nai. "
            "Age ekta change korun, tarpor restore korte parben."
        )
    return (
        "এই session-এ আগের expense draft version **সেভ নেই**। "
        "আগে একটা পরিবর্তন করুন, তারপর restore করা যাবে।"
    )


def restore_pick_notice(*, lang: str | None = None) -> str:
    if lang == "en":
        return "Please pick a **number** from the list above, or type **cancel**."
    if lang == "banglish":
        return "Uparer list theke **number** din, ba **cancel** likhun."
    return "উপরের তালিকা থেকে **নম্বর** দিন, অথবা **cancel** লিখুন।"


def apply_snapshot_to_block(
    block: dict[str, Any],
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    items = [dict(x) for x in list(snapshot.get("items") or [])]
    block["items"] = items
    block["stage"] = str(snapshot.get("stage") or STAGE_REVIEW)
    block.pop("pending_line", None)
    block.pop("pending_step", None)
    block.pop("clarification_issues", None)
    from chat.services.expense.expense_ingest_guard import clear_ingest_lock

    clear_ingest_lock(block)
    clear_restore_pending(block)
    if str(snapshot.get("incurred_date_iso") or "").strip():
        block["incurred_date_iso"] = snapshot["incurred_date_iso"]
    return items


def snapshots_for_restore_menu(
    workflow_state: dict[str, Any] | None,
    current_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Distinct prior versions (newest last), excluding empty."""
    snaps = read_snapshots(workflow_state)
    cur_fp = items_fingerprint(current_items)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for snap in snaps:
        fp = str(snap.get("fingerprint") or "")
        if not fp or fp in seen:
            continue
        seen.add(fp)
        out.append(snap)
    if len(out) > 1 and str(out[-1].get("fingerprint") or "") == cur_fp:
        pass
    return out[-_MAX_SNAPSHOTS:]


def has_travel_lines(items: list[dict[str, Any]]) -> bool:
    return any(is_travel_category(str(x.get("category") or "")) for x in items)
