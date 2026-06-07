"""
Pre-review clarification — batch location typo + missing category (D/E flow).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from chat.services.expense_copy import normalize_reply_lang
from chat.services.expense_extraction import (
    detect_likely_category_typo,
    is_travel_category,
    parse_category_token,
    parse_from_to_locations,
)
from chat.services.expense_locations import (
    detect_travel_location_typos,
    location_context_from_rows,
)

_CONFIRM_TYPo_RE = re.compile(
    r"\b(yes|yep|yeah|ha|han|hmm|ji|j|ok|okay|thik|correct|right|hoy|হ্যাঁ|হ্যা|ঠিক)\b",
    re.I,
)


@dataclass
class ClarificationIssue:
    kind: str  # location_typo | missing_category | category_typo
    item_index: int = -1
    field: str = ""
    original: str = ""
    suggestion: str = ""
    category: str = ""
    amount: float = 0.0
    pending_index: int = -1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> ClarificationIssue:
        return ClarificationIssue(
            kind=str(raw.get("kind") or ""),
            item_index=int(raw.get("item_index", -1)),
            field=str(raw.get("field") or ""),
            original=str(raw.get("original") or ""),
            suggestion=str(raw.get("suggestion") or ""),
            category=str(raw.get("category") or ""),
            amount=float(raw.get("amount") or 0),
            pending_index=int(raw.get("pending_index", -1)),
        )


@dataclass
class ClarificationResult:
    issues: list[ClarificationIssue] = field(default_factory=list)
    line_flags: dict[int, list[str]] = field(default_factory=dict)


def collect_clarification_issues(
    items: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]] | None = None,
) -> list[ClarificationIssue]:
    """Detect issues that need one batched clarify turn before review."""
    issues: list[ClarificationIssue] = []
    ctx = location_context_from_rows(items)

    for idx, row in enumerate(items):
        cat = str(row.get("category") or "").strip()
        amt = float(row.get("amount") or 0)
        if not cat and amt > 0:
            issues.append(
                ClarificationIssue(
                    kind="missing_category",
                    item_index=idx,
                    amount=amt,
                )
            )
        if not is_travel_category(cat):
            continue
        for typo in detect_travel_location_typos(row, context=ctx):
            issues.append(
                ClarificationIssue(
                    kind="location_typo",
                    item_index=idx,
                    field=typo["field"],
                    original=typo["original"],
                    suggestion=typo["suggestion"],
                    category=cat,
                    amount=float(row.get("amount") or 0),
                )
            )

    for pidx, pending in enumerate(pending_entries or []):
        amt = float(pending.get("amount") or 0)
        if amt <= 0:
            continue
        if str(pending.get("category") or "").strip():
            continue
        src = str(pending.get("source_clause") or "").strip()
        typo = detect_likely_category_typo(src) if src else None
        if typo:
            original, suggestion = typo
            issues.append(
                ClarificationIssue(
                    kind="category_typo",
                    pending_index=pidx,
                    amount=amt,
                    original=original,
                    suggestion=suggestion,
                    category=suggestion,
                )
            )
            continue
        issues.append(
            ClarificationIssue(
                kind="missing_category",
                pending_index=pidx,
                amount=amt,
            )
        )

    return issues


def serialize_clarification_issues(
    issues: list[ClarificationIssue],
) -> list[dict[str, Any]]:
    return [issue.to_dict() for issue in issues]


def deserialize_clarification_issues(
    raw: list[Any] | None,
) -> list[ClarificationIssue]:
    out: list[ClarificationIssue] = []
    for row in raw or []:
        if isinstance(row, dict):
            out.append(ClarificationIssue.from_dict(row))
    return out


def format_clarification_prompt(
    issues: list[ClarificationIssue],
    *,
    lang: str | None = None,
) -> str:
    reply_lang = normalize_reply_lang(lang)
    if not issues:
        return ""

    lines: list[str] = []
    if reply_lang == "en":
        lines.append("A few details need confirming before review:\n")
    else:
        lines.append("পর্যালোচনার আগে কিছু তথ্য নিশ্চিত করতে হবে:\n")

    for i, issue in enumerate(issues, start=1):
        if issue.kind == "location_typo":
            role = "To" if issue.field == "to_location" else "From"
            if reply_lang == "en":
                lines.append(
                    f"{i}. **{issue.category}** ({issue.amount:g} Tk) · {role}: "
                    f"**{issue.original}** — did you mean **{issue.suggestion}**?"
                )
            else:
                lines.append(
                    f"{i}. **{issue.category}** ({issue.amount:g} Tk) · {role}: "
                    f"**{issue.original}** — **{issue.suggestion}** বোঝাচ্ছেন?"
                )
        elif issue.kind == "category_typo":
            if reply_lang == "en":
                lines.append(
                    f"{i}. **{issue.amount:g} Tk** — did you mean **{issue.suggestion}** "
                    f"(not **{issue.original}**)?"
                )
            else:
                lines.append(
                    f"{i}. **{issue.amount:g} Tk** — আপনি কি **{issue.suggestion}** "
                    f"বুঝিয়েছেন (**{issue.original}**)?"
                )
        elif issue.kind == "missing_category":
            if reply_lang == "en":
                lines.append(
                    f"{i}. **{issue.amount:g} Tk** — what category? "
                    f"(e.g. lunch, bus, snack, rickshaw)"
                )
            else:
                lines.append(
                    f"{i}. **{issue.amount:g} Tk** — category ki? "
                    f"(যেমন: lunch, bus, snack, rickshaw)"
                )

    if reply_lang == "en":
        lines.append(
            "\nReply in one message (e.g. `mirpur, snack` or `yes, lunch`)."
        )
    else:
        lines.append(
            "\nএক মেসেজে উত্তর দিন (যেমন: `mirpur, snack` বা `yes, lunch`)।"
        )
    return "\n".join(lines)


def _answer_segments(message: str) -> list[str]:
    parts = re.split(r"[,;।\n]+|\s+then\s+|\s+and\s+|\s+এবং\s+", message or "", flags=re.I)
    return [p.strip() for p in parts if p.strip()]


def _resolve_typo_answer(segment: str, issue: ClarificationIssue) -> str | None:
    seg = (segment or "").strip()
    if not seg:
        return None
    low = seg.lower()
    if issue.suggestion and issue.suggestion.lower() in low:
        return issue.suggestion
    if _CONFIRM_TYPo_RE.search(low):
        return issue.suggestion
    pair = parse_from_to_locations(seg)
    if pair:
        return pair[1] if issue.field == "to_location" else pair[0]
    if len(seg) >= 2 and not parse_category_token(seg):
        return seg
    return None


def _resolve_category_answer(segment: str) -> str | None:
    return parse_category_token(segment)


def apply_clarification_reply(
    message: str,
    items: list[dict[str, Any]],
    issues: list[ClarificationIssue],
    pending_entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[ClarificationIssue]]:
    """
    Apply user clarify answers. Returns (items, pending_entries, unresolved_issues).
    """
    out_items = [dict(row) for row in items]
    out_pending = [dict(row) for row in pending_entries]
    unresolved: list[ClarificationIssue] = []
    segments = _answer_segments(message)
    seg_idx = 0

    for issue in issues:
        segment = segments[seg_idx] if seg_idx < len(segments) else message
        if issue.kind == "location_typo":
            resolved = _resolve_typo_answer(segment, issue)
            if resolved and 0 <= issue.item_index < len(out_items):
                out_items[issue.item_index][issue.field] = resolved
                if seg_idx < len(segments):
                    seg_idx += 1
            else:
                unresolved.append(issue)
        elif issue.kind == "category_typo":
            cat = issue.suggestion
            if not _CONFIRM_TYPo_RE.search((segment or "").lower()):
                cat = _resolve_category_answer(segment) or cat
            if cat and 0 <= issue.pending_index < len(out_pending):
                out_pending[issue.pending_index]["category"] = cat
                if seg_idx < len(segments):
                    seg_idx += 1
            else:
                unresolved.append(issue)
        elif issue.kind == "missing_category":
            cat = _resolve_category_answer(segment)
            if cat and 0 <= issue.pending_index < len(out_pending):
                out_pending[issue.pending_index]["category"] = cat
                if seg_idx < len(segments):
                    seg_idx += 1
            elif cat and 0 <= issue.item_index < len(out_items):
                out_items[issue.item_index]["category"] = cat
                if seg_idx < len(segments):
                    seg_idx += 1
            else:
                unresolved.append(issue)

    return out_items, out_pending, unresolved


def build_review_line_flags(
    items: list[dict[str, Any]],
    *,
    warnings: list[str] | None = None,
    unresolved_issues: list[ClarificationIssue] | None = None,
) -> dict[int, list[str]]:
    """Inline review flags per item index (E — review screen UX)."""
    flags: dict[int, list[str]] = {i: [] for i in range(len(items))}

    for issue in unresolved_issues or []:
        if issue.kind != "location_typo" or issue.item_index < 0:
            continue
        role = "To" if issue.field == "to_location" else "From"
        flags.setdefault(issue.item_index, []).append(
            f"⚠️ {role}: {issue.original} ({issue.suggestion}?)"
        )

    for w in warnings or []:
        for idx, row in enumerate(items):
            cat = str(row.get("category") or "")
            if cat and cat in w:
                flags.setdefault(idx, []).append(f"⚠️ {w}")

    for idx, row in enumerate(items):
        if not str(row.get("category") or "").strip():
            flags.setdefault(idx, []).append("⚠️ category?")

    return {k: v for k, v in flags.items() if v}
