from unittest.mock import patch

import pytest

from chat.services.orchestrator import ChatOrchestrator


@pytest.mark.django_db
def test_policy_question_while_leave_wizard_does_not_append_leave_form(settings):
    """Explicit HR policy mid-wizard must not dump the leave step after the answer."""
    settings.KB_RAG_ENABLED = True
    orch = ChatOrchestrator()
    session = orch.memory.get_or_create_session(
        company_id="company-a",
        session_id="test-policy-interrupt-session",
        employee_id="E1",
    )

    session.workflow_state = {
        "leave_request": {
            "active": True,
            "draft": {"leave_payment_category": None},
        }
    }
    session.save(update_fields=["workflow_state", "updated_at"])

    with patch(
        "chat.services.orchestrator.try_hr_policy_rag",
        return_value={
            "hit": True,
            "text": "Attendance policy: standard 9–6 working hours.",
            "sources": [{"document": "Handbook", "section": "Attendance", "snippet": "9-6", "score": 0.45}],
            "mode": "rag",
        },
    ):
        out = orch.run_chat(
            message="hello I want to know Attendance & Working Hours Policy",
            session_id=session.session_id,
            company_id="company-a",
            employee_id="E1",
            trace_id="trace-policy-interrupt",
        )

    msg = out["response"]["message"]
    assert out["intent"] == "HR_POLICY"
    assert "Attendance policy" in msg
    assert "প্রথম প্রশ্ন (১/৫)" not in msg
    assert "ছুটি ফর্ম এখনও চালু" not in msg

    session.refresh_from_db()
    lr = (session.workflow_state or {}).get("leave_request") or {}
    assert lr.get("paused") is True
    assert lr.get("active") is False


@pytest.mark.django_db
def test_policy_question_not_forced_leave_after_recent_leave_assistant_turn(settings):
    """Short policy follow-ups must not inherit LEAVE_REQUEST from last-assistant heuristic."""
    settings.KB_RAG_ENABLED = True
    orch = ChatOrchestrator()
    session = orch.memory.get_or_create_session(
        company_id="company-a",
        session_id="test-followup-policy-session",
        employee_id="E1",
    )
    orch.memory.append(
        session,
        "assistant",
        "কোন তারিখ(গুলো) ছুটি চান?\n• ছুটি আবেদন — নিচে উত্তর দিন",
    )
    session.save()

    with patch(
        "chat.services.orchestrator.try_hr_policy_rag",
        return_value={
            "hit": True,
            "text": "Cybersecurity: do not expose credentials.",
            "sources": [{"document": "KB", "section": "Cyber", "snippet": "x", "score": 0.9}],
            "mode": "rag",
        },
    ):
        out = orch.run_chat(
            message="Cybersecurity Rules ta amake bolo",
            session_id=session.session_id,
            company_id="company-a",
            employee_id="E1",
            trace_id="trace-followup-policy",
        )

    assert out["intent"] == "HR_POLICY"
    # Reply may be auto-translated to Bengali while keeping the cybersecurity topic.
    assert "Cybersecurity" in out["response"]["message"] or "সাইবার" in out["response"]["message"]
