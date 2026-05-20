import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from chat.authentication import ServicePrincipal
from chat.models import ConversationSession, ConversationTurn


@pytest.fixture
def api_client(settings):
    settings.HR_SERVICE_API_KEY = "test-key"
    settings.ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
    c = APIClient()
    c.force_authenticate(user=ServicePrincipal())
    c.defaults["HTTP_X_API_KEY"] = "test-key"
    return c


@pytest.mark.django_db
def test_list_sessions_empty(api_client):
    r = api_client.get(
        reverse("chat:chat-sessions"),
        {"company_id": "co-1", "employee_id": "emp-1"},
    )
    assert r.status_code == 200
    assert r.json()["sessions"] == []


@pytest.mark.django_db
def test_list_and_load_session(api_client):
    session = ConversationSession.objects.create(
        company_id="co-1",
        employee_id="emp-1",
        session_id="sess-abc",
    )
    ConversationTurn.objects.create(
        session=session,
        role=ConversationTurn.ROLE_USER,
        content="What is our leave policy?",
    )
    ConversationTurn.objects.create(
        session=session,
        role=ConversationTurn.ROLE_ASSISTANT,
        content="Here is the leave policy summary.",
    )

    listed = api_client.get(
        reverse("chat:chat-sessions"),
        {"company_id": "co-1", "employee_id": "emp-1"},
    )
    assert listed.status_code == 200
    sessions = listed.json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "sess-abc"
    assert "leave policy" in sessions[0]["title"].lower()

    detail = api_client.get(
        reverse("chat:chat-session-detail", kwargs={"session_id": "sess-abc"}),
        {"company_id": "co-1", "employee_id": "emp-1"},
    )
    assert detail.status_code == 200
    messages = detail.json()["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[1]["role"] == "assistant"
