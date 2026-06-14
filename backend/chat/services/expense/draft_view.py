"""
ExpenseDraftView — single source of truth for numbered draft display and mutations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from chat.services.expense_extraction import is_travel_category


def _new_line_id() -> str:
    return uuid.uuid4().hex[:10]


def ensure_line_ids(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in items:
        r = dict(row)
        if not str(r.get("line_id") or "").strip():
            r["line_id"] = _new_line_id()
        out.append(r)
    return out


@dataclass(frozen=True)
class DraftLine:
    number: int
    line_id: str
    category: str
    amount: float
    from_location: str
    to_location: str
    kind: str  # committed | pending | pending_queue
    source_index: int
    pending_gap: str | None = None

    @property
    def is_complete(self) -> bool:
        if not self.pending_gap:
            return True
        return False

    def display_label(self, *, lang: str | None = None) -> str:
        cat = self.category.capitalize() if self.category else "?"
        amt = self.amount
        route = ""
        if self.from_location and self.to_location:
            route = f" ({self.from_location} → {self.to_location})"
        elif self.pending_gap:
            if lang == "en":
                route = " (route pending)"
            else:
                route = " (route pending)"
        gap = ""
        if self.pending_gap:
            gap = f" ⚠ {self.pending_gap}"
        return f"{self.number}. {cat} — {amt:g} Tk{route}{gap}"


class ExpenseDraftView:
    def __init__(self, items: list[dict[str, Any]], block: dict[str, Any] | None = None):
        self.block = block if isinstance(block, dict) else {}
        self.items = ensure_line_ids([dict(x) for x in items])
        self._lines = self._build_lines()

    def _pending_gap(self, row: dict[str, Any]) -> str | None:
        cat = str(row.get("category") or "").strip().lower()
        if not is_travel_category(cat):
            return None
        frm = str(row.get("from_location") or "").strip()
        to = str(row.get("to_location") or "").strip()
        if frm and to:
            return None
        return "From/To needed"

    def _build_lines(self) -> list[DraftLine]:
        lines: list[DraftLine] = []
        n = 0
        for idx, row in enumerate(self.items):
            n += 1
            lines.append(
                DraftLine(
                    number=n,
                    line_id=str(row.get("line_id") or ""),
                    category=str(row.get("category") or "").strip(),
                    amount=float(row.get("amount") or 0),
                    from_location=str(row.get("from_location") or "").strip(),
                    to_location=str(row.get("to_location") or "").strip(),
                    kind="committed",
                    source_index=idx,
                    pending_gap=self._pending_gap(row),
                )
            )
        pending = self.block.get("pending_line")
        if isinstance(pending, dict) and pending.get("amount"):
            n += 1
            prow = dict(pending)
            if not str(prow.get("line_id") or "").strip():
                prow["line_id"] = _new_line_id()
            gap = self._pending_gap(prow)
            if not str(prow.get("category") or "").strip():
                gap = gap or "Category needed"
            lines.append(
                DraftLine(
                    number=n,
                    line_id=str(prow.get("line_id") or ""),
                    category=str(prow.get("category") or "").strip() or "?",
                    amount=float(prow.get("amount") or 0),
                    from_location=str(prow.get("from_location") or "").strip(),
                    to_location=str(prow.get("to_location") or "").strip(),
                    kind="pending",
                    source_index=0,
                    pending_gap=gap,
                )
            )
        for qi, row in enumerate(list(self.block.get("pending_queue") or [])):
            if not isinstance(row, dict) or not row.get("amount"):
                continue
            n += 1
            qrow = dict(row)
            if not str(qrow.get("line_id") or "").strip():
                qrow["line_id"] = _new_line_id()
            lines.append(
                DraftLine(
                    number=n,
                    line_id=str(qrow.get("line_id") or ""),
                    category=str(qrow.get("category") or "").strip() or "?",
                    amount=float(qrow.get("amount") or 0),
                    from_location=str(qrow.get("from_location") or "").strip(),
                    to_location=str(qrow.get("to_location") or "").strip(),
                    kind="pending_queue",
                    source_index=qi,
                    pending_gap=self._pending_gap(qrow),
                )
            )
        return lines

    @property
    def lines(self) -> list[DraftLine]:
        return list(self._lines)

    def line_by_number(self, num: int) -> DraftLine | None:
        for ln in self._lines:
            if ln.number == num:
                return ln
        return None

    def lines_by_category(self, category: str) -> list[DraftLine]:
        cat_l = (category or "").strip().lower()
        return [ln for ln in self._lines if ln.category.lower() == cat_l]

    def committed_total(self) -> float:
        return sum(ln.amount for ln in self._lines if ln.kind == "committed")

    def draft_total(self) -> float:
        """All visible lines — committed items plus pending / queued rows."""
        return sum(ln.amount for ln in self._lines)

    def has_pending_gaps(self) -> bool:
        return any(ln.pending_gap for ln in self._lines)

    def pending_gap_lines(self) -> list[DraftLine]:
        return [ln for ln in self._lines if ln.pending_gap]

    def apply_items_to_block(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Write line_ids back to block items and pending rows."""
        self.block["items"] = self.items
        for ln in self._lines:
            if ln.kind == "pending":
                prow = dict(self.block.get("pending_line") or {})
                prow["line_id"] = ln.line_id
                self.block["pending_line"] = prow
            elif ln.kind == "pending_queue":
                queue = list(self.block.get("pending_queue") or [])
                if 0 <= ln.source_index < len(queue):
                    queue[ln.source_index] = {**dict(queue[ln.source_index]), "line_id": ln.line_id}
                    self.block["pending_queue"] = queue
        return self.items, self.block

    def remove_committed_by_line_id(self, line_id: str) -> bool:
        lid = (line_id or "").strip()
        before = len(self.items)
        self.items = [r for r in self.items if str(r.get("line_id") or "") != lid]
        if len(self.items) < before:
            self._lines = self._build_lines()
            return True
        return False
