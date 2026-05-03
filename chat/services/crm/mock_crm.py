import uuid
from datetime import datetime
from typing import Any

from chat.services.crm.base import CRMAdapter


_MOCK_SINGLETON: "MockCRMAdapter | None" = None


def get_mock_singleton() -> "MockCRMAdapter":
    global _MOCK_SINGLETON
    if _MOCK_SINGLETON is None:
        _MOCK_SINGLETON = MockCRMAdapter()
    return _MOCK_SINGLETON


class MockCRMAdapter(CRMAdapter):
    """
    In-memory CRM for local testing. Dummy data per employee_id prefix.
    """

    def __init__(self) -> None:
        self._requests: dict[str, dict[str, Any]] = {}

    def health(self) -> dict[str, Any]:
        return {"crm": "mock", "ok": True}

    def get_leave_balance(self, employee_id: str) -> dict[str, Any]:
        base = 12.0
        if employee_id.upper().endswith("LOW"):
            base = 0.5
        return {
            "employee_id": employee_id,
            "leave_balance_days": base,
            "as_of": datetime.utcnow().isoformat() + "Z",
        }

    def create_request(
        self,
        employee_id: str,
        intent: str,
        entities: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        rid = f"MOCK-{uuid.uuid4().hex[:10].upper()}"
        self._requests[rid] = {
            "request_id": rid,
            "employee_id": employee_id,
            "intent": intent,
            "entities": entities,
            "decision": decision,
            "status": self._initial_status(decision),
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        return {"request_id": rid, "record": self._requests[rid]}

    def _initial_status(self, decision: dict[str, Any]) -> str:
        outcome = (decision or {}).get("outcome")
        mapping = {
            "AUTO_APPROVED": "APPROVED",
            "APPROVED": "APPROVED",
            "REJECTED": "REJECTED",
            "PENDING_APPROVAL": "PENDING",
            "PENDING_REVIEW": "PENDING_REVIEW",
            "INFORMATIONAL": "COMPLETED",
            "NEEDS_CLARIFICATION": "DRAFT",
        }
        return mapping.get(str(outcome), "PENDING")

    def get_request_status(self, request_id: str) -> dict[str, Any]:
        rec = self._requests.get(request_id)
        if not rec:
            return {"request_id": request_id, "status": "NOT_FOUND", "detail": "Unknown request"}
        return {
            "request_id": request_id,
            "status": rec["status"],
            "intent": rec.get("intent"),
            "updated_at": rec.get("created_at"),
        }
