"""
Outbound expense submission boundary.

Today: simulate acceptance + structured logging.
Tomorrow: swap transport for PHP CRM HTTP client without changing payload builders.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from chat.services.expense_payload import expense_payload_to_json

logger = logging.getLogger("hr_chatbot.expense_submission")


def submit_expense_request(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Submit a validated expense payload to the enterprise HR layer.

    No reimbursement approval occurs here — CRM/workflow owns final state.
    """
    body_preview = expense_payload_to_json(payload)
    logger.info("expense_submission_accepted payload=%s", body_preview)

    year = datetime.now().year
    reference = f"EXP-{year}-{uuid.uuid4().hex[:6].upper()}"
    return {
        "ok": True,
        "reference_id": reference,
        "status": "accepted_for_crm",
        "detail": "Simulated queue; replace with PHP CRM API call.",
    }
