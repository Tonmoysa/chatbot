"""
Hybrid expense entity extraction: parser layer + optional LLM + merge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from chat.constants import INTENT_EXPENSE_CLAIM
from chat.services.entity_extractor import EntityExtractor
from chat.services.expense.entity_merge import (
    extraction_to_entities,
    fill_parser_gaps_with_llm,
    merge_parser_and_llm,
    overlay_llm_expense_lines,
)
from chat.services.expense_extraction import ExtractionResult, extract_expense_items
from datetime import date

from chat.services.expense_incurred_date import infer_expense_incurred_date_iso


@dataclass
class ExpenseExtractionResult:
    entities: dict[str, Any] = field(default_factory=dict)
    extraction: ExtractionResult | None = None
    source: str = "pipeline"
    field_sources: dict[str, str] = field(default_factory=dict)


class ExpenseEntityPipeline:
    """Unified hybrid extraction for expense claim turns."""

    def __init__(self, entity_extractor: EntityExtractor | None = None) -> None:
        self._extractor = entity_extractor or EntityExtractor()

    def extract(
        self,
        message: str,
        *,
        intent: str,
        context_lines: list[str],
        trace_id: str,
        use_llm: bool = True,
    ) -> ExpenseExtractionResult:
        """
        Run parser + optional LLM + merge; return flat entities for orchestrator/workflow.

        Structured fields (category, amount, route): parser primary.
        Free-form lines and incurred_date hints: LLM fills gaps.
        """
        llm_entities: dict[str, Any] = {}
        llm_source = "rules"
        llm_invoked = False

        if use_llm and self._extractor._llm.is_configured():
            pack = self._extractor.extract(
                message, intent, context_lines, trace_id
            )
            llm_entities = dict(pack.get("entities") or {})
            llm_source = str(pack.get("source") or "llm")
            llm_invoked = llm_source == "llm"
        elif use_llm:
            llm_entities = self._extractor.extract_rules_only(
                message, intent=intent
            )
            llm_source = "rules"
        else:
            llm_entities = self._extractor.extract_rules_only(
                message, intent=intent
            )
            llm_source = "rules_wizard"

        parser_ex = extract_expense_items(message)
        merged_ex, field_sources = merge_parser_and_llm(
            parser_ex, llm_entities, message
        )

        if llm_invoked:
            gap_ex, gap_sources = fill_parser_gaps_with_llm(
                merged_ex,
                llm_entities,
                message,
                llm_used=True,
            )
            merged_ex = gap_ex
            field_sources.update(gap_sources)

            if not merged_ex.items:
                sem_ex, sem_sources = overlay_llm_expense_lines(
                    merged_ex,
                    llm_entities,
                    message,
                    llm_used=True,
                )
                if sem_ex.items:
                    merged_ex = sem_ex
                field_sources.update(sem_sources)

        entities = extraction_to_entities(merged_ex, llm_entities)
        inc = (
            llm_entities.get("expense_incurred_date")
            or infer_expense_incurred_date_iso(
                message=message, hints=llm_entities, today=date.today()
            )
        )
        if inc:
            entities["expense_incurred_date"] = inc
            if "expense_incurred_date" not in field_sources:
                field_sources["expense_incurred_date"] = (
                    "llm_entities" if llm_invoked else "rules_date"
                )

        return ExpenseExtractionResult(
            entities=entities,
            extraction=merged_ex,
            source=f"pipeline+{llm_source}",
            field_sources=field_sources,
        )

    def extract_lines(
        self,
        message: str,
        *,
        intent: str = INTENT_EXPENSE_CLAIM,
        context_lines: list[str] | None = None,
        trace_id: str = "",
        use_llm: bool = True,
        preloaded: ExpenseExtractionResult | None = None,
    ) -> ExtractionResult:
        """
        Return merged line extraction for expense_workflow ingestion.

        When ``preloaded`` is supplied (orchestrator already ran the pipeline), reuse it
        to avoid a second LLM call on the same turn.
        """
        if preloaded and preloaded.extraction is not None:
            return preloaded.extraction
        result = self.extract(
            message,
            intent=intent,
            context_lines=context_lines or [],
            trace_id=trace_id or "expense-workflow-local",
            use_llm=use_llm,
        )
        return result.extraction or ExtractionResult()
