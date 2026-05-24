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
        self._balances: dict[tuple[str, str], float] = {}
        self._idempotency: dict[tuple[str, str], str] = {}

    def health(self) -> dict[str, Any]:
        return {"crm": "mock", "ok": True}

    def list_employee_leave_requests(
        self,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        company, emp = self._identity(company_id, employee_id, session_id)
        rows: list[dict[str, Any]] = []
        for rid, rec in self._requests.items():
            if (
                rec.get("company_id") == company
                and rec.get("employee_id") == emp
                and rec.get("intent") == "LEAVE_REQUEST"
            ):
                rows.append(
                    {
                        "request_id": rid,
                        "company_id": company,
                        "employee_id": emp,
                        "status": rec.get("status"),
                        "leave_status": (rec.get("decision") or {}).get("leave_status"),
                        "entities": rec.get("entities") or {},
                    }
                )
        return {"leave_requests": rows}

    def get_leave_balance(
        self,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        company, emp = self._identity(company_id, employee_id, session_id)
        key = (company, emp)
        if key not in self._balances:
            self._balances[key] = self._default_balance(emp)
        return {
            "company_id": company,
            "employee_id": emp,
            "session_id": session_id,
            "leave_balance_days": float(self._balances[key]),
            "as_of": datetime.utcnow().isoformat() + "Z",
        }

    def get_expense_day_approved_total(
        self,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
        incurred_date_iso: str,
    ) -> dict[str, Any]:
        company, emp = self._identity(company_id, employee_id, session_id)
        target = (incurred_date_iso or "").strip().split("T")[0]
        total = 0.0
        for rec in self._requests.values():
            if (
                rec.get("company_id") != company
                or rec.get("employee_id") != emp
                or rec.get("intent") != "EXPENSE_CLAIM"
            ):
                continue
            dec = rec.get("decision") or {}
            outcome = str(dec.get("outcome") or "")
            if outcome not in ("AUTO_APPROVED", "SUBMITTED"):
                continue
            ent = rec.get("entities") or {}
            inc = str(ent.get("expense_incurred_date") or ent.get("date") or "").split("T")[
                0
            ]
            if not inc or inc != target:
                continue
            try:
                if ent.get("expense_items"):
                    total += sum(
                        float(x.get("amount") or 0) for x in (ent.get("expense_items") or [])
                    )
                else:
                    total += float(ent.get("amount") or 0)
            except (TypeError, ValueError):
                pass
        return {"expense_day_approved_total": float(total)}

    def get_expense_day_breakdown(
        self,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
        incurred_date_iso: str,
    ) -> dict[str, Any]:
        company, emp = self._identity(company_id, employee_id, session_id)
        target = (incurred_date_iso or "").strip().split("T")[0]
        entries: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        logged_total = 0.0
        for rid, rec in sorted(
            self._requests.items(),
            key=lambda kv: str((kv[1] or {}).get("created_at") or ""),
        ):
            if (
                rec.get("company_id") != company
                or rec.get("employee_id") != emp
                or rec.get("intent") != "EXPENSE_CLAIM"
            ):
                continue
            ent = rec.get("entities") or {}
            inc = str(ent.get("expense_incurred_date") or ent.get("date") or "").split("T")[0]
            if not inc or inc != target:
                continue
            dec = rec.get("decision") or {}
            outcome = str(dec.get("outcome") or "")
            line_items = list(ent.get("expense_items") or [])
            if line_items:
                subtotal = sum(float(x.get("amount") or 0) for x in line_items)
                logged_total += subtotal
                for row in line_items:
                    items.append(dict(row))
                entries.append(
                    {
                        "request_id": rid,
                        "amount": subtotal,
                        "outcome": outcome,
                        "status": rec.get("status") or "",
                        "line_count": len(line_items),
                    }
                )
            else:
                try:
                    amt = float(ent.get("amount") or 0)
                except (TypeError, ValueError):
                    amt = 0.0
                logged_total += amt
                entries.append(
                    {
                        "request_id": rid,
                        "amount": amt,
                        "outcome": outcome,
                        "status": rec.get("status") or "",
                    }
                )
        approved = float(
            self.get_expense_day_approved_total(
                company_id=company,
                employee_id=emp,
                session_id=session_id,
                incurred_date_iso=target,
            ).get("expense_day_approved_total")
            or 0
        )
        return {
            "expense_incurred_date": target,
            "expense_day_approved_total": approved,
            "expense_day_logged_total": float(logged_total),
            "expense_daily_cap_bdt": float(EXPENSE_DAY_CAP_BDT),
            "expense_day_entries": entries,
            "expense_day_items": items,
        }

    def _default_balance(self, employee_id: str) -> float:
        # Convenience knobs for demos
        if employee_id.upper().endswith("LOW"):
            return 0.5
        return 12.0

    def _identity(self, company_id: str, employee_id: str, session_id: str) -> tuple[str, str]:
        company = (company_id or "").strip()
        emp = (employee_id or "").strip()
        sid = (session_id or "").strip()
        if not company or not emp or not sid:
            raise ValueError("company_id, employee_id, and session_id are required.")
        return company, emp

    def _requested_leave_days(self, entities: dict[str, Any]) -> float:
        return compute_requested_leave_days(entities or {})

    def submit_leave(
        self,
        payload: dict[str, Any],
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        company, emp = self._identity(company_id, employee_id, session_id)
        idem = (idempotency_key or "").strip()
        if idem and (company, idem) in self._idempotency:
            rid = self._idempotency[(company, idem)]
            return {
                "ok": True,
                "reference_id": rid,
                "request_id": rid,
                "status": "accepted_for_crm",
                "_idempotent_replay": True,
            }

        ref = f"PHP-LEAVE-{uuid.uuid4().hex[:12].upper()}"
        record = {
            "request_id": ref,
            "reference_id": ref,
            "company_id": company,
            "employee_id": emp,
            "session_id": session_id,
            "idempotency_key": idem,
            "intent": "LEAVE_REQUEST",
            "payload": payload,
            "status": "PENDING",
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        self._requests[ref] = record
        if idem:
            self._idempotency[(company, idem)] = ref
        return {
            "ok": True,
            "reference_id": ref,
            "request_id": ref,
            "status": "accepted_for_crm",
            "detail": "Simulated leave submit; replace with PHP CRM API.",
        }

    def create_request(
        self,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
        intent: str,
        entities: dict[str, Any],
        decision: dict[str, Any],
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        company, emp = self._identity(company_id, employee_id, session_id)
        idem = (idempotency_key or "").strip()
        if idem and (company, idem) in self._idempotency:
            rid = self._idempotency[(company, idem)]
            return {"request_id": rid, "record": self._requests[rid], "_idempotent_replay": True}

        bal_key = (company, emp)
        if bal_key not in self._balances:
            self._balances[bal_key] = self._default_balance(emp)

        rid = f"MOCK-{uuid.uuid4().hex[:10].upper()}"
        # Leave balance is updated only in CRM after approval — not when logging chatbot submission.

        leave_status = (decision or {}).get("leave_status")
        crm_status = self._initial_status(decision)
        if intent == "LEAVE_REQUEST" and leave_status:
            crm_status = self._map_leave_lifecycle_status(str(leave_status))

        self._requests[rid] = {
            "request_id": rid,
            "company_id": company,
            "employee_id": emp,
            "session_id": session_id,
            "idempotency_key": idem,
            "intent": intent,
            "entities": entities,
            "decision": decision,
            "status": crm_status,
            "leave_status": leave_status or crm_status,
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
        if idem:
            self._idempotency[(company, idem)] = rid
        return {"request_id": rid, "record": self._requests[rid]}

    def _initial_status(self, decision: dict[str, Any]) -> str:
        outcome = (decision or {}).get("outcome")
        mapping = {
            "AUTO_APPROVED": "APPROVED",
            "APPROVED": "APPROVED",
            "SUBMITTED": "PENDING",
            "REJECTED": "REJECTED",
            "PENDING_APPROVAL": "PENDING",
            "PENDING_REVIEW": "PENDING_REVIEW",
            "INFORMATIONAL": "COMPLETED",
            "NEEDS_CLARIFICATION": "DRAFT",
        }
        return mapping.get(str(outcome), "PENDING")

    @staticmethod
    def _map_leave_lifecycle_status(leave_status: str) -> str:
        m = {
            "approved": "APPROVED",
            "rejected": "REJECTED",
            "cancelled": "CANCELLED",
            "pending": "PENDING",
            "manager_review": "PENDING",
            "hr_review": "PENDING",
            "draft": "DRAFT",
        }
        return m.get(leave_status.lower(), "PENDING")

    def get_request_status(
        self,
        request_id: str,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        company, emp = self._identity(company_id, employee_id, session_id)
        rec = self._requests.get(request_id)
        if not rec or rec.get("company_id") != company or rec.get("employee_id") != emp:
            return {"request_id": request_id, "status": "NOT_FOUND", "detail": "Unknown request"}
        return {
            "request_id": request_id,
            "status": rec["status"],
            "intent": rec.get("intent"),
            "updated_at": rec.get("created_at"),
        }
