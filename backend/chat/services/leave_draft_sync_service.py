"""
Future-ready leave draft sync to CRM (no live API yet).

When PHP exposes PATCH /leave-drafts/{id}, implement ``CrmLeaveDraftBackend``.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from chat.services.leave_fsm import (
    KEY_CRM_DRAFT_ID,
    normalize_workflow_state,
    read_leave_state,
)
from chat.services.leave_payload import build_leave_submission_payload, leave_payload_to_json

logger = logging.getLogger("hr_chatbot.leave_draft_sync")


@dataclass(frozen=True)
class DraftSyncResult:
    """Outcome of a draft sync attempt (local or future CRM)."""

    ok: bool
    crm_draft_id: str | None = None
    detail: str = ""
    mode: str = "local_only"  # local_only | crm_patch | crm_create


class LeaveDraftBackend(ABC):
    """CRM transport for draft autosave — swap when API is available."""

    @abstractmethod
    def upsert_draft(
        self,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
        crm_draft_id: str | None,
        payload: dict[str, Any],
        trace_id: str = "",
    ) -> DraftSyncResult:
        """PATCH existing draft or POST new draft; return stable crm_draft_id."""


class LocalOnlyDraftBackend(LeaveDraftBackend):
    """Default until CRM draft endpoints exist."""

    def upsert_draft(
        self,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
        crm_draft_id: str | None,
        payload: dict[str, Any],
        trace_id: str = "",
    ) -> DraftSyncResult:
        del company_id, employee_id, payload, trace_id
        sid = (session_id or "")[:8] or "anon"
        local_id = crm_draft_id or f"local-draft-{sid}"
        logger.debug("leave_draft_sync_local_only crm_draft_id=%s", local_id)
        return DraftSyncResult(
            ok=True,
            crm_draft_id=local_id,
            detail="Draft held in chat session until submit (CRM draft API not wired).",
            mode="local_only",
        )


class LeaveDraftSyncService:
    """
    Prepare draft payloads for future CRM sync without coupling workflow code to HTTP.
    """

    def __init__(self, backend: LeaveDraftBackend | None = None) -> None:
        self._backend = backend or LocalOnlyDraftBackend()

    def build_sync_payload(
        self,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
        draft: dict[str, Any],
        trace_id: str = "",
    ) -> dict[str, Any]:
        entities = {
            "start_date": draft.get("start_date"),
            "end_date": draft.get("end_date"),
            "leave_type": draft.get("leave_type"),
            "reason": draft.get("reason"),
            "leave_payment_category": draft.get("leave_payment_category"),
            "day_scope": draft.get("day_scope"),
            "document_text": draft.get("document_text"),
        }
        return build_leave_submission_payload(
            company_id=company_id,
            employee_id=employee_id,
            session_id=session_id,
            entities=entities,
            decision=None,
            trace_id=trace_id,
        )

    def sync_draft(
        self,
        workflow_state: dict[str, Any],
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
        draft: dict[str, Any],
        trace_id: str = "",
    ) -> tuple[dict[str, Any], DraftSyncResult]:
        """
        Run draft sync hook and attach ``crm_draft_id`` to workflow_state when successful.
        """
        wf = normalize_workflow_state(workflow_state)
        st = read_leave_state(wf)
        if st.get("locked"):
            return wf, DraftSyncResult(ok=False, detail="Workflow already submitted.")

        payload = self.build_sync_payload(
            company_id=company_id,
            employee_id=employee_id,
            session_id=session_id,
            draft=draft,
            trace_id=trace_id,
        )
        logger.info(
            "leave_draft_sync_prepared trace_id=%s payload_chars=%s",
            trace_id,
            len(leave_payload_to_json(payload)),
        )
        result = self._backend.upsert_draft(
            company_id=company_id,
            employee_id=employee_id,
            session_id=session_id,
            crm_draft_id=st.get("crm_draft_id") or None,
            payload=payload,
            trace_id=trace_id,
        )
        if result.ok and result.crm_draft_id:
            wf[KEY_CRM_DRAFT_ID] = result.crm_draft_id
        return wf, result
