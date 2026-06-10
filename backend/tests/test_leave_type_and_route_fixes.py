"""Non-sick leave type choice + Bengali route amount strip."""

from __future__ import annotations

from chat.services.expense_extraction import extract_expense_items, preprocess_expense_message
from chat.services.leave.conversation_manager import LeaveConversationManager
from chat.services.leave_draft_utils import apply_leave_draft_defaults
from chat.services.leave_slots import SLOT_LEAVE_TYPE, get_missing_slots
from chat.services.leave_policies import get_company_leave_policy


def test_family_program_does_not_auto_pick_annual_leave_type() -> None:
    draft = {
        "reason": "family program",
        "days": 3.0,
        "day_scope": "full",
    }
    policy = get_company_leave_policy("default")
    apply_leave_draft_defaults(draft, policy)
    assert draft.get("leave_type") is None
    missing = get_missing_slots(draft, policy=policy)
    assert SLOT_LEAVE_TYPE in missing


def test_sick_reason_may_auto_infer_leave_type() -> None:
    draft = {"reason": "onek osusto", "day_scope": "full", "start_date": "2026-06-11"}
    policy = get_company_leave_policy("default")
    apply_leave_draft_defaults(draft, policy)
    assert draft.get("leave_type") == "sick"


def test_non_sick_select_leave_prompt_excludes_sick_option() -> None:
    draft = {"reason": "family program", "days": 3.0}
    q = LeaveConversationManager().build_follow_up(
        draft,
        primary_slot=SLOT_LEAVE_TYPE,
        missing=[SLOT_LEAVE_TYPE],
    )
    assert "annual leave" in q
    assert "leave without pay" in q
    assert "sick leave" not in q.lower() or "annual leave" in q


def test_family_program_compound_message_asks_leave_type() -> None:
    from chat.services.leave_slot_extraction import extract_leave_slots
    from chat.services.leave.normalization import normalize_leave_draft

    msg = "amar 3 diner leave lagbe family program e jabo"
    ex = extract_leave_slots(msg, skip_leave_phrase_gate=True)
    draft: dict = {}
    if ex.days.value:
        draft["days"] = ex.days.value
    if ex.reason.value:
        draft["reason"] = ex.reason.value
    normalize_leave_draft(draft)
    assert draft.get("leave_type") is None
    assert draft.get("reason")
    missing = get_missing_slots(draft)
    assert SLOT_LEAVE_TYPE in missing


def test_family_program_process_leave_turn_does_not_auto_pick_annual() -> None:
    from chat.services.leave.entity_pipeline import LeaveEntityPipeline
    from chat.services.leave_workflow import process_leave_turn
    from chat.services.leave_fsm import read_leave_state

    msg = "amar 3 diner leave lagbe family program e jabo"
    pipe = LeaveEntityPipeline()
    entities = pipe.extract(
        msg,
        intent="leave_request",
        context_lines=[],
        trace_id="test",
        use_llm=False,
    ).entities
    pack = process_leave_turn(
        workflow_state={},
        message=msg,
        entities=entities,
        company_id="default",
        trace_id="test",
    )
    draft = dict(read_leave_state(pack["workflow_state"]).get("draft") or {})
    assert draft.get("leave_type") is None
    assert draft.get("reason") == "family program"
    assert draft.get("days") == 3.0
    question = str(pack.get("question") or "")
    assert "annual leave" not in question.lower() or "select" in question.lower()
    missing = get_missing_slots(draft)
    assert SLOT_LEAVE_TYPE in missing


def test_bengali_route_strips_amount_from_destination() -> None:
    msg = "আজকে উত্তরা থেকে আগারগাঁও ৭০ টাকা"
    pre = preprocess_expense_message(msg)
    assert "70" in pre
    ext = extract_expense_items(msg)
    assert len(ext.items) >= 1
    row = ext.items[0]
    assert row.amount == 70.0
    assert "70" not in (row.to_location or "")
    assert "টাকা" not in (row.to_location or "")
