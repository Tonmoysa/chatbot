"""
Confidence-based merge policy for expense parser (regex) and LLM entity layers.

Parser wins for structured line items (category, amount, route).
LLM fills gaps when regex finds no lines or only partial amounts.
"""

from __future__ import annotations

from dataclasses import replace
import re
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
    route_explicit_for_category,
    route_explicit_in_user_message,
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
    if frm and to and not route_explicit_in_user_message(message, frm, to):
        frm, to = "", ""
    if not frm and not to:
        pair = parse_from_to_locations(message)
        if pair and route_explicit_in_user_message(message, pair[0], pair[1]):
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
    pair = parse_from_to_locations(message)
    if not pair and desc:
        pair = parse_from_to_locations(desc)
        if pair and not route_explicit_in_user_message(message, pair[0], pair[1]):
            pair = None
    elif pair and not route_explicit_in_user_message(message, pair[0], pair[1]):
        pair = None
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


def explicit_category_mentions(msg: str) -> set[str]:
    """
    Canonical categories explicitly mentioned in user text.
    Used to block LLM from inventing transport (e.g. Bus) when not stated.
    """
    if not msg:
        return set()
    low = (msg or "").lower()
    cats: set[str] = set()
    if re.search(r"\b(bus|bos|বাস|বাসে)\b", low):
        cats.add("Bus")
    if re.search(r"\b(bike|baik|baike|bicycle)\b", low) or re.search(
        r"\b(বাইক|বাইকে)\b", msg, re.I
    ):
        cats.add("Bike")
    if re.search(r"\b(rickshaw|riksha|রিকশা|রিকশায়|রিক্সা)\b", msg, re.I):
        cats.add("Rickshaw")
    if re.search(r"\b(train|ট্রেন)\b", msg, re.I):
        cats.add("Train")
    if re.search(r"\b(cng|সিএনজি|auto)\b", msg, re.I):
        cats.add("CNG")
    if re.search(
        r"\b(metro|মেট্রো|metro\s*rail|metrorail|metroral)\b", low, re.I
    ) or re.search(r"\b(মেট্রো)\b", msg, re.I):
        cats.add("Metro Rail")
    elif re.search(r"\brail\b", low) and not re.search(
        r"\b(railway|train)\b", low, re.I
    ):
        cats.add("Metro Rail")
    if re.search(r"\b(lunch|lanch|luch|lunc|খাওয়া|খাবার|লাঞ্ছ|লাঞ্চ)\b", msg, re.I):
        cats.add("Lunch")
    if re.search(r"\b(snack|snacks|স্ন্যাক)\b", msg, re.I):
        cats.add("Snack")
    return cats


def _parser_has_uncategorized_route(
    items: list[ExpenseLineItem],
) -> list[tuple[float, str, str]]:
    """Amount + route pairs waiting for category (parser output)."""
    pending: list[tuple[float, str, str]] = []
    for it in items:
        if it.category or not it.amount:
            continue
        frm = str(it.from_location or "").strip()
        to = str(it.to_location or "").strip()
        if frm and to:
            pending.append((round(float(it.amount), 2), frm.lower(), to.lower()))
    return pending


def _llm_line_matches_uncategorized_route(
    llm_item: ExpenseLineItem,
    uncategorized_routes: list[tuple[float, str, str]],
) -> bool:
    if not uncategorized_routes:
        return False
    amt = round(float(llm_item.amount or 0), 2)
    frm = str(llm_item.from_location or "").strip().lower()
    to = str(llm_item.to_location or "").strip().lower()
    for u_amt, u_frm, u_to in uncategorized_routes:
        if amt != u_amt:
            continue
        if (frm == u_frm and to == u_to) or (frm == u_to and to == u_frm):
            return True
    return False


