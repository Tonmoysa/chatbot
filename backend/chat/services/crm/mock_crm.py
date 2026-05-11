import uuid
from datetime import datetime
from typing import Any

from chat.constants import EXPENSE_DAY_CAP_BDT
from chat.services.crm.base import CRMAdapter
from chat.services.leave_days import compute_requested_leave_days


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
        self._balances: dict[str, float] = {}

    def health(self) -> dict[str, Any]:
        return {"crm": "mock", "ok": True}

    def get_leave_balance(self, employee_id: str) -> dict[str, Any]:
        emp = (employee_id or "").strip() or "demo-employee"
        if emp not in self._balances:
            self._balances[emp] = self._default_balance(emp)
        return {
            "employee_id": emp,
            "leave_balance_days": float(self._balances[emp]),
            "as_of": datetime.utcnow().isoformat() + "Z",
        }

    def get_expense_day_approved_total(
        self, employee_id: str, incurred_date_iso: str
    ) -> dict[str, Any]:
        emp = (employee_id or "").strip() or "demo-employee"
        target = (incurred_date_iso or "").strip().split("T")[0]
        total = 0.0
        for rec in self._requests.values():
            if rec.get("employee_id") != emp or rec.get("intent") != "EXPENSE_CLAIM":
                continue
            dec = rec.get("decision") or {}
            if dec.get("outcome") != "AUTO_APPROVED":
                continue
            ent = rec.get("entities") or {}
            inc = str(ent.get("expense_incurred_date") or ent.get("date") or "").split("T")[
                0
            ]
            if not inc or inc != target:
                continue
            try:
                total += float(ent.get("amount") or 0)
            except (TypeError, ValueError):
                pass
        return {"expense_day_approved_total": float(total)}

    def get_expense_day_breakdown(
        self, employee_id: str, incurred_date_iso: str
    ) -> dict[str, Any]:
        emp = (employee_id or "").strip() or "demo-employee"
        target = (incurred_date_iso or "").strip().split("T")[0]
        entries: list[dict[str, Any]] = []
        logged_total = 0.0
        for rid, rec in sorted(
            self._requests.items(),
            key=lambda kv: str((kv[1] or {}).get("created_at") or ""),
        ):
            if rec.get("employee_id") != emp or rec.get("intent") != "EXPENSE_CLAIM":
                continue
            ent = rec.get("entities") or {}
            inc = str(ent.get("expense_incurred_date") or ent.get("date") or "").split("T")[0]
            if not inc or inc != target:
                continue
            try:
                amt = float(ent.get("amount") or 0)
            except (TypeError, ValueError):
                amt = 0.0
            dec = rec.get("decision") or {}
            outcome = str(dec.get("outcome") or "")
            entries.append(
                {
                    "request_id": rid,
                    "amount": amt,
                    "outcome": outcome,
                    "status": rec.get("status") or "",
                }
            )
            logged_total += amt
        approved = float(
            self.get_expense_day_approved_total(emp, target).get("expense_day_approved_total") or 0
        )
        return {
            "expense_incurred_date": target,
            "expense_day_approved_total": approved,
            "expense_day_logged_total": float(logged_total),
            "expense_daily_cap_bdt": float(EXPENSE_DAY_CAP_BDT),
            "expense_day_entries": entries,
        }

    def _default_balance(self, employee_id: str) -> float:
        # Convenience knobs for demos
        if employee_id.upper().endswith("LOW"):
            return 0.5
        return 12.0

    def _requested_leave_days(self, entities: dict[str, Any]) -> float:
        return compute_requested_leave_days(entities or {})

    def create_request(
        self,
        employee_id: str,
        intent: str,
        entities: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        emp = (employee_id or "").strip() or "demo-employee"
        if emp not in self._balances:
            self._balances[emp] = self._default_balance(emp)

        rid = f"MOCK-{uuid.uuid4().hex[:10].upper()}"
        # Real-feel simulation: approved leave reduces leave balance
        if intent == "LEAVE_REQUEST" and (decision or {}).get("outcome") == "APPROVED":
            used = self._requested_leave_days(entities or {})
            self._balances[emp] = max(0.0, float(self._balances[emp]) - float(used))

        self._requests[rid] = {
            "request_id": rid,
            "employee_id": emp,
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
