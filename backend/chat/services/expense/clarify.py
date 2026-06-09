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
from chat.services.expense.clarify_affirmatives import (
    is_clarify_affirmative_only,
    is_clarify_affirmative_token,
    is_implausible_clarify_location,
    is_invalid_clarify_location,
    looks_like_typo_acknowledgment,
)
from chat.services.expense.clarify_copy import ClarifyPromptContext
from chat.services.expense_locations import (
    detect_travel_location_typos,
    location_context_from_rows,
)

_CONFIRM_TYPo_RE = re.compile(
    r"\b(yes|yep|yeah|ha|hae|haan|han|hmm|ji|j|ok|okay|thik|correct|right|hoy|হ্যাঁ|হ্যা|ঠিক)\b",
    re.I,
)

_PARTIAL_ISSUE_INDEX_RE = re.compile(
    r"(?:"
    r"(?P<idx1>\d+)\s*(?:no\.?|number|option|opt)\b"
    r"|"
    r"(?:option|opt|number|no\.?)\s*(?P<idx2>\d+)"
    r")",
    re.I,
)

_CLARIFY_SHORT_AFFIRMATIVE_RE = re.compile(
    r"^(?:yes|yep|yeah|ha|hae|haan|han|ji|j|ok|okay|thik|hoy|হ্যাঁ|হ্যা|ঠিক)\s*\.?!?$",
    re.I,
)


def looks_like_clarify_reply_signal(message: str) -> bool:
    """True when message could answer a batched clarify prompt (not chit-chat)."""
    text = (message or "").strip()
    if not text:
        return False
    if _CLARIFY_SHORT_AFFIRMATIVE_RE.match(text):
        return True
    if is_clarify_affirmative_only(text):
        return True
    if _CONFIRM_TYPo_RE.search(text):
        return True
    if _looks_like_category_assignment(text):
        return True
    if looks_like_typo_acknowledgment(text):
        return True
    if len(_answer_segments(text)) >= 2:
        return True
    if re.match(r"^\d+\s", text):
        return True
    if parse_category_token(text):
        return True
    return False


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
    prompt_context: ClarifyPromptContext | None = None,
) -> str:
    from chat.services.expense.clarify_copy import clarify_footer, clarify_intro

    reply_lang = normalize_reply_lang(lang)
    if not issues:
        return ""

    ctx = prompt_context or ClarifyPromptContext(
        variant="initial",
        total_issues=len(issues),
        open_count=len(issues),
    )
    lines: list[str] = [clarify_intro(lang=reply_lang, context=ctx)]

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

    lines.append(
        clarify_footer(
            lang=reply_lang,
            variant=ctx.variant,
            seed=len(issues) + ctx.resolved_count,
        )
    )
    return "\n".join(lines)


def _answer_segments(message: str) -> list[str]:
    parts = re.split(r"[,;।\n]+|\s+then\s+|\s+and\s+|\s+এবং\s+", message or "", flags=re.I)
    return [p.strip() for p in parts if p.strip()]


def parse_clarification_partial_confirm(
    message: str, issue_count: int
) -> set[int] | None:
    """
    Parse numbered partial confirms like ``2 option thik ache``.
    Returns 0-based issue indices, or None when not a partial confirm.
    """
    text = (message or "").strip()
    if not text or issue_count <= 0:
        return None
    if not _CONFIRM_TYPo_RE.search(text):
        return None
    indices: set[int] = set()
    for m in _PARTIAL_ISSUE_INDEX_RE.finditer(text):
        raw = m.group("idx1") or m.group("idx2")
        if not raw:
            continue
        idx = int(raw) - 1
        if 0 <= idx < issue_count:
            indices.add(idx)
    return indices if indices else None


def format_clarification_followup_prompt(
    unresolved: list[ClarificationIssue],
    *,
    lang: str | None = None,
    total_issues: int = 0,
    resolved_count: int = 0,
) -> str:
    """Ask only for clarify items still open after a partial reply."""
    from chat.services.expense.clarify_copy import ClarifyPromptContext, clarify_intro

    reply_lang = normalize_reply_lang(lang)
    if not unresolved:
        return ""
    ctx = ClarifyPromptContext(
        variant="followup",
        total_issues=total_issues or len(unresolved),
        resolved_count=resolved_count,
        open_count=len(unresolved),
    )
    head = clarify_intro(lang=reply_lang, context=ctx)
    body = format_clarification_prompt(
        unresolved,
        lang=lang,
        prompt_context=ClarifyPromptContext(
            variant="followup",
            total_issues=ctx.total_issues,
            resolved_count=resolved_count,
            open_count=len(unresolved),
        ),
    ).split("\n", 1)[-1]
    return head + body


