"""
Session-aware expense ledger — submitted batches, pending draft, totals, cap.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from chat.constants import EXPENSE_DAY_CAP_BDT
from chat.services.expense.expense_draft_snapshots import (
    KEY_RESTORE_PENDING,
    items_fingerprint,
    read_snapshots,
)
from chat.services.expense.expense_fsm import (
    KEY_EXPENSE_LAST_SUBMISSION,
    read_expense_block,
)
from chat.services.expense.session_action_memory import (
    KEY_BOT_ACTION_LOG,
    KEY_LAST_BOT_ACTION,
)
from chat.services.expense_workflow import _format_line_display
from chat.services.workflow_suspend import KEY_SUSPENDED_EXPENSE

KEY_EXPENSE_SUBMISSIONS_HISTORY = "expense_submissions_history"


from chat.services.expense_workflow import message_mentions_expense_spend


def wants_recent_expense_recall_query(message: str) -> bool:
    """
    User asks what expense they submitted/added on a past day (voice/Bengali).

    E.g. ``লাস্ট দিনে কোন এক্সপেন্স দিছিলাম আমি``.
    """
    raw = (message or "").strip()
    if not raw or not message_mentions_expense_spend(raw):
        return False
    low = raw.lower()
    past_time = bool(
        re.search(
            r"(?:"
            r"লাস্ট\s*দিন|গত\s*দিন|previous\s*day|last\s*day|yesterday|"
            r"goto\s*kal|gata\s*kal|gato\s*kal|গতকাল|কালকে|কাল\s*কে"
            r")",
            raw,
            re.I | re.UNICODE,
        )
    )
    recall_verb = bool(
        re.search(
            r"(?:"
            r"দিছিলাম|দিয়েছি|দিছি|দিয়েছিলাম|"
            r"diyechi|dilam|diyechilam|korsilam|korechilam|korsi|"
            r"submit|জমা|জমা\s*দিয়|add\s*kore|এড\s*কর"
            r")",
            raw,
            re.I | re.UNICODE,
        )
    )
    which_what = bool(
        re.search(r"\b(which|what|ki|kono)\b", low)
        or re.search(r"(কোন|কী|কি)", raw)
    )
    if past_time and (recall_verb or which_what):
        return True
    if which_what and recall_verb:
        return True
    return False


def wants_pending_expense_query(message: str) -> bool:
    """
    User asks whether any pending/unsubmitted expense draft exists in this session.

    E.g. ``pending kono expense ache tomar kache?``, ``amar kache pending expense ache ki?``
    """
    raw = (message or "").strip()
    if not raw:
        return False
    low = raw.lower()
    has_pending = bool(
        re.search(
            r"\b(pending|draft|unsubmitted|parked|collecting)\b",
            low,
        )
        or re.search(
            r"(পেন্ডিং|ড্রাফট|জমা\s*হয়নি|জমা\s*হয়নি|হয়নি|হয়নি)",
            raw,
            re.I | re.UNICODE,
        )
    )
    if not has_pending:
        return False
    has_expense_domain = bool(
        message_mentions_expense_spend(raw)
        or re.search(r"\b(kharcha|khoroch|draft|line)\b", low)
        or re.search(r"(খরচ|ড্রাফট|লাইন)", raw, re.I | re.UNICODE)
    )
    if not has_expense_domain:
        return False
    if re.search(r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?(?!\d)", raw):
        try:
            from chat.services.expense_extraction import extract_expense_items

            ext = extract_expense_items(raw)
            if ext.items:
                return False
        except Exception:
            pass
    return bool(
        re.search(
            r"(?:"
            r"\bache\b|\base\b|\bexists?\b|\bhave\b|\bany\b|\bkono\b|"
            r"\bki\b|\bwhat\b|\bshow\b|\blist\b|"
            r"tomar\s+kache|amar\s+kache|your|session|"
            r"কোন|কি|আছে|আছে\s*কি|তোমার\s*কাছে|আমার\s*কাছে|"
            r"\b(daw|dao|dekhao|bolo|বল|দাও|দেখ)"
            r")",
            low,
            re.I,
        )
        or re.search(
            r"(তোমার\s*কাছে|আমার\s*কাছে|session)",
            raw,
            re.I | re.UNICODE,
        )
    )


def wants_session_expense_ledger_query(message: str) -> bool:
    """User asks for same-day spend recap / history / how much they added."""
    raw = message or ""
    low = raw.lower()
    if wants_recent_expense_recall_query(raw):
        return True
    if wants_pending_expense_query(raw):
        return True
    if not message_mentions_expense_spend(raw):
        return False

    if re.search(r"\b(history|histori|record|ledger)\b", low) or re.search(
        r"(ইতিহাস|হিস্টোরি|history)", raw, re.I
    ):
        return True
    if re.search(
        r"\b(add|adding|added|korchi|korci|korechi|korsilam|korsi|dilam|diyechi|diyeci)\b",
        low,
    ) and re.search(r"\b(koto|total|mot|koy|how\s+much)\b", low):
        return True
    if re.search(
        r"(এড|যোগ|add|করছি|korchi|korci|korechi)",
        raw,
        re.I | re.UNICODE,
    ) and re.search(
        r"(?:\b(?:কত|koto|total|mot|how\s+much)\b|মোট)",
        raw,
        re.I | re.UNICODE,
    ):
        return True
    if re.search(r"\bsara\s+din\b", low) and re.search(
        r"\b(koto|total|mot|cost|kharcha|khoroch|expense)\b", low
    ):
        return True
    return False


def _normalize_date(iso: str) -> str:
    return str(iso or "").strip().split("T")[0]


def _batch_total(items: list[dict[str, Any]]) -> float:
    return sum(float(x.get("amount") or 0) for x in items)


def draft_line_rows_for_block(block: dict[str, Any]) -> list[dict[str, Any]]:
    """
    All expense lines visible in the collecting wizard: committed items plus
    the open pending_line and pending_queue entries (not yet in items).
    """
    rows = [dict(x) for x in list(block.get("items") or [])]
    pending = block.get("pending_line")
    if isinstance(pending, dict) and pending.get("amount"):
        rows.append(dict(pending))
    for row in list(block.get("pending_queue") or []):
        if isinstance(row, dict) and row.get("amount"):
            rows.append(dict(row))
    return rows


def _batch_from_record(record: dict[str, Any], *, source: str) -> dict[str, Any]:
    items = [dict(x) for x in list(record.get("items") or [])]
    return {
        "reference_id": str(record.get("reference_id") or record.get("request_id") or ""),
        "items": items,
        "total": _batch_total(items) if items else float(record.get("amount") or 0),
        "incurred_date_iso": _normalize_date(str(record.get("incurred_date_iso") or "")),
        "source": source,
    }


def _batches_from_crm(breakdown: dict[str, Any], target_date: str) -> list[dict[str, Any]]:
    entries = list(breakdown.get("expense_day_entries") or [])
    flat_items = [dict(x) for x in list(breakdown.get("expense_day_items") or [])]
    batches: list[dict[str, Any]] = []
    offset = 0
    for entry in entries:
        rid = str(entry.get("request_id") or "")
        line_count = int(entry.get("line_count") or 0)
        if line_count > 0 and offset + line_count <= len(flat_items):
            batch_items = flat_items[offset : offset + line_count]
            offset += line_count
        elif len(entries) == 1:
            batch_items = flat_items
        else:
            batch_items = []
        amt = float(entry.get("amount") or 0)
        if not batch_items and amt > 0:
            total = amt
        else:
            total = _batch_total(batch_items) if batch_items else amt
        batches.append(
            {
                "reference_id": rid,
                "items": batch_items,
                "total": total,
                "incurred_date_iso": target_date,
                "source": "crm",
            }
        )
    if not batches and flat_items:
        ref = str(breakdown.get("expense_summary_reference_id") or "")
        batches.append(
            {
                "reference_id": ref,
                "items": flat_items,
                "total": _batch_total(flat_items),
                "incurred_date_iso": target_date,
                "source": "crm",
            }
        )
    return batches


def _batches_from_session(workflow_state: dict[str, Any], target_date: str) -> list[dict[str, Any]]:
    wf = workflow_state or {}
    batches: list[dict[str, Any]] = []
    history = list(wf.get(KEY_EXPENSE_SUBMISSIONS_HISTORY) or [])
    if history:
        for rec in history:
            if not isinstance(rec, dict):
                continue
            inc = _normalize_date(str(rec.get("incurred_date_iso") or ""))
            if inc and inc != target_date:
                continue
            batches.append(_batch_from_record(rec, source="session"))
    else:
        last = wf.get(KEY_EXPENSE_LAST_SUBMISSION) or {}
        if isinstance(last, dict) and last.get("items"):
            inc = _normalize_date(str(last.get("incurred_date_iso") or ""))
            if not inc or inc == target_date:
                batches.append(_batch_from_record(last, source="session"))
    return batches


def _submission_fingerprint(batch: dict[str, Any]) -> tuple[str, str, float]:
    """Same-day + same line items = same submission (even if refs differ)."""
    date_iso = _normalize_date(str(batch.get("incurred_date_iso") or ""))
    items = list(batch.get("items") or [])
    fp = items_fingerprint(items) if items else ""
    total = round(float(batch.get("total") or _batch_total(items)), 2)
    return (date_iso, fp, total)


def _prefer_batch_ref(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Prefer EXP-* / session source over generic MOCK-* duplicates."""
    ra = str(a.get("reference_id") or "")
    rb = str(b.get("reference_id") or "")
    if ra.startswith("EXP-") and not rb.startswith("EXP-"):
        return a
    if rb.startswith("EXP-") and not ra.startswith("EXP-"):
        return b
    if a.get("source") == "session" and b.get("source") != "session":
        return a
    if b.get("source") == "session" and a.get("source") != "session":
        return b
    if ra.startswith("MOCK-") and not rb.startswith("MOCK-"):
        return b
    if rb.startswith("MOCK-") and not ra.startswith("MOCK-"):
        return a
    return a if len(ra) >= len(rb) else b


