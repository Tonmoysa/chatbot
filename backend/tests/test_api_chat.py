import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from chat.authentication import ServicePrincipal


@pytest.fixture
def api_client(settings):
    settings.HR_SERVICE_API_KEY = "test-key"
    settings.ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
    c = APIClient()
    c.force_authenticate(user=ServicePrincipal())
    c.defaults["HTTP_X_API_KEY"] = "test-key"
    return c


@pytest.mark.django_db
def test_health_no_auth():
    c = APIClient()
    r = c.get(reverse("chat:health"))
    assert r.status_code == 200
    assert r.json()["status"] in {"success", "degraded"}


@pytest.mark.django_db
def test_chat_leave_balance(api_client):
    r = api_client.post(
        reverse("chat:chat"),
        {
            "company_id": "company-a",
            "employee_id": "E001",
            "session_id": "session-a",
            "message": "What is my remaining PTO balance?",
        },
        format="json",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert "LEAVE_BALANCE" in body["intent"]
    assert "X-Session-Id" in r
