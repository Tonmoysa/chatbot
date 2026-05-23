"""Compound slot messages and chitchat during leave review."""

from unittest.mock import patch

import pytest

from chat.services.leave_workflow import _is_compound_slot_message


def test_is_compound_slot_message_detects_comma_list():
    assert _is_compound_slot_message("paid,sick,full day")
    assert not _is_compound_slot_message("paid")


@pytest.mark.django_db
def test_kemon_acho_during_review_gets_reply_and_keeps_draft(settings):
    settings.KB_RAG_ENABLED = False
    from chat.services.orchestrator import ChatOrchestrator

    orch = ChatOrchestrator()
    session = orch.memory.get_or_create_session(
        company_id="company-a",
        session_id="test-kemon-acho-review",
        employee_id="E1",
    )
    session.workflow_state = {
        "leave_request": {
            "active": True,
            "stage": "awaiting_confirmation",
            "draft": {
                "leave_type": "sick",
                "leave_payment_category": "paid",
                "day_scope": "full",
                "start_date": "2026-05-22",
                "end_date": "2026-05-22",
                "reason": "fever",
            },
        }
    }
    session.save(update_fields=["workflow_state", "updated_at"])

    with patch(
        "chat.services.orchestrator.conversational_reply",
        return_value="ভালো আছি, ধন্যবাদ জানতে চাওয়ার জন্য!",
    ):
        out = orch.run_chat(
            message="kemon acho?",
            session_id=session.session_id,
            company_id="company-a",
            employee_id="E1",
            trace_id="trace-kemon-acho",
        )

    msg = out["response"]["message"]
    assert out["intent"] == "UNKNOWN"
    assert "ভালো" in msg or "ধন্যবাদ" in msg
    assert "পর্যালোচনা" in msg or "জমা দেবেন" in msg

    session.refresh_from_db()
    from chat.services.leave_fsm import read_leave_state

    st = read_leave_state(session.workflow_state)
    assert st.get("draft", {}).get("leave_type") == "sick"
    assert st.get("review_pending") is True
