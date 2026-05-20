"""
Outbound leave submission boundary.

Today: simulate acceptance + structured logging.
Tomorrow: swap transport for PHP CRM HTTP client without changing payload builders.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from chat.services.leave_payload import leave_payload_to_json

logger = logging.getLogger("hr_chatbot.leave_submission")


def submit_leave_request(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Submit a validated leave payload to the enterprise HR layer.

    No approval occurs here — CRM/workflow owns final state.
    """
    body_preview = leave_payload_to_json(payload)
    logger.info("leave_submission_accepted payload=%s", body_preview)

    reference = f"PHP-LEAVE-{uuid.uuid4().hex[:12].upper()}"
    return {
        "ok": True,
        "reference_id": reference,
        "status": "accepted_for_crm",
        "detail": "Simulated queue; replace with PHP CRM API call.",
    }
