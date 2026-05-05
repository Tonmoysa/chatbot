from typing import Any

from django.db import transaction

from chat.models import ConversationSession, ConversationTurn


class ConversationMemoryStore:
    """Session-scoped history for multi-turn context (database-backed)."""

    def __init__(self, max_turns: int = 30) -> None:
        self.max_turns = max_turns

    def get_or_create_session(self, session_id: str, employee_id: str) -> ConversationSession:
        sid = (session_id or "").strip() or self._new_session_id()
        emp = (employee_id or "").strip()
        with transaction.atomic():
            s, _ = ConversationSession.objects.select_for_update().get_or_create(
                session_id=sid, defaults={"employee_id": emp}
            )
            if emp and s.employee_id != emp:
                s.employee_id = emp
                s.save(update_fields=["employee_id", "updated_at"])
        return s

    def _new_session_id(self) -> str:
        import uuid

        return uuid.uuid4().hex

    def recent_context_lines(self, session: ConversationSession, limit: int = 12) -> list[str]:
        turns = list(session.turns.order_by("-created_at")[:limit])
        turns.reverse()
        lines: list[str] = []
        for t in turns:
            who = "User" if t.role == ConversationTurn.ROLE_USER else "Assistant"
            lines.append(f"{who}: {t.content}")
        return lines

    def append(self, session: ConversationSession, role: str, content: str) -> None:
        ConversationTurn.objects.create(session=session, role=role, content=content)
        self._trim(session)

    def _trim(self, session: ConversationSession) -> None:
        ids = list(
            session.turns.order_by("-created_at").values_list("id", flat=True)[self.max_turns :]
        )
        if ids:
            ConversationTurn.objects.filter(id__in=ids).delete()
