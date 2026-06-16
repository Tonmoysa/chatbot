"""Typed expense command plans (Phase 2)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CorrectionCommandPlan:
    """Deterministic correction ops parsed from one user message."""

    replacements: list[tuple[str, str]] = field(default_factory=list)
    remove_travel_group: bool = False
    transfers: list[tuple[str, str, float]] = field(default_factory=list)
    partial_deducts: list[tuple[str, float]] = field(default_factory=list)
    remove_one: list[str] = field(default_factory=list)
    remove_loose: list[str] = field(default_factory=list)
    remove_verb_first: list[str] = field(default_factory=list)
    remove_by_amount: list[tuple[str, float]] = field(default_factory=list)
    remove_category_suffix: list[str] = field(default_factory=list)
    update_amounts: list[tuple[str, float]] = field(default_factory=list)
    set_amounts: list[tuple[str, float]] = field(default_factory=list)
    cat_er_amounts: list[tuple[str, float]] = field(default_factory=list)
    add_amounts: list[tuple[str, float]] = field(default_factory=list)
    amount_replacements: list[tuple[float, float]] = field(default_factory=list)
    remove_by_index: int | None = None
    update_amount_by_index: tuple[int, float] | None = None
    has_update_amount_pattern: bool = False
    has_remove_one_pattern: bool = False
    has_transfer_pattern: bool = False
    has_partial_deduct_pattern: bool = False
    set_category_only: str = ""
    bare_amount_set: float | None = None
    set_routes: list[tuple[str, str, str]] = field(default_factory=list)
    set_routes_by_index: list[tuple[int, str, str]] = field(default_factory=list)

    def has_any_correction(self) -> bool:
        return bool(
            self.replacements
            or self.remove_travel_group
            or self.transfers
            or self.partial_deducts
            or self.remove_one
            or self.remove_loose
            or self.remove_verb_first
            or self.remove_by_amount
            or self.remove_category_suffix
            or self.update_amounts
            or self.set_amounts
            or self.cat_er_amounts
            or self.add_amounts
            or self.amount_replacements
            or self.remove_by_index is not None
            or self.update_amount_by_index is not None
            or bool(self.set_category_only)
            or self.bare_amount_set is not None
            or self.set_routes
            or self.set_routes_by_index
        )


@dataclass
class WizardFlowCommandPlan:
    """High-level wizard navigation commands (collecting / review)."""

    finish_collecting: bool = False
    submit_draft: bool = False


@dataclass
class CommandExecuteResult:
    items: list[dict]
    changed: bool
    parse_source: str = "rules"


@dataclass
class CorrectionParseResult:
    plan: CorrectionCommandPlan
    source: str = "rules"