def _resolve_typo_answer(segment: str, issue: ClarificationIssue) -> str | None:
    seg = (segment or "").strip()
    if not seg:
        return None
    if _looks_like_category_assignment(seg):
        return None
    low = seg.lower()
    if is_clarify_affirmative_only(seg) or is_clarify_affirmative_token(seg):
        return issue.suggestion
    if looks_like_typo_acknowledgment(seg):
        return issue.suggestion
    if issue.suggestion and issue.suggestion.lower() in low:
        return issue.suggestion
    if _CONFIRM_TYPo_RE.search(low):
        return issue.suggestion
    if parse_category_token(seg):
        return None
    pair = parse_from_to_locations(seg)
    if pair:
        resolved = pair[1] if issue.field == "to_location" else pair[0]
        if resolved.lower() == (issue.original or "").lower():
            return None
        if is_invalid_clarify_location(resolved):
            return issue.suggestion if is_clarify_affirmative_only(seg) else None
        return resolved
    if len(seg) >= 2 and not parse_category_token(seg):
        if seg.lower() == (issue.original or "").lower():
            return None
        if is_invalid_clarify_location(seg):
            return None
        if is_implausible_clarify_location(seg, issue=issue):
            return None
        return seg
    return None


def _resolve_category_answer(segment: str) -> str | None:
    return parse_category_token(segment)


def _looks_like_category_assignment(message: str) -> bool:
    """True when user assigns a category (e.g. ``metro rail hobe``), not a typo yes/no."""
    text = (message or "").strip()
    if not text:
        return False
    if len(_answer_segments(text)) > 1:
        return False
    try:
        from chat.services.expense.expense_confirm import parse_category_slot_answer

        if parse_category_slot_answer(text):
            return True
    except Exception:
        pass
    if re.search(r"\b(hobe|habe|hoy|হবে|হয়)\b", text, re.I) and parse_category_token(text):
        return True
    return False


def _issue_context_row(
    issue: ClarificationIssue,
    items: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if issue.kind == "missing_category" and 0 <= issue.pending_index < len(pending_entries):
        return pending_entries[issue.pending_index]
    if 0 <= issue.item_index < len(items):
        return items[issue.item_index]
    return None


def build_clarify_disambiguation_prompt(
    issues: list[ClarificationIssue],
    items: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]],
    *,
    lang: str | None = None,
) -> str:
    """Ask which numbered clarify item the user is answering when reply is ambiguous."""
    from chat.services.expense.clarify_copy import (
        ClarifyPromptContext,
        clarify_footer,
        clarify_intro,
    )

    reply_lang = normalize_reply_lang(lang)
    ctx = ClarifyPromptContext(
        variant="disambiguation",
        total_issues=len(issues),
        open_count=len(issues),
    )
    lines: list[str] = [clarify_intro(lang=reply_lang, context=ctx)]
    for i, issue in enumerate(issues, start=1):
        row = _issue_context_row(issue, items, pending_entries) or {}
        amt = float(issue.amount or row.get("amount") or 0)
        frm = str(row.get("from_location") or "").strip()
        to = str(row.get("to_location") or "").strip()
        route = f" · {frm} → {to}" if frm and to else ""
        if issue.kind == "location_typo":
            role = "To" if issue.field == "to_location" else "From"
            lines.append(
                f"{i}. **{issue.category}** ({amt:g} Tk){route} · {role} **{issue.original}**?"
            )
        elif issue.kind == "missing_category":
            lines.append(f"{i}. **{amt:g} Tk**{route} · category?")
        elif issue.kind == "category_typo":
            lines.append(
                f"{i}. **{amt:g} Tk** · **{issue.suggestion}** (not {issue.original})?"
            )

    return "\n".join(lines) + clarify_footer(lang=reply_lang, variant="disambiguation")


_NUMBERED_ANSWER_RE = re.compile(
    r"^(?P<idx>\d+)\s*(?:[.):\-]|)\s*(?P<body>.+)?$",
    re.I,
)


def parse_numbered_clarify_answer(
    message: str, issue_count: int
) -> tuple[set[int], str] | None:
    """Parse ``2 metro rail`` or ``1 yes`` style numbered clarify replies."""
    text = (message or "").strip()
    if not text or issue_count <= 0:
        return None
    m = _NUMBERED_ANSWER_RE.match(text)
    if m:
        idx = int(m.group("idx")) - 1
        if 0 <= idx < issue_count:
            body = (m.group("body") or "").strip()
            return {idx}, body or text
    partial = parse_clarification_partial_confirm(text, issue_count)
    if partial and len(partial) == 1:
        idx = next(iter(partial))
        body = _PARTIAL_ISSUE_INDEX_RE.sub("", text).strip()
        body = re.sub(
            r"\b(thik|ache|hobe|yes|ok|option|opt|number|no\.?)\b",
            "",
            body,
            flags=re.I,
        ).strip()
        return {idx}, body or text
    return None


