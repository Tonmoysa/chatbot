import pytest
from django.urls import reverse
from rest_framework.test import APIClient

from chat.authentication import ServicePrincipal


@pytest.fixture
def api_client(settings):
    settings.HR_SERVICE_API_KEY = "test-key"
    c = APIClient()
    c.force_authenticate(user=ServicePrincipal())
    c.defaults["HTTP_X_API_KEY"] = "test-key"
    return c


@pytest.mark.django_db
def test_health_no_auth():
    c = APIClient()
    r = c.get(reverse("chat:health"))
    assert r.status_code == 200
    assert r.json()["status"] == "success"


@pytest.mark.django_db
def test_chat_leave_balance(api_client):
    r = api_client.post(
        reverse("chat:chat"),
        {"message": "What is my remaining PTO balance?", "employee_id": "E001"},
        format="json",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "success"
    assert "LEAVE_BALANCE" in body["intent"]
    assert "X-Session-Id" in r
