"""Sick leave + duration without user stating paid/full day."""

from unittest.mock import patch

from chat.services.leave.conversation_manager import LeaveConversationManager
from chat.services.leave_slots import SLOT_DATES, SLOT_PAYMENT, SLOT_SCOPE, get_missing_slots
from chat.services.leave_workflow import _apply_slots_from_message, process_leave_turn

SICK_3_DAY_MSG = (
    "tumi amar jonno ekta leave apply kore daw..."
    "ami onek osusto tai 3 diner jonno ekta leave nite chai"
)


def test_reason_extracts_osusto_not_amar_jonno() -> None:
    from chat.services.leave.reason_value import extract_reason_value

    reason = extract_reason_value(SICK_3_DAY_MSG)
    assert reason
    assert "amar jonno" not in reason.lower()
    assert "osusto" in reason.lower() or "অসুস্থ" in reason


def test_llm_boilerplate_reason_replaced_with_osusto() -> None:
    draft: dict = {}
    _apply_slots_from_message(
        draft,
        SICK_3_DAY_MSG,
        {
            "reason": "amar jonno",
            "leave_type": "sick",
            "days": 3,
        },
    )
    assert "amar jonno" not in str(draft.get("reason") or "").lower()
    assert "osusto" in str(draft.get("reason") or "").lower() or "অসুস্থ" in str(
        draft.get("reason") or ""
    )


def test_llm_invented_payment_not_applied_without_user_words() -> None:
    draft: dict = {}
    entities = {
        "reason": "onek osusto",
        "leave_type": "sick",
        "days": 3,
        "leave_payment_category": "paid",
        "day_scope": "full",
        "start_date": "2026-06-09",
        "end_date": "2026-06-09",
    }
    _apply_slots_from_message(draft, SICK_3_DAY_MSG, entities)
    assert draft.get("days") == 3
    assert "osusto" in str(draft.get("reason") or "").lower()
    assert "leave_payment_category" not in draft
    assert "day_scope" not in draft
    assert "start_date" not in draft
    assert "end_date" not in draft


def test_missing_slots_include_payment_scope_before_dates() -> None:
    draft: dict = {}
    _apply_slots_from_message(
        draft,
        SICK_3_DAY_MSG,
        {
            "reason": "onek osusto",
            "leave_type": "sick",
            "days": 3,
            "leave_payment_category": "paid",
            "day_scope": "full",
        },
    )
    missing = get_missing_slots(draft)
    assert SLOT_PAYMENT in missing
    assert SLOT_SCOPE in missing
    assert SLOT_DATES in missing
    assert missing[0] == SLOT_PAYMENT


def test_dates_prompt_for_multi_day_is_professional() -> None:
    mgr = LeaveConversationManager()
    q = mgr.build_follow_up(
        {"reason": "onek osusto", "days": 3},
        primary_slot=SLOT_DATES,
        missing=[SLOT_DATES],
    )
    assert "3 দিনের" in q
    assert "কোন তারিখ থেকে" in q
    assert "২০২৬-০৫-১৫" not in q
    assert "Paid leave" not in q
    assert "Full day" not in q


def test_process_leave_turn_sick_3_day_asks_payment_not_fake_ack() -> None:
    wf: dict = {}
    with patch(
        "chat.services.leave.entity_pipeline.EntityExtractor"
    ) as mock_ext_cls:
        mock_ext_cls.return_value._llm.is_configured.return_value = True
        mock_ext_cls.return_value.extract.return_value = {
            "entities": {
                "reason": "onek osusto",
                "leave_type": "sick",
                "days": 3,
                "leave_payment_category": "paid",
                "day_scope": "full",
            },
            "source": "llm",
        }
        pack = process_leave_turn(
            workflow_state=wf,
            message=SICK_3_DAY_MSG,
            entities={
                "reason": "onek osusto",
                "leave_type": "sick",
                "days": 3,
                "leave_payment_category": "paid",
                "day_scope": "full",
            },
            company_id="company-a",
        )
    q = pack.get("question") or ""
    assert "Paid leave — ঠিক আছে" not in q
    assert "Full day — ঠিক আছে" not in q
    assert "paid" in q.lower() or "Paid" in q
    assert "Full Day" in q or "Half Day" in q
    assert "ছুটির তারিখ" not in q


def test_agamikal_theke_applies_three_day_end_date() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": "leave_dates",
        "draft": {
            "reason": "onek osusto",
            "leave_type": "sick",
            "days": 3,
            "leave_payment_category": "paid",
            "day_scope": "full",
        },
    }
    pack = process_leave_turn(
        workflow_state=wf,
        message="agamikal theke",
        entities={},
        company_id="company-a",
    )
    draft = pack["workflow_state"].get("draft") or {}
    assert draft.get("start_date") == "2026-06-09"
    assert draft.get("end_date") == "2026-06-11"
    q = pack.get("question") or ""
    assert "2026-06-09" in q and "2026-06-11" in q


def test_agamkal_typo_theke_applies_three_day_end_date() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": "leave_dates",
        "draft": {
            "reason": "onek osusto",
            "leave_type": "sick",
            "days": 3,
            "leave_payment_category": "paid",
            "day_scope": "full",
        },
    }
    pack = process_leave_turn(
        workflow_state=wf,
        message="agamkal theke",
        entities={},
        company_id="company-a",
    )
    draft = pack["workflow_state"].get("draft") or {}
    assert draft.get("start_date") == "2026-06-09"
    assert draft.get("end_date") == "2026-06-11"


def test_paid_full_day_then_asks_three_day_dates_not_review() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": "leave_payment_category",
        "draft": {
            "reason": "onek osusto",
            "leave_type": "sick",
            "days": 3,
        },
    }
    pack = process_leave_turn(
        workflow_state=wf,
        message="paid and full day",
        entities={},
        company_id="company-a",
    )
    q = pack.get("question") or ""
    draft = (pack["workflow_state"].get("draft") or {})
    assert draft.get("leave_payment_category") == "paid"
    assert draft.get("day_scope") == "full"
    assert draft.get("days") == 3
    assert "start_date" not in draft
    assert "কোন তারিখ থেকে" in q or "কোন তারিখ" in q
    assert "জমা দেবেন" not in q