def _looks_like_typo_only_answer(message: str) -> bool:
    if _looks_like_category_assignment(message):
        return False
    text = (message or "").strip()
    if not text:
        return False
    if parse_category_token(text):
        return False
    if _CONFIRM_TYPo_RE.search(text):
        return True
    if looks_like_typo_acknowledgment(text):
        return True
    low = text.lower()
    if issue_suggestion := parse_from_to_locations(text):
        del issue_suggestion
        return True
    if len(text.split()) <= 2 and not re.search(r"\d", text):
        return True
    return bool(re.search(r"\b(motejheel|motijheel|mirpur|uttora|badda)\b", low, re.I))


def _resolve_category_for_issue(message: str, issue: ClarificationIssue) -> str | None:
    try:
        from chat.services.expense.expense_confirm import parse_category_slot_answer

        cat = parse_category_slot_answer(message)
        if cat:
            return cat
    except Exception:
        pass
    return _resolve_category_answer(message)


def _apply_clarification_reply_rules(
    message: str,
    items: list[dict[str, Any]],
    issues: list[ClarificationIssue],
    pending_entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[ClarificationIssue], bool]:
    """Rules-only clarify apply. Returns (items, pending, unresolved, needs_disambiguation)."""
    out_items = [dict(row) for row in items]
    out_pending = [dict(row) for row in pending_entries]
    unresolved: list[ClarificationIssue] = []
    segments = _answer_segments(message)
    seg_idx = 0

    numbered = parse_numbered_clarify_answer(message, len(issues))
    partial_ok = (
        parse_clarification_partial_confirm(message, len(issues))
        if numbered is None
        else None
    )
    target_indices: set[int] | None = None
    answer_body = message

    if numbered:
        target_indices, answer_body = numbered
    elif partial_ok is not None:
        target_indices = partial_ok

    cat_assign = _looks_like_category_assignment(message) and target_indices is None
    typo_only = (
        _looks_like_typo_only_answer(message)
        and target_indices is None
        and len(segments) == 1
    )

    if cat_assign and len(issues) > 1:
        target_indices = {i for i, iss in enumerate(issues) if iss.kind == "missing_category"}
    elif typo_only and len(issues) > 1:
        target_indices = {i for i, iss in enumerate(issues) if iss.kind == "location_typo"}

    needs_disambiguation = False
    if (
        len(issues) > 1
        and target_indices is None
        and len(segments) == 1
        and not cat_assign
        and not typo_only
    ):
        needs_disambiguation = True
        return out_items, out_pending, issues, True

    for issue_idx, issue in enumerate(issues):
        if target_indices is not None and issue_idx not in target_indices:
            unresolved.append(issue)
            continue
        if cat_assign and issue.kind == "location_typo":
            unresolved.append(issue)
            continue

        segment = segments[seg_idx] if seg_idx < len(segments) else message
        if target_indices is not None and len(target_indices) == 1:
            segment = answer_body
        elif partial_ok is not None:
            segment = message

        if issue.kind == "location_typo":
            resolved = _resolve_typo_answer(segment, issue)
            if resolved and 0 <= issue.item_index < len(out_items):
                out_items[issue.item_index][issue.field] = resolved
                if target_indices is None and partial_ok is None and seg_idx < len(segments):
                    seg_idx += 1
            else:
                unresolved.append(issue)
        elif issue.kind == "category_typo":
            cat = issue.suggestion
            if not _CONFIRM_TYPo_RE.search((segment or "").lower()):
                cat = _resolve_category_for_issue(segment, issue) or cat
            if cat and 0 <= issue.pending_index < len(out_pending):
                out_pending[issue.pending_index]["category"] = cat
                if target_indices is None and partial_ok is None and seg_idx < len(segments):
                    seg_idx += 1
            else:
                unresolved.append(issue)
        elif issue.kind == "missing_category":
            cat = _resolve_category_for_issue(segment, issue)
            if cat and 0 <= issue.pending_index < len(out_pending):
                out_pending[issue.pending_index]["category"] = cat
                if target_indices is None and partial_ok is None and seg_idx < len(segments):
                    seg_idx += 1
            elif cat and 0 <= issue.item_index < len(out_items):
                out_items[issue.item_index]["category"] = cat
                if target_indices is None and partial_ok is None and seg_idx < len(segments):
                    seg_idx += 1
            else:
                unresolved.append(issue)

    return out_items, out_pending, unresolved, False


