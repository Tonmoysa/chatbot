"""
Confidence-based merge policy for expense parser (regex) and LLM entity layers.

Parser wins for structured line items (category, amount, route).
LLM fills gaps when regex finds no lines or only partial amounts.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from chat.services.expense.normalization import (
    normalize_amount,
    normalize_category_label,
    normalize_location,
    resolve_llm_expense_category,
)
from chat.services.expense_locations import category_explicitly_other
from chat.services.expense_extraction import (
    ExpenseLineItem,
    ExtractionResult,
    is_travel_category,
    parse_from_to_locations,
)

# Fields where regex/parser is authoritative when it produced line items.
PARSER_PRIORITY_FIELDS: frozenset[str] = frozenset(
    {
        "category",
        "amount",
        "from_location",
        "to_location",
    }
)

# Fields where LLM semantic understanding is preferred when parser is weak.
SEMANTIC_FIELDS: frozenset[str] = frozenset(
    {
        "notes",
        "description",
        "expense_lines",
    }
)


def _line_has_parser_signal(item: ExpenseLineItem) -> bool:
    return bool(item.category and item.amount and item.amount > 0)


def _llm_rows(llm_entities: dict[str, Any]) -> list[dict[str, Any]]:
    raw = llm_entities.get("expense_lines")
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _row_to_item(
    row: dict[str, Any],
    *,
    message: str = "",
) -> ExpenseLineItem | None:
    amt = normalize_amount(row.get("amount"))
    if amt is None:
        return None
    notes = str(row.get("notes") or row.get("description") or "").strip()
    cat = resolve_llm_expense_category(
        row.get("category"), message=message, notes=notes
    )
    frm = normalize_location(row.get("from_location"))
    to = normalize_location(row.get("to_location"))
    if not frm and not to:
        pair = parse_from_to_locations(notes or message)
        if pair:
            frm, to = pair
    return ExpenseLineItem(
        category=cat,
        amount=float(amt),
        from_location=frm,
        to_location=to,
        notes=notes,
    )


def _usable_llm_category(category: str, *, message: str = "", notes: str = "") -> bool:
    if not (category or "").strip():
        return False
    if category != "Other":
        return True
    return category_explicitly_other(message, notes)


def _single_amount_fallback(
    llm_entities: dict[str, Any],
    message: str,
) -> list[ExpenseLineItem]:
    """Build a single uncategorized pending line when LLM only returns amount."""
    amt = normalize_amount(llm_entities.get("amount"))
    if amt is None:
        return []
    desc = str(llm_entities.get("description") or "").strip()
    pair = parse_from_to_locations(message) or parse_from_to_locations(desc)
    frm, to = pair if pair else ("", "")
    cat = ""
    if desc:
        cat = normalize_category_label(desc) if normalize_category_label(desc) != "Other" else ""
    return [
        ExpenseLineItem(
            category=cat,
            amount=float(amt),
            from_location=frm,
            to_location=to,
            notes=desc,
        )
    ]


def _amounts_close(a: float, b: float, *, tol: float = 0.01) -> bool:
    return abs(round(float(a), 2) - round(float(b), 2)) <= tol


def _travel_line_needs_route(item: ExpenseLineItem) -> bool:
    return is_travel_category(item.category) and not (
        item.from_location and item.to_location
    )


def parser_needs_llm_gap_fill(
    parser: ExtractionResult,
    llm_entities: dict[str, Any] | None = None,
    *,
    message: str = "",
) -> bool:
    """True when regex output is incomplete and LLM should enrich it."""
    if not parser.items:
        return bool(parser.malformed)

    for item in parser.items:
        if _travel_line_needs_route(item):
            return True
        if item.amount > 0 and not item.category:
            return True

    if parser.malformed:
        return True

    if llm_entities:
        llm_items = _llm_items_from_entities(llm_entities, message)
        for llm_item in llm_items:
            if not _line_has_parser_signal(llm_item):
                continue
            if not any(
                normalize_category_label(it.category)
                == normalize_category_label(llm_item.category)
                and _amounts_close(it.amount, llm_item.amount)
                for it in parser.items
            ):
                return True

    return False


def _llm_items_from_entities(
    llm_entities: dict[str, Any],
    message: str,
) -> list[ExpenseLineItem]:
    llm_items: list[ExpenseLineItem] = []
    for row in _llm_rows(llm_entities):
        item = _row_to_item(row, message=message)
        if item:
            llm_items.append(item)
    if not llm_items:
        llm_items = _single_amount_fallback(llm_entities, message)
    return llm_items


def _find_best_llm_match(
    item: ExpenseLineItem,
    llm_items: list[ExpenseLineItem],
    used: set[int],
) -> tuple[int, ExpenseLineItem] | None:
    best_idx: int | None = None
    best_item: ExpenseLineItem | None = None
    best_score = -1
    target_cat = normalize_category_label(item.category) if item.category else ""

    for idx, llm_item in enumerate(llm_items):
        if idx in used:
            continue
        score = 0
        llm_cat = normalize_category_label(llm_item.category)
        if target_cat and llm_cat == target_cat:
            score += 40
        elif target_cat and llm_cat != target_cat:
            continue
        if item.amount and llm_item.amount:
            if _amounts_close(item.amount, llm_item.amount):
                score += 50
            else:
                continue
        elif item.amount or llm_item.amount:
            continue
        if score > best_score:
            best_score = score
            best_idx = idx
            best_item = llm_item

    if best_idx is None or best_item is None:
        return None
    return best_idx, best_item


def fill_parser_gaps_with_llm(
    parser: ExtractionResult,
    llm_entities: dict[str, Any],
    message: str,
    *,
    llm_used: bool,
) -> tuple[ExtractionResult, dict[str, str]]:
    """
    Fill regex gaps from LLM expense_lines when parser is partial.

    - Travel rows missing From/To get LLM route when category+amount match.
    - Uncategorized parser amounts get LLM category.
    - Parser-missed lines from LLM are appended when grounded.
    - Empty parser + malformed clauses fall back to LLM-primary overlay.
    """
    sources: dict[str, str] = {}
    if not llm_used or not llm_entities:
        return parser, sources
    if not parser_needs_llm_gap_fill(parser, llm_entities, message=message):
        return parser, sources

    if not parser.items:
        return overlay_llm_expense_lines(
            parser, llm_entities, message, llm_used=True
        )

    llm_items = _llm_items_from_entities(llm_entities, message)
    if not llm_items:
        return parser, sources

    merged_items = [replace(item) for item in parser.items]
    used_llm: set[int] = set()

    for idx, item in enumerate(merged_items):
        needs_route = _travel_line_needs_route(item)
        needs_category = bool(item.amount > 0 and not item.category)
        if not needs_route and not needs_category:
            continue

        match = _find_best_llm_match(item, llm_items, used_llm)
        if not match:
            continue
        llm_idx, llm_item = match
        used_llm.add(llm_idx)

        if needs_route and llm_item.from_location and llm_item.to_location:
            item.from_location = llm_item.from_location
            item.to_location = llm_item.to_location
            sources[f"line_{idx}_route"] = "llm_gap_fill"

        if (
            needs_category
            and llm_item.category
            and _usable_llm_category(
                llm_item.category, message=message, notes=llm_item.notes
            )
        ):
            item.category = llm_item.category
            sources[f"line_{idx}_category"] = "llm_gap_fill"

    for llm_idx, llm_item in enumerate(llm_items):
        if llm_idx in used_llm:
            continue
        if not _line_has_parser_signal(llm_item):
            continue
        if not _usable_llm_category(
            llm_item.category, message=message, notes=llm_item.notes
        ):
            continue
        duplicate = any(
            normalize_category_label(it.category)
            == normalize_category_label(llm_item.category)
            and _amounts_close(it.amount, llm_item.amount)
            for it in merged_items
        )
        if duplicate:
            continue
        merged_items.append(llm_item)
        new_idx = len(merged_items) - 1
        sources[f"line_{new_idx}_category"] = "llm_gap_fill"
        if llm_item.from_location or llm_item.to_location:
            sources[f"line_{new_idx}_route"] = "llm_gap_fill"

    malformed = list(parser.malformed)

    if sources:
        sources["items"] = "parser+llm_gap_fill"

    return ExtractionResult(items=merged_items, malformed=malformed), sources


def merge_parser_and_llm(
    parser: ExtractionResult,
    llm_entities: dict[str, Any],
    message: str = "",
) -> tuple[ExtractionResult, dict[str, str]]:
    """
    Overlay LLM entities onto parser extraction following documented priority.

    Parser line items win when present. LLM lines are used only when parser is empty.
    """
    sources: dict[str, str] = {"items": "parser"}
    if parser.items:
        for idx, item in enumerate(parser.items):
            if _line_has_parser_signal(item):
                sources[f"line_{idx}_category"] = "parser"
        return parser, sources

    llm_items: list[ExpenseLineItem] = []
    for row in _llm_rows(llm_entities):
        item = _row_to_item(row, message=message)
        if item:
            llm_items.append(item)

    if not llm_items:
        llm_items = _single_amount_fallback(llm_entities, "")

    llm_items = [
        item
        for item in llm_items
        if _line_has_parser_signal(item)
        and _usable_llm_category(item.category, message="", notes=item.notes)
    ]

    if llm_items:
        sources["items"] = "llm_entities"
        return ExtractionResult(items=llm_items, malformed=list(parser.malformed)), sources

    return parser, sources


def overlay_llm_expense_lines(
    parser: ExtractionResult,
    llm_entities: dict[str, Any],
    message: str,
    *,
    llm_used: bool,
) -> tuple[ExtractionResult, dict[str, str]]:
    """
    LLM-first overlay when parser found no complete lines.

    Used for free-form Bangla/Banglish expense descriptions without category tokens.
    """
    sources: dict[str, str] = {}
    if not llm_used or parser.items:
        return parser, sources

    llm_items: list[ExpenseLineItem] = []
    for row in _llm_rows(llm_entities):
        item = _row_to_item(row, message=message)
        if item:
            llm_items.append(item)

    if not llm_items:
        llm_items = _single_amount_fallback(llm_entities, message)

    llm_items = [
        item
        for item in llm_items
        if _line_has_parser_signal(item)
        and _usable_llm_category(item.category, message=message, notes=item.notes)
    ]

    if not llm_items:
        return parser, sources

    merged = ExtractionResult(items=llm_items, malformed=list(parser.malformed))
    sources["items"] = "llm_primary"
    for idx, item in enumerate(llm_items):
        if item.category:
            sources[f"line_{idx}_category"] = "llm_primary"
        if item.from_location or item.to_location:
            sources[f"line_{idx}_route"] = "llm_primary"
    return merged, sources


def extraction_to_entities(
    extraction: ExtractionResult,
    llm_entities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Flatten extraction + LLM hints to an entities dict for orchestrator."""
    ext = dict(llm_entities or {})
    out: dict[str, Any] = {}
    if ext.get("expense_incurred_date"):
        out["expense_incurred_date"] = ext["expense_incurred_date"]
    if ext.get("description"):
        out["description"] = ext["description"]
    if extraction.items:
        out["expense_items"] = [item.to_dict() for item in extraction.items]
    if ext.get("amount") is not None and not extraction.items:
        out["amount"] = ext.get("amount")
    return out


def merge_malformed_with_llm(
    parser: ExtractionResult,
    llm_entities: dict[str, Any],
    message: str,
) -> ExtractionResult:
    """
    When parser left malformed clauses, try LLM rows before keeping malformed pending.
    """
    filled, _sources = fill_parser_gaps_with_llm(
        parser, llm_entities, message, llm_used=True
    )
    if filled.items:
        return filled
    return parser
