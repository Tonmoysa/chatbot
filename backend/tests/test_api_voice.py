import pytest
from django.urls import reverse


@pytest.mark.django_db
def test_voice_transcribe_rejects_empty(api_client):
    url = reverse("voice:transcribe")
    r = api_client.post(url, {}, format="multipart")
    assert r.status_code == 400


@pytest.mark.django_db
def test_voice_transcribe_validation_small_file(api_client):
    from django.core.files.uploadedfile import SimpleUploadedFile

    url = reverse("voice:transcribe")
    f = SimpleUploadedFile("empty.webm", b"", content_type="audio/webm")
    r = api_client.post(url, {"file": f}, format="multipart")
    assert r.status_code == 400
    body = r.json()
    assert body.get("trace_id")
    assert body.get("status") == "failed"


@pytest.mark.django_db
def test_voice_transcribe_without_api_key_returns_502(api_client, settings):
    from django.core.files.uploadedfile import SimpleUploadedFile

    settings.OPENAI_WHISPER_API_KEY = ""
    url = reverse("voice:transcribe")
    f = SimpleUploadedFile("test.webm", b"not-real-audio", content_type="audio/webm")
    r = api_client.post(url, {"file": f}, format="multipart")
    assert r.status_code in (400, 502)
    assert r.json().get("trace_id")
