"""
Canonical leave request payload for external HR/CRM (e.g. PHP SaaS).

Chatbot only validates and builds this structure — it does not approve leave.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from chat.services.leave_policies import get_company_leave_policy
from chat.services.leave_validation import medical_doc_required
from chat.services.leave_workflow import LEAVE_PAYMENT_LWOP, supporting_document_needed

logger = logging.getLogger(__name__)


def _map_leave_category(pay: str) -> str:
    s = (pay or "").strip().lower()
    return "unpaid" if s == LEAVE_PAYMENT_LWOP else "paid"


def _map_duration_type(scope: str) -> str:
    s = (scope or "full").strip().lower()
    if s.startswith("half"):
        return "half_day"
    return "full_day"


def build_leave_submission_payload(
    *,
    company_id: str,
    employee_id: str,
    session_id: str,
    entities: dict[str, Any],
    decision: dict[str, Any] | None = None,
    trace_id: str = "",
) -> dict[str, Any]:
    """
    Future PHP / CRM contract. Keep field names stable for autosave into CRM forms.
    """
    pay = str(entities.get("leave_payment_category") or "").strip().lower()
    scope = str(entities.get("day_scope") or "full").strip().lower()
    start = str(entities.get("start_date") or "").strip()
    end = str(entities.get("end_date") or start or "").strip()
    policy = get_company_leave_policy((company_id or "").strip() or "default")

    attachment_required = bool(
        supporting_document_needed(entities) or medical_doc_required(entities, policy)
    )

    doc = str(entities.get("document_text") or "").strip()
    attachments: list[dict[str, Any]] = []
    if doc:
        attachments.append(
            {
                "kind": "inline_text",
                "preview_chars": min(500, len(doc)),
            }
        )

    dec = decision or {}
    payload = {
        "company_id": str(company_id or "").strip(),
        "employee_id": str(employee_id or "").strip(),
        "session_id": str(session_id or "").strip(),
        "trace_id": str(trace_id or "").strip(),
        "leave_type": str(entities.get("leave_type") or "").strip().lower() or None,
        "leave_category": _map_leave_category(pay),
        "duration_type": _map_duration_type(scope),
        "start_date": start or None,
        "end_date": end or None,
        "reason": str(entities.get("reason") or "").strip() or None,
        "paid_leave_days": entities.get("paid_leave_days"),
        "unpaid_leave_days": entities.get("unpaid_leave_days"),
        "attachments": attachments,
        "attachment_required": attachment_required,
        "requested_from": "chatbot",
        "crm_workflow_hint": {
            "leave_status": dec.get("leave_status"),
            "route_to": dec.get("route_to"),
            "approval_chain": dec.get("approval_chain"),
            "rules_applied": list(dec.get("rules_applied") or []),
        },
    }
    # Strip None string keys optional - keep explicit nulls for JSON clarity
    logger.debug(
        "leave_payload_built company=%s employee=%s keys=%s",
        payload["company_id"],
        payload["employee_id"],
        list(payload.keys()),
    )
    return payload


def leave_payload_to_json(payload: dict[str, Any]) -> str:
    """Stable JSON for logging and future HTTP bodies."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
