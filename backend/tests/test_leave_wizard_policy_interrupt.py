from unittest.mock import patch

import pytest

from chat.services.leave_fsm import read_leave_state
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
        "active_flow": "leave",
        "status": "active",
        "draft": {"leave_payment_category": None},
        "step": "leave_payment_category",
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
    from chat.services.leave_fsm import read_leave_state

    st = read_leave_state(session.workflow_state)
    assert st.get("draft") is not None
    assert st.get("status") == "paused"
    assert "বেতন" not in msg
    assert "paid" not in msg.lower() or "Attendance policy" in msg


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
    msg_out = out["response"]["message"]
    assert (
        "Cybersecurity" in msg_out
        or "Cyber Security" in msg_out
        or "সাইবার" in msg_out
        or "credentials" in msg_out.lower()
        or "security" in msg_out.lower()
    )


@pytest.mark.django_db
def test_policy_during_leave_confirmation_returns_rag_not_review(settings):
    """Policy question at review step must answer from KB, not re-show confirmation."""
    settings.KB_RAG_ENABLED = True
    orch = ChatOrchestrator()
    session = orch.memory.get_or_create_session(
        company_id="company-a",
        session_id="test-policy-at-confirm",
        employee_id="E1",
    )
    session.workflow_state = {
        "active_flow": "leave",
        "status": "active",
        "review_pending": True,
        "draft": {
            "leave_type": "sick",
            "leave_payment_category": "paid",
            "day_scope": "full",
            "start_date": "2026-05-22",
            "reason": "fever",
        },
    }
    session.save(update_fields=["workflow_state", "updated_at"])

    with patch(
        "chat.services.orchestrator.try_hr_policy_rag",
        return_value={
            "hit": True,
            "text": "Leave policy: sick leave requires notice when possible.",
            "sources": [],
            "mode": "rag",
        },
    ):
        out = orch.run_chat(
            message="leave policy ta amake bolo",
            session_id=session.session_id,
            company_id="company-a",
            employee_id="E1",
            trace_id="trace-policy-at-confirm",
        )

    msg = out["response"]["message"]
    assert out["intent"] == "HR_POLICY"
    assert "জমা দেবেন" not in msg
    assert "পর্যালোচনা" not in msg
    assert "নোটিশ" in msg or "notice" in msg.lower()

    session.refresh_from_db()
    st = read_leave_state(session.workflow_state)
    assert st.get("status") == "paused"
    assert st.get("draft", {}).get("leave_type") == "sick"


@pytest.mark.django_db
def test_policy_after_submitted_leave_not_blocked_by_lock(settings):
    """After submit lock, policy questions still reach RAG (not resubmit copy)."""
    settings.KB_RAG_ENABLED = True
    orch = ChatOrchestrator()
    session = orch.memory.get_or_create_session(
        company_id="company-a",
        session_id="test-policy-after-lock",
        employee_id="E1",
    )
    session.workflow_state = {
        "status": "submitted",
        "locked": True,
        "draft": {"leave_type": "sick"},
        "submission_id": "PHP-LEAVE-TEST123",
        "idempotency_key": "idem-1",
    }
    session.save(update_fields=["workflow_state", "updated_at"])

    with patch(
        "chat.services.orchestrator.try_hr_policy_rag",
        return_value={
            "hit": True,
            "text": "Annual leave accrues monthly per handbook section 4.",
            "sources": [],
            "mode": "rag",
        },
    ):
        out = orch.run_chat(
            message="amake leave policy somporke bolo",
            session_id=session.session_id,
            company_id="company-a",
            employee_id="E1",
            trace_id="trace-policy-after-lock",
        )

    msg = out["response"]["message"]
    assert out["intent"] == "HR_POLICY"
    assert "already submitted" not in msg.lower()
    assert "৪" in msg or "handbook" in msg.lower()