def _guardrail_fix_misapplied_locations(
    message: str,
    out_items: list[dict[str, Any]],
    issues: list[ClarificationIssue],
) -> list[dict[str, Any]]:
    """Revert locations wrongly set from affirmatives or conversational meta replies."""
    fixed = [dict(row) for row in out_items]
    for issue in issues:
        if issue.kind != "location_typo" or issue.item_index < 0:
            continue
        if issue.item_index >= len(fixed):
            continue
        val = str(fixed[issue.item_index].get(issue.field) or "").strip()
        if looks_like_typo_acknowledgment(message) and issue.suggestion:
            fixed[issue.item_index][issue.field] = issue.suggestion
            continue
        if is_clarify_affirmative_only(message) and is_invalid_clarify_location(val):
            if issue.suggestion:
                fixed[issue.item_index][issue.field] = issue.suggestion
            continue
        if is_implausible_clarify_location(val, issue=issue):
            fixed[issue.item_index][issue.field] = issue.original or val
    return fixed


def _unresolved_indices(
    issues: list[ClarificationIssue], unresolved: list[ClarificationIssue]
) -> set[int]:
    open_keys = {
        (u.kind, u.item_index, u.pending_index, u.field) for u in unresolved
    }
    return {
        idx
        for idx, issue in enumerate(issues)
        if (issue.kind, issue.item_index, issue.pending_index, issue.field) in open_keys
    }


def apply_clarification_reply(
    message: str,
    items: list[dict[str, Any]],
    issues: list[ClarificationIssue],
    pending_entries: list[dict[str, Any]],
    *,
    trace_id: str = "",
    use_llm: bool = True,
    last_question: str = "",
    lang: str | None = None,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[ClarificationIssue],
    bool,
    "ClarifyPraiseContext | None",
]:
    """
    Apply user clarify answers (rules + semantic LLM + guardrails).

    Returns (items, pending_entries, unresolved_issues, needs_disambiguation, praise_ctx).
    """
    from chat.services.expense.clarify_praise import ClarifyPraiseContext
    out_items, out_pending, unresolved, needs_disambig = _apply_clarification_reply_rules(
        message, items, issues, pending_entries
    )
    out_items = _guardrail_fix_misapplied_locations(message, out_items, issues)

    from chat.services.expense.clarify_llm_parser import (
        clarify_llm_enabled,
        parse_clarify_reply_llm,
        reconcile_clarify_rules_and_llm,
    )

    open_indices = _unresolved_indices(issues, unresolved)
    if needs_disambig:
        open_indices = set(range(len(issues)))
    for idx, issue in enumerate(issues):
        if issue.kind == "location_typo" and issue.item_index >= 0:
            val = str(out_items[issue.item_index].get(issue.field) or "").strip()
            if is_invalid_clarify_location(val) or is_implausible_clarify_location(
                val, issue=issue
            ):
                open_indices.add(idx)

    llm_result = None
    llm_invoked = False
    from chat.services.expense.clarify_praise import looks_like_clarify_praise_message

    may_be_praise = looks_like_clarify_praise_message(message)
    if clarify_llm_enabled(use_llm=use_llm) and (
        open_indices or needs_disambig or may_be_praise
    ):
        llm_invoked = True
        llm_result = parse_clarify_reply_llm(
            message,
            issues,
            out_items,
            out_pending,
            trace_id=trace_id,
            last_question=last_question,
        )

    final_items, final_pending, final_unresolved, final_disambig = (
        reconcile_clarify_rules_and_llm(
            message,
            issues,
            out_items,
            out_pending,
            unresolved,
            needs_disambig,
            llm_result,
        )
    )

    from chat.services.expense.clarify_observability import (
        log_clarify_resolver,
        resolver_path_label,
    )

    rules_had_partial = bool(unresolved) and len(unresolved) < len(issues)
    log_clarify_resolver(
        trace_id,
        user_message=message,
        rules_unresolved=len(unresolved),
        final_unresolved=len(final_unresolved),
        total_issues=len(issues),
        needs_disambiguation=final_disambig,
        llm_invoked=llm_invoked,
        llm_answer_count=len(llm_result.answers) if llm_result else 0,
        path=resolver_path_label(
            llm_invoked=llm_invoked,
            rules_had_partial=rules_had_partial,
        ),
        open_kinds=[i.kind for i in final_unresolved],
    )

    praise_ctx: ClarifyPraiseContext | None = None
    if not final_unresolved and not final_disambig:
        from chat.services.expense.clarify_praise import resolve_clarify_praise_for_review

        praise_ctx = resolve_clarify_praise_for_review(
            message,
            lang=lang,
            trace_id=trace_id,
            last_question=last_question,
            clarify_llm_praise=bool(llm_result and llm_result.user_sent_praise_or_meta),
        )

    return final_items, final_pending, final_unresolved, final_disambig, praise_ctx


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
