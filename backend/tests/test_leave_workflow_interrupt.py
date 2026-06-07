"""Leave wizard: interruptions, patch merge, auto-resume."""

import datetime as dt

import pytest

from chat.services.leave_workflow import (
    is_leave_collecting,
    merge_extractor_entities,
    process_leave_turn,
)


def test_merge_extractor_entities_does_not_overwrite_filled_slots():
    draft = {
        "leave_type": "sick",
        "leave_payment_category": "paid",
        "day_scope": "full",
    }
    merge_extractor_entities(
        draft,
        {
            "leave_type": "casual",
            "leave_payment_category": "lwop",
            "day_scope": "half",
            "reason": "travel",
        },
    )
    assert draft["leave_type"] == "sick"
    assert draft["leave_payment_category"] == "paid"
    assert draft["day_scope"] == "full"
    assert draft.get("reason") == "travel"


@pytest.mark.django_db
def test_greeting_mid_wizard_preserves_draft_and_resumes(monkeypatch):
    fixed = dt.date(2026, 5, 7)

    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)

    wf: dict = {}
    r1 = process_leave_turn(
        workflow_state=wf,
        message="ami kalke sick leave nite chai",
        entities={},
        company_id="company-a",
    )
    assert not r1["complete"]
    assert is_leave_collecting(r1["workflow_state"])

    from chat.services.orchestrator import ChatOrchestrator

    orch = ChatOrchestrator()
    session = orch.memory.get_or_create_session(
        company_id="company-a",
        session_id="test-greeting-interrupt",
        employee_id="E1",
    )
    session.workflow_state = r1["workflow_state"]
    session.save(update_fields=["workflow_state", "updated_at"])

    monkeypatch.setattr(
        "chat.services.orchestrator.conversational_reply",
        lambda **kwargs: "Sure.",
    )
    out = orch.run_chat(
        message="hi",
        session_id=session.session_id,
        company_id="company-a",
        employee_id="E1",
        trace_id="trace-greeting-interrupt",
    )

    session.refresh_from_db()
    from chat.services.leave_fsm import read_leave_state

    st = read_leave_state(session.workflow_state)
    assert st.get("status") == "active"
    assert st.get("draft", {}).get("leave_type") == "sick"
    msg = out["response"]["message"]
    assert (
        "draft" in msg.lower()
        or "leave" in msg.lower()
        or "ছুটি" in msg
        or "chuti" in msg.lower()
        or "Sure." in msg
    )


@pytest.mark.django_db
def test_stepwise_sick_paid_half_no_reask(monkeypatch):
    fixed = dt.date(2026, 5, 7)

    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)
    monkeypatch.setattr("chat.services.entity_extractor.date", FixedDate)

    wf: dict = {}
    r1 = process_leave_turn(
        workflow_state=wf,
        message="ami kalke sick leave nite chai",
        entities={},
        company_id="company-a",
    )
    r2 = process_leave_turn(
        workflow_state=r1["workflow_state"],
        message="paid",
        entities={},
        company_id="company-a",
    )
    from chat.services.leave_fsm import read_leave_state

    assert (
        read_leave_state(r2["workflow_state"]).get("draft", {}).get(
            "leave_payment_category"
        )
        == "paid"
    )
    q2 = r2.get("question") or ""
    assert "leave_type" not in q2.lower() or "ধরন" not in q2

    r3 = process_leave_turn(
        workflow_state=r2["workflow_state"],
        message="half",
        entities={},
        company_id="company-a",
    )
    assert not r3["complete"]
    r4 = process_leave_turn(
        workflow_state=r3["workflow_state"],
        message="yes",
        entities={},
        company_id="company-a",
    )
    assert r4["complete"]
    assert r4.get("confirmed_submit")
    draft = r4["merged_entities"]
    assert draft.get("leave_type") == "sick"
    assert draft.get("leave_payment_category") == "paid"
    assert draft.get("day_scope") == "half"
