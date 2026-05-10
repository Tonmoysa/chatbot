from django.db import models


class ConversationSession(models.Model):
    session_id = models.CharField(max_length=64, unique=True, db_index=True)
    employee_id = models.CharField(max_length=64, blank=True, default="")
    workflow_state = models.JSONField(blank=True, default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.session_id


class ConversationTurn(models.Model):
    ROLE_USER = "user"
    ROLE_ASSISTANT = "assistant"
    ROLE_CHOICES = ((ROLE_USER, "user"), (ROLE_ASSISTANT, "assistant"))

    session = models.ForeignKey(
        ConversationSession, on_delete=models.CASCADE, related_name="turns"
    )
    role = models.CharField(max_length=16, choices=ROLE_CHOICES)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
