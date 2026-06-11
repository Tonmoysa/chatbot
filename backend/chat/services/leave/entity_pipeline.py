"""
Hybrid leave entity extraction: parser layer + optional LLM + merge + draft apply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from chat.constants import INTENT_LEAVE_REQUEST
from chat.services.entity_extractor import EntityExtractor
from chat.services.leave.entity_merge import (
    extraction_to_entities,
    merge_parser_and_llm,
    overlay_llm_semantic_fields,
)
from chat.services.leave_slot_extraction import (
    LeaveSlotExtraction,
    explicit_leave_type_from_message,
    extract_leave_slots,
    extract_reason_from_message,
    is_payment_only_message,
)
from chat.services.leave_slots import prefill_draft_from_extraction


@dataclass
class LeaveExtractionResult:
    entities: dict[str, Any] = field(default_factory=dict)
    extraction: LeaveSlotExtraction | None = None
    source: str = "pipeline"
    field_sources: dict[str, str] = field(default_factory=dict)


class LeaveEntityPipeline:
    """Unified hybrid extraction for leave request turns."""

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
    ) -> LeaveExtractionResult:
        """
        Run parser + optional LLM + merge; return flat entities for orchestrator/workflow.

        Semantic fields (reason, leave_type): LLM primary, regex fallback.
        Structured fields (dates, payment, scope): parser primary.
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

        parser_ex = extract_leave_slots(message, skip_leave_phrase_gate=True)
        merged_ex, field_sources = merge_parser_and_llm(
            parser_ex, llm_entities, message=message
        )

        if llm_invoked:
            sem = overlay_llm_semantic_fields(
                merged_ex,
                llm_entities,
                message,
                llm_used=True,
            )
            field_sources.update(sem)

        entities = extraction_to_entities(merged_ex)

        # Regex fallback for reason when LLM did not extract one.
        from chat.services.workflow_navigation import is_leave_navigation_phrase

        if is_leave_navigation_phrase(message):
            entities.pop("reason", None)
            entities.pop("description", None)
        elif not entities.get("reason"):
            reason = extract_reason_from_message(message)
            if reason:
                entities["reason"] = reason
                field_sources["reason"] = "rules_fallback"
        else:
            from chat.services.leave.reason_value import (
                extract_reason_value,
                is_boilerplate_leave_reason,
            )

            if is_boilerplate_leave_reason(str(entities.get("reason") or "")):
                better = extract_reason_value(message)
                if better:
                    entities["reason"] = better
                    field_sources["reason"] = "rules_fallback"

        explicit_lt = explicit_leave_type_from_message(message)
        if explicit_lt:
            entities["leave_type"] = explicit_lt
            field_sources["leave_type"] = "rules_explicit_type"
        elif not entities.get("leave_type") and llm_entities.get("leave_type"):
            lt = str(llm_entities.get("leave_type") or "").strip().lower()
            from chat.services.leave.normalization import text_has_sick_signal
            from chat.services.leave_draft_utils import reason_indicates_non_sick_leave

            if explicit_lt:
                entities["leave_type"] = explicit_lt
            elif lt == "sick" and text_has_sick_signal(message):
                entities["leave_type"] = "sick"
            elif not reason_indicates_non_sick_leave(message) and field_sources.get(
                "leave_type"
            ) == "llm_primary" and lt == "sick":
                entities["leave_type"] = "sick"

        from chat.services.leave.normalization import (
            strip_ungrounded_day_scope,
            strip_ungrounded_leave_dates,
            strip_ungrounded_payment_category,
        )
        from chat.services.leave.reason_value import strip_ungrounded_reason

        entities = strip_ungrounded_payment_category(entities, message)
        entities = strip_ungrounded_day_scope(entities, message)
        entities = strip_ungrounded_leave_dates(entities, message)
        entities = strip_ungrounded_reason(entities, message)

        return LeaveExtractionResult(
            entities=entities,
            extraction=merged_ex,
            source=f"pipeline+{llm_source}",
            field_sources=field_sources,
        )

    def apply_to_draft(
        self,
        draft: dict[str, Any],
        message: str,
        entities: dict[str, Any],
        *,
        overwrite: bool = False,
        trace_id: str = "",
    ) -> None:
        """
        Merge extracted entities into workflow draft.

        Uses pre-extracted entities (LLM-first). Runs local LLM extract only when
        entities are empty and API is configured.
        """
        ent = dict(entities or {})
        from chat.services.leave.normalization import (
            message_explicitly_states_day_scope,
            message_explicitly_states_leave_date,
            message_explicitly_states_payment_category,
            message_mentions_leave_duration,
            should_suppress_inferred_leave_dates,
            strip_ungrounded_day_scope,
            strip_ungrounded_leave_dates,
            strip_ungrounded_payment_category,
        )

        from chat.services.leave.reason_value import strip_ungrounded_reason

        ent = strip_ungrounded_payment_category(ent, message)
        ent = strip_ungrounded_day_scope(ent, message)
        ent = strip_ungrounded_leave_dates(ent, message)
        ent = strip_ungrounded_reason(ent, message)
        preserve_dates = overwrite and not message_explicitly_states_leave_date(message)
        if preserve_dates and message_mentions_leave_duration(message):
            preserve_dates = False
        preserved_start = draft.get("start_date") if preserve_dates else None
        preserved_end = draft.get("end_date") if preserve_dates else None
        had_scope = bool(draft.get("day_scope"))
        if not ent and self._extractor._llm.is_configured():
            local = self.extract(
                message,
                intent=INTENT_LEAVE_REQUEST,
                context_lines=[],
                trace_id=trace_id or "leave-draft-local",
                use_llm=True,
            )
            ent = dict(local.entities or {})

        if is_payment_only_message(message):
            from chat.services.leave_workflow import (
                _infer_day_scope,
                _infer_payment_category,
            )

            _infer_payment_category(message, draft, force=True)
            _infer_day_scope(message, draft)
            self._apply_semantic_entities(draft, ent, message, overwrite=overwrite)
            self._normalize_draft(draft)
            return

        from chat.services.leave_workflow import (
            _force_scope_from_message,
            _infer_day_scope,
            _infer_leave_type,
            _infer_payment_category,
            _is_compound_slot_message,
        )

        parts = [p.strip() for p in re.split(r"[,;]+", message) if p.strip()]
        if not parts:
            parts = [message]
        seen: set[str] = set()
        for part in parts:
            if part in seen:
                continue
            seen.add(part)
            ex = extract_leave_slots(part, skip_leave_phrase_gate=True)
            prefill_draft_from_extraction(
                draft, ex, external_entities=None, overwrite=overwrite
            )
            _infer_leave_type(part, draft)
            _infer_payment_category(part, draft)
            _infer_day_scope(part, draft)

        ext = dict(ent)
        ex_whole = extract_leave_slots(message, skip_leave_phrase_gate=True)
        merged_ex, _sources = merge_parser_and_llm(ex_whole, ext, message=message)
        if ent.get("reason") or ent.get("description"):
            sem = overlay_llm_semantic_fields(
                merged_ex, ent, message, llm_used=True
            )
            _sources.update(sem)
        slot_overwrite = overwrite or _is_compound_slot_message(message)
        prefill_draft_from_extraction(
            draft,
            merged_ex,
            external_entities=extraction_to_entities(merged_ex),
            overwrite=slot_overwrite,
        )

        explicit_lt = explicit_leave_type_from_message(message)
        if explicit_lt:
            draft["leave_type"] = explicit_lt
        _infer_leave_type(message, draft)
        _infer_payment_category(message, draft)
        _infer_day_scope(message, draft)

        self._apply_semantic_entities(draft, ent, message, overwrite=overwrite)

        if overwrite:
            _force_scope_from_message(message, draft)

        if not had_scope and not message_explicitly_states_day_scope(message):
            draft.pop("day_scope", None)

        if not message_explicitly_states_payment_category(message):
            draft.pop("leave_payment_category", None)

        if should_suppress_inferred_leave_dates(message):
            draft.pop("start_date", None)
            draft.pop("end_date", None)

        if preserve_dates:
            if preserved_start:
                draft["start_date"] = preserved_start
            if preserved_end:
                draft["end_date"] = preserved_end

        self._normalize_draft(draft)

    @staticmethod
    def _apply_semantic_entities(
        draft: dict[str, Any],
        entities: dict[str, Any],
        message: str,
        *,
        overwrite: bool,
    ) -> None:
        """LLM entities first; regex reason only as fallback."""
        from chat.services.leave.reason_value import (
            extract_reason_value,
            is_boilerplate_leave_reason,
        )

        from chat.services.leave.reason_value import reason_grounded_in_message

        reason = str(entities.get("reason") or entities.get("description") or "").strip()
        if reason and is_boilerplate_leave_reason(reason):
            reason = str(extract_reason_value(message) or "").strip()
        if reason and not reason_grounded_in_message(reason, message):
            reason = str(extract_reason_value(message) or "").strip()
        existing = str(draft.get("reason") or "").strip()
        if existing and is_boilerplate_leave_reason(existing):
            existing = ""
        generic_implied = bool(draft.get("_reason_implied")) or existing.startswith(
            "অসুস্থতা"
        )
        if (
            reason
            and len(reason) >= 3
            and reason_grounded_in_message(reason, message)
            and (overwrite or not existing or generic_implied)
        ):
            draft["reason"] = reason[:2000]
            draft.pop("_reason_implied", None)
        elif not draft.get("reason"):
            rules_reason = extract_reason_from_message(message)
            if rules_reason:
                draft["reason"] = rules_reason
                draft.pop("_reason_implied", None)

        lt = entities.get("leave_type")
        if lt and (overwrite or not draft.get("leave_type")):
            from chat.services.leave_draft_utils import should_auto_infer_wizard_leave_type

            if explicit_leave_type_from_message(message):
                draft["leave_type"] = str(lt).strip().lower()
            elif (
                str(lt).strip().lower() == "sick"
                and should_auto_infer_wizard_leave_type(draft)
            ):
                draft["leave_type"] = "sick"

    @staticmethod
    def _normalize_draft(draft: dict[str, Any]) -> None:
        from chat.services.leave.normalization import normalize_leave_draft

        normalize_leave_draft(draft)
