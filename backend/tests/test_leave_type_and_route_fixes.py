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


def test_agami_august_leave_chai_no_reason_no_annual(monkeypatch) -> None:
    """Cold-start application phrase must not invent reason or annual leave type."""
    import datetime as dt

    from chat.constants import INTENT_LEAVE_REQUEST
    from chat.services.entity_extractor import EntityExtractor
    from chat.services.leave.entity_pipeline import LeaveEntityPipeline
    from chat.services.leave_workflow import process_leave_turn
    from chat.services.leave_fsm import read_leave_state

    class _BadLeaveLLM:
        def is_configured(self):
            return True

        def chat_json(self, *, system_prompt, user_prompt, trace_id):
            return {
                "reason": "15 august leave chai",
                "leave_type": "annual",
                "start_date": "2026-08-15",
            }

    fixed = dt.date(2026, 6, 11)
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: fixed)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: fixed)

    msg = "agami 15 august leave chai"
    pipe = LeaveEntityPipeline(EntityExtractor(llm=_BadLeaveLLM()))
    entities = pipe.extract(
        msg,
        intent=INTENT_LEAVE_REQUEST,
        context_lines=[],
        trace_id="aug-chai",
        use_llm=True,
    ).entities
    assert not entities.get("reason")
    assert not entities.get("leave_type")
    assert entities.get("start_date") == "2026-08-15"

    pack = process_leave_turn(
        workflow_state={},
        message=msg,
        entities=entities,
        company_id="default",
        trace_id="aug-chai",
    )
    draft = dict(read_leave_state(pack["workflow_state"]).get("draft") or {})
    assert not draft.get("reason")
    assert draft.get("leave_type") is None
    question = str(pack.get("question") or "").lower()
    assert "annual leave" in question or "leave without pay" in question
    assert "15 august leave chai" not in question
    missing = get_missing_slots(draft)
    assert SLOT_LEAVE_TYPE in missing


def test_sick_leave_nite_chai_after_balance_interrupt_keeps_sick_type() -> None:
    """Explicit sick choice must survive semantic reconcile (not reset to annual/LWOP)."""
    from chat.services.leave.reason_bucket_classifier import apply_leave_semantic_reconcile
    from chat.services.leave_slots import SLOT_LEAVE_TYPE, SLOT_SCOPE
    from chat.services.leave_workflow import apply_leave_state, process_leave_turn
    from chat.services.leave_fsm import STATUS_ACTIVE, read_leave_state

    wf = apply_leave_state(
        {},
        draft={
            "start_date": "2026-06-12",
            "_last_user_message": "amar kalke leave lagbe",
        },
        step=SLOT_LEAVE_TYPE,
        status=STATUS_ACTIVE,
    )
    msg = "acch ami sick leave nite chai"
    pack = process_leave_turn(
        workflow_state=wf,
        message=msg,
        entities={},
        company_id="default",
        trace_id="sick-nite-chai",
    )
    draft = dict(read_leave_state(pack["workflow_state"]).get("draft") or {})
    assert draft.get("leave_type") == "sick"
    assert draft.get("_stated_leave_type") == "sick"
    assert draft.get("_leave_bucket") == "sick"
    missing = get_missing_slots(draft)
    assert SLOT_LEAVE_TYPE not in missing
    assert SLOT_SCOPE in missing
    question = str(pack.get("question") or "").lower()
    assert "annual leave" not in question or "full day" in question
    assert "full day" in question or "half day" in question

    draft_only = {
        "start_date": "2026-06-12",
        "leave_type": "sick",
        "_stated_leave_type": "sick",
        "_last_user_message": msg,
    }
    apply_leave_semantic_reconcile(draft_only, message=msg, use_llm=False)
    assert draft_only.get("leave_type") == "sick"


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
