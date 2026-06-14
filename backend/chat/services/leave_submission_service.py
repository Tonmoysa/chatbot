"""
Single outbound path for confirmed leave requests.

Conversation → workflow → review → confirm → LeaveSubmissionService → CRM adapter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from chat.services.crm.base import CRMAdapter
from chat.services.leave_fsm import (
    is_leave_submission_locked,
    mark_submitted,
    normalize_workflow_state,
    read_leave_state,
)
from chat.services.leave_days import leave_booking_signature
from chat.services.leave_payload import build_leave_submission_payload

logger = logging.getLogger("hr_chatbot.leave_submission")


@dataclass(frozen=True)
class LeaveSubmissionResult:
    ok: bool
    submission_id: str
    workflow_state: dict[str, Any]
    payload: dict[str, Any]
    crm_response: dict[str, Any]
    deduped: bool = False
    detail: str = ""


class LeaveSubmissionService:
    """
    Centralized leave submit — the only module that should call CRM for leave create.
    """

    def __init__(self, crm: CRMAdapter) -> None:
        self._crm = crm

    def submit_confirmed_leave(
        self,
        *,
        workflow_state: dict[str, Any],
        company_id: str,
        employee_id: str,
        session_id: str,
        entities: dict[str, Any],
        decision: dict[str, Any],
        trace_id: str = "",
        idempotency_key: str = "",
    ) -> LeaveSubmissionResult:
        wf = normalize_workflow_state(workflow_state)
        st = read_leave_state(wf)

        if is_leave_submission_locked(wf):
            existing = str(st.get("submission_id") or "")
            return LeaveSubmissionResult(
                ok=True,
                submission_id=existing,
                workflow_state=wf,
                payload={},
                crm_response={"reference_id": existing, "_deduped": True},
                deduped=True,
                detail="Leave already submitted for this session.",
            )

        from chat.services.leave_meta_queries import block_duplicate_submitted_leave_dates

        dup_msg = block_duplicate_submitted_leave_dates(wf, entities)
        if dup_msg:
            return LeaveSubmissionResult(
                ok=False,
                submission_id="",
                workflow_state=wf,
                payload={},
                crm_response={},
                detail=dup_msg,
            )

        idem = (idempotency_key or "").strip()
        if idem and st.get("idempotency_key") == idem and st.get("submission_id"):
            from chat.services.leave.session_action_memory import record_leave_submitted

            wf = record_leave_submitted(
                wf,
                submission_id=str(st.get("submission_id") or ""),
                draft=dict(st.get("draft") or {}),
            )
            return LeaveSubmissionResult(
                ok=True,
                submission_id=st["submission_id"],
                workflow_state=wf,
                payload={},
                crm_response={"reference_id": st["submission_id"], "_deduped": True},
                deduped=True,
                detail="Idempotent replay of prior submission.",
            )

        prior_rid = str(st.get("submission_id") or "")
        if prior_rid:
            cur_sig = leave_booking_signature(entities)
            prev_sig = leave_booking_signature(dict(st.get("draft") or {}))
            if cur_sig[0] and cur_sig == prev_sig:
                wf_locked = mark_submitted(
                    wf,
                    draft=dict(st.get("draft") or {}),
                    submission_id=prior_rid,
                    idempotency_key=str(st.get("idempotency_key") or idem),
                )
                from chat.services.leave.session_action_memory import record_leave_submitted

                wf_locked = record_leave_submitted(
                    wf_locked,
                    submission_id=prior_rid,
                    draft=dict(st.get("draft") or {}),
                )
                return LeaveSubmissionResult(
                    ok=True,
                    submission_id=prior_rid,
                    workflow_state=wf_locked,
                    payload={},
                    crm_response={"reference_id": prior_rid, "_deduped": True},
                    deduped=True,
                    detail="Duplicate booking matches prior submission in this session.",
                )

        pl = build_leave_submission_payload(
            company_id=company_id,
            employee_id=employee_id,
            session_id=session_id,
            entities=dict(entities),
            decision=dict(decision),
            trace_id=trace_id,
        )
        if st.get("crm_draft_id"):
            pl["crm_draft_id"] = st["crm_draft_id"]

        crm_out = self._crm.submit_leave(
            pl,
            company_id=company_id,
            employee_id=employee_id,
            session_id=session_id,
            idempotency_key=idem,
        )
        submission_id = str(crm_out.get("reference_id") or crm_out.get("request_id") or "")
        if not submission_id:
            logger.error("leave_submission_missing_reference trace_id=%s", trace_id)
            return LeaveSubmissionResult(
                ok=False,
                submission_id="",
                workflow_state=wf,
                payload=pl,
                crm_response=crm_out,
                detail="CRM did not return a submission reference.",
            )

        draft = dict(st.get("draft") or {})
        for k in (
            "leave_type",
            "leave_payment_category",
            "day_scope",
            "start_date",
            "end_date",
            "reason",
        ):
            if entities.get(k) is not None:
                draft[k] = entities[k]

        wf_locked = mark_submitted(
            wf,
            draft=draft,
            submission_id=submission_id,
            idempotency_key=idem,
        )
        from chat.services.leave.session_action_memory import record_leave_submitted

        wf_locked = record_leave_submitted(
            wf_locked,
            submission_id=submission_id,
            draft=draft,
        )
        logger.info(
            "leave_submission_complete trace_id=%s submission_id=%s idempotency=%s",
            trace_id,
            submission_id,
            idem or "(none)",
        )
        return LeaveSubmissionResult(
            ok=True,
            submission_id=submission_id,
            workflow_state=wf_locked,
            payload=pl,
            crm_response=crm_out,
            deduped=bool(crm_out.get("_deduped")),
        )


def submit_leave_request(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Deprecated: use LeaveSubmissionService + CRMAdapter.submit_leave.
    Kept for imports/tests that patch this symbol.
    """
    from chat.services.crm.factory import get_crm_adapter

    svc = LeaveSubmissionService(get_crm_adapter())
    wf: dict[str, Any] = {}
    result = svc.submit_confirmed_leave(
        workflow_state=wf,
        company_id=str(payload.get("company_id") or ""),
        employee_id=str(payload.get("employee_id") or ""),
        session_id=str(payload.get("session_id") or ""),
        entities={
            "leave_type": payload.get("leave_type"),
            "leave_payment_category": payload.get("leave_category"),
            "day_scope": "half" if payload.get("duration_type") == "half_day" else "full",
            "start_date": payload.get("start_date"),
            "end_date": payload.get("end_date"),
            "reason": payload.get("reason"),
            "document_text": None,
        },
        decision=dict(payload.get("crm_workflow_hint") or {}),
        trace_id=str(payload.get("trace_id") or ""),
        idempotency_key="",
    )
    return {
        "ok": result.ok,
        "reference_id": result.submission_id,
        "status": "accepted_for_crm" if result.ok else "failed",
        "detail": result.detail,
        "_deduped": result.deduped,
    }
