from abc import ABC, abstractmethod
from typing import Any


class CRMError(Exception):
    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class CRMAdapter(ABC):
    @abstractmethod
    def health(self) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_leave_balance(
        self,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        ...

    def list_employee_leave_requests(
        self,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """
        Active/recent leave rows for overlap detection. Real CRM may implement
        GET /employees/{id}/leave-requests/; default empty list.
        """
        return {"leave_requests": []}

    @abstractmethod
    def get_expense_day_approved_total(
        self,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
        incurred_date_iso: str,
    ) -> dict[str, Any]:
        """Sum of same-day AUTO_APPROVED expense amounts for policy checks (mock/CRM)."""
        ...

    @abstractmethod
    def get_expense_day_breakdown(
        self,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
        incurred_date_iso: str,
    ) -> dict[str, Any]:
        """
        Same calendar day expense lines for the employee (request id, amount, outcome, status)
        plus expense_day_approved_total, expense_day_logged_total, expense_daily_cap_bdt.
        """

    @abstractmethod
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
        ...

    @abstractmethod
    def get_request_status(
        self,
        request_id: str,
        *,
        company_id: str,
        employee_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        ...
