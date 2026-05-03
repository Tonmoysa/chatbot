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
    def get_leave_balance(self, employee_id: str) -> dict[str, Any]:
        ...

    @abstractmethod
    def create_request(
        self, employee_id: str, intent: str, entities: dict[str, Any], decision: dict[str, Any]
    ) -> dict[str, Any]:
        ...

    @abstractmethod
    def get_request_status(self, request_id: str) -> dict[str, Any]:
        ...
