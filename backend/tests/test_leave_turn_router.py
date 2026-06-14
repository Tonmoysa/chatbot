"""Leave turn router — inline field edits at review (reason, scope, payment)."""

from unittest.mock import patch

from chat.services.leave.reason_value import extract_reason_value
from chat.services.leave.turn_parser import (
    detect_edit_target_slot,
    extract_inline_field_value,
    resolve_leave_turn,
)
from chat.services.leave.turn_schema import TURN_EDIT_FIELD
from chat.services.leave_confirm import process_confirmation_turn
from chat.services.leave_fsm import read_leave_state
from chat.services.leave_workflow import process_leave_turn

USER_REASON_EDIT = (
    "accha reason ta tumi change koro ...reaosn ta hobe amar ashole pet betha"
)


def _review_draft():
    return {
        "leave_type": "casual",
        "leave_payment_category": "paid",
        "day_scope": "full",
        "start_date": "2026-06-09",
        "end_date": "2026-06-09",
        "reason": "familly program e jabo tai",
    }


def _review_wf(draft=None):
    return {
        "active_flow": "leave",
        "status": "active",
        "review_pending": True,
        "draft": dict(draft or _review_draft()),
    }


def test_extract_reason_value_edit_wrapper_pet_betha():
    val = extract_reason_value(USER_REASON_EDIT, edit_context=True)
    assert val
    assert "pet betha" in val.lower()


def test_detect_edit_target_slot_reason():
    assert detect_edit_target_slot(USER_REASON_EDIT) == "reason"


def test_extract_inline_reason_from_combined_message():
    val = extract_inline_field_value("reason", USER_REASON_EDIT, edit_context=True)
    assert val
    assert "pet" in val.lower()


def test_review_inline_reason_edit_returns_summary_not_reask():
    out = process_confirmation_turn(
        workflow_state=_review_wf(),
        message=USER_REASON_EDIT,
        draft=_review_draft(),
    )
    d = read_leave_state(out["workflow_state"]).get("draft") or {}
    assert "pet betha" in str(d.get("reason") or "").lower()
    q = out.get("question") or ""
    assert "জমা দেবেন" in q or "জমা দিন" in q
    assert "Reason টা এক লাইনে" not in q


def test_review_reason_edit_keeps_other_fields():
    out = process_confirmation_turn(
        workflow_state=_review_wf(),
        message=USER_REASON_EDIT,
        draft=_review_draft(),
    )
    d = read_leave_state(out["workflow_state"]).get("draft") or {}
    assert d.get("start_date") == "2026-06-09"
    assert d.get("leave_payment_category") == "paid"
    assert d.get("day_scope") == "full"


def test_review_half_day_inline_still_works():
    draft = _review_draft()
    out = process_confirmation_turn(
        workflow_state=_review_wf(draft),
        message="half day hobe",
        draft=draft,
    )
    d = read_leave_state(out["workflow_state"]).get("draft") or {}
    assert d.get("day_scope") == "half"


def test_edit_menu_reason_inline_same_message():
    draft = _review_draft()
    mid = process_confirmation_turn(
        workflow_state=_review_wf(draft),
        message="edit",
        draft=draft,
    )
    out = process_confirmation_turn(
        workflow_state=mid["workflow_state"],
        message=USER_REASON_EDIT,
        draft=draft,
    )
    d = read_leave_state(out["workflow_state"]).get("draft") or {}
    assert "pet betha" in str(d.get("reason") or "").lower()
    assert "জমা দেবেন" in (out.get("question") or "") or "জমা দিন" in (out.get("question") or "")


def test_llm_fallback_reason_edit():
    draft = _review_draft()
    msg = "actually the real cause is severe migraine headache"
    llm_payload = {
        "slot": "reason",
        "value": "severe migraine headache",
        "confidence": 0.9,
    }
    with patch(
        "chat.services.leave.reason_value.extract_reason_value", return_value=None
    ), patch(
        "chat.services.leave.turn_parser.LLMClient.is_configured", return_value=True
    ), patch(
        "chat.services.leave.turn_parser.LLMClient.chat_json", return_value=llm_payload
    ):
        decision = resolve_leave_turn(
            "change reason to severe migraine",
            draft=draft,
            review_pending=True,
            use_llm=True,
        )
    assert decision.turn_type == TURN_EDIT_FIELD
    assert "migraine" in str(decision.field_update.value).lower()


def test_conversation_replay_family_to_pet_betha():
    wf: dict = {}
    pack = process_leave_turn(
        workflow_state=wf,
        message="amar kalke leave lagbe family program e jabo tai fully paid leave lagbe",
        entities={"start_date": "2026-06-09", "leave_payment_category": "paid"},
        company_id="company-a",
    )
    pack = process_leave_turn(
        workflow_state=pack["workflow_state"],
        message="full day",
        entities={},
        company_id="company-a",
    )
    out = process_confirmation_turn(
        workflow_state=pack["workflow_state"],
        message=USER_REASON_EDIT,
        draft=read_leave_state(pack["workflow_state"]).get("draft") or {},
    )
    reason = str(
        (read_leave_state(out["workflow_state"]).get("draft") or {}).get("reason") or ""
    ).lower()
    assert "pet betha" in reason
