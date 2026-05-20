"""
Canonical expense request payload for external HR/CRM (e.g. PHP SaaS).

Chatbot collects and submits — it does not approve reimbursements.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_expense_submission_payload(
    *,
    company_id: str,
    employee_id: str,
    session_id: str,
    items: list[dict[str, Any]],
    incurred_date_iso: str,
    trace_id: str = "",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    expenses: list[dict[str, Any]] = []
    total = 0.0
    for row in items:
        try:
            amt = float(row.get("amount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        total += amt
        expenses.append(
            {
                "category": str(row.get("category") or "Other").strip(),
                "amount": amt,
                "from_location": str(row.get("from_location") or "").strip(),
                "to_location": str(row.get("to_location") or "").strip(),
                "notes": str(row.get("notes") or "").strip(),
            }
        )

    payload = {
        "company_id": str(company_id or "").strip(),
        "employee_id": str(employee_id or "").strip(),
        "session_id": str(session_id or "").strip(),
        "trace_id": str(trace_id or "").strip(),
        "incurred_date": str(incurred_date_iso or "").strip(),
        "expenses": expenses,
        "total_amount": round(total, 2),
        "validation_warnings": list(warnings or []),
        "requested_from": "chatbot",
    }
    logger.debug(
        "expense_payload_built company=%s employee=%s lines=%s total=%s",
        payload["company_id"],
        payload["employee_id"],
        len(expenses),
        payload["total_amount"],
    )
    return payload


def expense_payload_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