def _merge_submitted_batches(
    crm_batches: list[dict[str, Any]],
    session_batches: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    seen_fps: dict[tuple[str, str, float], int] = {}

    def _add_batch(batch: dict[str, Any]) -> None:
        b = dict(batch)
        ref = str(b.get("reference_id") or "")
        fp = _submission_fingerprint(b)
        has_items = bool(b.get("items"))

        if fp[1] and fp in seen_fps:
            idx = seen_fps[fp]
            existing = merged[idx]
            if not existing.get("items") and has_items:
                existing["items"] = list(b["items"])
                existing["total"] = _batch_total(existing["items"])
                b = dict(existing)
            preferred = _prefer_batch_ref(existing, b)
            merged[idx] = dict(preferred)
            return

        if ref and ref in seen_refs:
            for row in merged:
                if row.get("reference_id") == ref and not row.get("items") and has_items:
                    row["items"] = list(b["items"])
                    row["total"] = _batch_total(row["items"])
            return

        idx = len(merged)
        merged.append(b)
        if ref:
            seen_refs.add(ref)
        if fp[1]:
            seen_fps[fp] = idx

    for cb in crm_batches:
        _add_batch(cb)
    for sb in session_batches:
        _add_batch(sb)
    return merged


def _build_session_context(workflow_state: dict[str, Any] | None) -> dict[str, Any]:
    """Footnote metadata: last action, ingest lock, snapshots."""
    wf = workflow_state or {}
    block = read_expense_block(wf)
    last_action = wf.get(KEY_LAST_BOT_ACTION)
    action_log = wf.get(KEY_BOT_ACTION_LOG)
    recent: list[str] = []
    if isinstance(action_log, list):
        for entry in action_log[-3:]:
            if isinstance(entry, dict):
                summary = str(entry.get("summary") or "").strip()
                if summary:
                    recent.append(summary)
    last_summary = ""
    if isinstance(last_action, dict):
        last_summary = str(last_action.get("summary") or "").strip()

    return {
        "ingest_lock": bool(block.get("ingest_lock")),
        "ingest_lock_reason": str(block.get("ingest_lock_reason") or ""),
        "restore_pending": bool(block.get(KEY_RESTORE_PENDING)),
        "last_action_summary": last_summary,
        "recent_actions": recent,
        "snapshot_count": len(read_snapshots(wf)),
        "draft_stage": str(block.get("stage") or ""),
    }


def format_ledger_footnotes(
    ledger: dict[str, Any],
    *,
    lang: str | None = None,
) -> str:
    """Session context lines appended to day summary / history."""
    ctx = ledger.get("session_context") or {}
    if not ctx:
        return ""

    lines: list[str] = []
    last = str(ctx.get("last_action_summary") or "").strip()
    recent = list(ctx.get("recent_actions") or [])

    if last:
        if lang == "en":
            lines.append(f"📝 **Last action:** {last}")
        else:
            lines.append(f"📝 **সাম্প্রতিক action:** {last}")

    if ctx.get("ingest_lock"):
        from chat.services.expense.expense_ingest_guard import ingest_lock_notice

        lines.append(
            ingest_lock_notice(
                {
                    "ingest_lock": True,
                    "ingest_lock_reason": ctx.get("ingest_lock_reason"),
                },
                lang=lang,
            )
        )

    if ctx.get("restore_pending"):
        if lang == "en":
            lines.append("⏸ **Restore menu open** — pick a version number or `cancel`.")
        else:
            lines.append("⏸ **Restore menu** খোলা — নম্বর বাছুন বা `cancel` দিন।")

    extra_recent = [s for s in recent if s != last]
    if extra_recent:
        if lang == "en":
            lines.append("🕓 **Earlier this session:**")
        else:
            lines.append("🕓 **এই session-এ আগে:**")
        for summary in extra_recent:
            lines.append(f"   - {summary}")

    snap_n = int(ctx.get("snapshot_count") or 0)
    if snap_n >= 2 and not ctx.get("restore_pending"):
        if lang == "en":
            lines.append(
                f"↩ **{snap_n} saved draft versions** — say **ager thik chilo restore koro** to roll back."
            )
        else:
            lines.append(
                f"↩ **{snap_n} টি draft version** সেভ আছে — ফিরতে **ager thik chilo restore koro** বলুন।"
            )

    if not lines:
        return ""
    return "\n\n" + "\n".join(lines)


def iter_session_expense_blocks(
    workflow_state: dict[str, Any] | None,
) -> list[tuple[str, dict[str, Any]]]:
    """Active expense wizard block, then suspended snapshot (if any)."""
    wf = workflow_state or {}
    out: list[tuple[str, dict[str, Any]]] = []
    block = read_expense_block(wf)
    if block.get("active") or draft_line_rows_for_block(block):
        out.append(("active", block))
    se = wf.get(KEY_SUSPENDED_EXPENSE) or {}
    sblock = (
        se.get("expense_request")
        if isinstance(se, dict) and "expense_request" in se
        else se
    )
    if isinstance(sblock, dict) and (
        sblock.get("active") or draft_line_rows_for_block(sblock)
    ):
        out.append(("suspended", sblock))
    return out


def line_incompleteness_notes(row: dict[str, Any]) -> list[str]:
    """Human notes for summary — what is still missing on a draft line."""
    from chat.services.expense_extraction import is_travel_category

    notes: list[str] = []
    cat = str(row.get("category") or "").strip()
    if not cat:
        notes.append("category এখনো দেওয়া হয়নি")
        return notes
    if is_travel_category(cat):
        frm = str(row.get("from_location") or "").strip()
        to = str(row.get("to_location") or "").strip()
        if not frm and not to:
            notes.append("From/To location এখনো দেওয়া হয়নি")
        elif frm and not to:
            notes.append(f"To location দেওয়া হয়নি (From: {frm})")
        elif to and not frm:
            notes.append(f"From location দেওয়া হয়নি (To: {to})")
    return notes


def _format_pending_row_display(row: dict[str, Any]) -> list[str]:
    lines = [_format_line_display(row).lstrip("- ")]
    for note in line_incompleteness_notes(row):
        lines.append(f"      ⚠ {note}")
    return lines


def infer_session_expense_summary_date(
    workflow_state: dict[str, Any] | None,
    *,
    message: str,
    hints: dict[str, Any] | None = None,
    today: date | None = None,
) -> str:
    """
    Date for expense summary — prefer session draft dates over leave-entity hints.

    While leave is active, upstream entity extraction may set ``date`` to the leave
    start date; that must not override a parked expense draft's incurred date.
    """
    from chat.services.expense_incurred_date import infer_expense_incurred_date_iso

    today_d = today or date.today()
    raw = message or ""
    low = raw.lower()
    if re.search(
        r"\b(today|ajke|ajker|aj\s+ke|eikhon|ei\s+din|yesterday|kalke|tomorrow|kal|"
        r"last\s+day|previous\s+day|goto\s*kal|gata\s*kal)\b",
        low,
    ) or re.search(
        r"(আজ|আজকে|গতকাল|আগামীকাল|কাল|লাস্ট\s*দিন|গত\s*দিন)",
        raw,
        re.I | re.UNICODE,
    ):
        return infer_expense_incurred_date_iso(
            message=raw,
            hints={"expense_incurred_date": (hints or {}).get("expense_incurred_date")},
            today=today_d,
        )
    if re.search(r"\d{4}-\d{1,2}-\d{1,2}", raw):
        return infer_expense_incurred_date_iso(message=raw, hints={}, today=today_d)

    draft_dates: list[str] = []
    for _source, block in iter_session_expense_blocks(workflow_state):
        if not draft_line_rows_for_block(block):
            continue
        inc = _normalize_date(str(block.get("incurred_date_iso") or ""))
        if inc:
            draft_dates.append(inc)
    if len(set(draft_dates)) == 1:
        return draft_dates[0]
    if draft_dates:
        return draft_dates[0]

    clean_hints: dict[str, Any] = {}
    exp_hint = (hints or {}).get("expense_incurred_date")
    if exp_hint:
        clean_hints["expense_incurred_date"] = exp_hint
    return infer_expense_incurred_date_iso(
        message=raw,
        hints=clean_hints,
        today=today_d,
    )


def _read_pending_draft(
    workflow_state: dict[str, Any], target_date: str
) -> dict[str, Any] | None:
    wf = workflow_state or {}
    candidates = iter_session_expense_blocks(wf)
    fallback: dict[str, Any] | None = None

    for source, block in candidates:
        rows = draft_line_rows_for_block(block)
        if not rows:
            continue
        inc = _normalize_date(str(block.get("incurred_date_iso") or ""))
        payload = {
            "items": rows,
            "total": _batch_total(rows),
            "stage": str(block.get("stage") or ""),
            "source": source,
            "incurred_date_iso": inc or target_date,
            "pending_step": str(block.get("pending_step") or ""),
            "ingest_lock": bool(block.get("ingest_lock")),
            "ingest_lock_reason": str(block.get("ingest_lock_reason") or ""),
        }
        if inc and inc != target_date:
            if fallback is None:
                fallback = payload
            continue
        return payload
    return fallback


def session_has_expense_draft_data(workflow_state: dict[str, Any] | None) -> bool:
    for _source, block in iter_session_expense_blocks(workflow_state):
        if draft_line_rows_for_block(block):
            return True
    return False


def format_no_session_expense_summary_message(*, lang: str = "bn") -> str:
    if lang == "en":
        return (
            "No expense draft or submission was found in this session.\n\n"
            "To start a claim, write e.g. `lunch 100, bus 50 office to badda`."
        )
    return (
        "এই session-এ কোনো expense draft বা submission পাওয়া যায়নি।\n\n"
        "নতুন খরচ জমা দিতে লিখুন — যেমন: `lunch 100, bus 50 office to badda`"
    )


def build_session_expense_ledger(
    workflow_state: dict[str, Any] | None,
    *,
    crm_breakdown: dict[str, Any] | None,
    incurred_date_iso: str,
    daily_cap: float = EXPENSE_DAY_CAP_BDT,
) -> dict[str, Any]:
    target = _normalize_date(incurred_date_iso)
    breakdown = dict(crm_breakdown or {})
    crm_batches = _batches_from_crm(breakdown, target)
    session_batches = _batches_from_session(workflow_state or {}, target)
    submitted = _merge_submitted_batches(crm_batches, session_batches)
    pending = _read_pending_draft(workflow_state or {}, target)
    if pending:
        p_inc = _normalize_date(str(pending.get("incurred_date_iso") or ""))
        if p_inc and p_inc != target:
            target = p_inc
            crm_batches = _batches_from_crm(breakdown, target)
            session_batches = _batches_from_session(workflow_state or {}, target)
            submitted = _merge_submitted_batches(crm_batches, session_batches)

    submitted_total = sum(float(b.get("total") or 0) for b in submitted)
    pending_total = float((pending or {}).get("total") or 0)
    combined_total = submitted_total + pending_total
    cap = float(breakdown.get("expense_daily_cap_bdt") or daily_cap)
    crm_logged = float(breakdown.get("expense_day_logged_total") or 0)
    if crm_logged > submitted_total:
        submitted_total = crm_logged

    session_context = _build_session_context(workflow_state)

    return {
        "incurred_date_iso": target,
        "submitted_batches": submitted,
        "pending_draft": pending,
        "submitted_total": submitted_total,
        "pending_total": pending_total,
        "combined_total": combined_total if combined_total else submitted_total,
        "daily_cap": cap,
        "over_cap": combined_total > cap if combined_total else submitted_total > cap,
        "remaining_under_cap": max(0.0, cap - submitted_total),
        "session_context": session_context,
    }


def format_session_expense_ledger_message(ledger: dict[str, Any]) -> str:
    """Human-readable session expense history with submitted / pending / totals."""
    date_iso = str(ledger.get("incurred_date_iso") or "").strip() or "আজ"
    submitted = list(ledger.get("submitted_batches") or [])
    pending = ledger.get("pending_draft")
    submitted_total = float(ledger.get("submitted_total") or 0)
    pending_total = float(ledger.get("pending_total") or 0)
    combined = float(ledger.get("combined_total") or 0)
    cap = float(ledger.get("daily_cap") or EXPENSE_DAY_CAP_BDT)
    over_cap = bool(ledger.get("over_cap"))

    if not submitted and not pending:
        return format_no_session_expense_summary_message()

    lines: list[str] = [f"**দৈনিক খরচ — সারাংশ** ({date_iso})", ""]

    if submitted:
        lines.append("✅ **জমা হয়েছে**")
        for idx, batch in enumerate(submitted, start=1):
            ref = str(batch.get("reference_id") or "").strip()
            total = float(batch.get("total") or 0)
            head = f"{idx}. "
            if ref:
                head += f"`{ref}` · **{total:g} Tk**"
            else:
                head += f"**{total:g} Tk**"
            lines.append(head)
            for row in list(batch.get("items") or []):
                lines.append(f"   {_format_line_display(row).lstrip('- ')}")
        lines.append("")

    if pending:
        stage = str(pending.get("stage") or "").strip()
        src = str(pending.get("source") or "").strip()
        stage_hint = f" ({stage})" if stage else ""
        src_hint = " · parked" if src == "suspended" else ""
        lines.append(f"⏳ **Pending (জমা হয়নি)**{stage_hint}{src_hint}")
        pending_step = str(pending.get("pending_step") or "").strip().lower()
        if pending_step == "from_to":
            lines.append("   ℹ কিছু travel line-এ **From/To** এখনো লাগবে।")
        for row in list(pending.get("items") or []):
            for part in _format_pending_row_display(row):
                lines.append(f"   {part}" if not part.startswith("      ") else part)
        lines.append(f"   **মোট pending: {pending_total:g} Tk**")
        lines.append("")

    lines.append("📊 **মোট**")
    lines.append(f"   - জমা হয়েছে: **{submitted_total:g} Tk**")
    if pending_total > 0:
        lines.append(f"   - Pending draft: **{pending_total:g} Tk**")
        lines.append(f"   - মোট track: **{combined:g} Tk**")
    lines.append(f"   - Daily cap: **{cap:g} Tk**")
    if over_cap:
        lines.append(
            f"\n⚠ **{combined:g} Tk** — daily cap **{cap:g} Tk**-এর বেশি। "
            "Submit সম্ভব; চূড়ান্ত অনুমোদন CRM/Finance করবে।"
        )
    footnotes = format_ledger_footnotes(ledger)
    if footnotes:
        lines.append(footnotes)
    return "\n".join(lines)


def enrich_crm_payload_with_ledger(
    crm_payload: dict[str, Any], ledger: dict[str, Any]
) -> dict[str, Any]:
    out = dict(crm_payload)
    out["session_expense_ledger"] = ledger
    if ledger.get("submitted_batches"):
        flat: list[dict[str, Any]] = []
        for batch in ledger["submitted_batches"]:
            flat.extend(list(batch.get("items") or []))
        if flat and not out.get("expense_day_items"):
            out["expense_day_items"] = flat
    if ledger.get("submitted_total") and not out.get("expense_day_logged_total"):
        out["expense_day_logged_total"] = ledger["submitted_total"]
    return out