def _message_suggests_more_lines_than_parser(
    message: str,
    parser: ExtractionResult,
) -> bool:
    """Heuristic: user named more amounts/categories than regex extracted."""
    from chat.services.expense.llm_extraction_trigger import (
        count_distinct_amount_mentions,
        looks_like_long_compound_expense_message,
    )

    if not (message or "").strip():
        return False
    amt_count = count_distinct_amount_mentions(message)
    if amt_count >= 2 and len(parser.items) < amt_count:
        return True
    if looks_like_long_compound_expense_message(message) and len(parser.items) < 2:
        return True
    return False


def parser_needs_llm_gap_fill(
    parser: ExtractionResult,
    llm_entities: dict[str, Any] | None = None,
    *,
    message: str = "",
) -> bool:
    """True when regex output is incomplete and LLM should enrich it."""
    if not parser.items:
        if parser.malformed:
            return True
        from chat.services.expense.llm_extraction_trigger import (
            looks_like_long_compound_expense_message,
        )

        return looks_like_long_compound_expense_message(message)

    for item in parser.items:
        if _travel_line_needs_route(item):
            return True
        if item.amount > 0 and not item.category:
            return True
        frm = str(item.from_location or "").strip()
        to = str(item.to_location or "").strip()
        if is_travel_category(str(item.category or "")) and (frm or to) and not (frm and to):
            return True

    if parser.malformed:
        return True

    if _message_suggests_more_lines_than_parser(message, parser):
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
    explicit = explicit_category_mentions(message)
    uncategorized_routes = _parser_has_uncategorized_route(merged_items)

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
            if route_explicit_for_category(
                message,
                item.category,
                llm_item.from_location,
                llm_item.to_location,
            ):
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
            llm_cat = normalize_category_label(llm_item.category)
            if (
                is_travel_category(llm_cat)
                and llm_cat not in explicit
                and uncategorized_routes
            ):
                pass
            else:
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
        llm_cat = normalize_category_label(llm_item.category)
        if is_travel_category(llm_cat) and llm_cat not in explicit:
            if _llm_line_matches_uncategorized_route(llm_item, uncategorized_routes):
                continue
            if uncategorized_routes:
                continue
        duplicate = any(
            normalize_category_label(it.category)
            == normalize_category_label(llm_item.category)
            and _amounts_close(it.amount, llm_item.amount)
            for it in merged_items
        )
        if duplicate:
            continue
        llm_cat = normalize_category_label(llm_item.category)
        if any(
            it.category
            and _amounts_close(it.amount, llm_item.amount)
            and normalize_category_label(it.category) != llm_cat
            for it in merged_items
        ):
            continue
        if llm_cat in ("Lunch", "Snack") and (
            llm_item.from_location and llm_item.to_location
        ):
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
        # Default: keep parser items (fast path, deterministic).
        for idx, item in enumerate(parser.items):
            if _line_has_parser_signal(item):
                sources[f"line_{idx}_category"] = "parser"

        # Reconcile category conflicts when user text explicitly mentions another category.
        explicit = explicit_category_mentions(message)
        parser_cats = {normalize_category_label(it.category) for it in parser.items if it.category}
        # Only trigger when we have explicit evidence and parser misses some of it.
        if explicit and any(c in explicit for c in parser_cats) and not explicit.issubset(parser_cats):
            # Build llm items and prefer those that cover explicit categories better.
            llm_items: list[ExpenseLineItem] = []
            for row in _llm_rows(llm_entities):
                item = _row_to_item(row, message=message)
                if item:
                    llm_items.append(item)
            if llm_items:
                llm_items = [
                    item
                    for item in llm_items
                    if _line_has_parser_signal(item)
                    and _usable_llm_category(
                        item.category, message="", notes=item.notes
                    )
                ]
            if llm_items:
                llm_cats = {normalize_category_label(it.category) for it in llm_items if it.category}
                # Pick llm if it covers more explicitly mentioned categories.
                if len(explicit & llm_cats) > len(explicit & parser_cats):
                    sources["items"] = "llm_conflict_reconcile"
                    return ExtractionResult(
                        items=llm_items, malformed=list(parser.malformed)
                    ), sources

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
