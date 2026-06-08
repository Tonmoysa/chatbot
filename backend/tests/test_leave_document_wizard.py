"""Supporting document wizard — skip / refusal phrases + LLM fallback."""

from unittest.mock import patch

from chat.services.leave.document_turn_parser import apply_document_answer
from chat.services.leave_confirm import build_leave_review_summary
from chat.services.leave_draft_utils import (
    has_real_supporting_document,
    is_supporting_document_skip_message,
    supporting_document_needed,
)
from chat.services.leave_fsm import read_leave_state
from chat.services.leave_slots import SLOT_DOCUMENT, apply_wizard_answer, get_missing_slots
from chat.services.leave_workflow import process_leave_turn

import pytest


@pytest.mark.parametrize(
    "message",
    [
        "skip",
        "nah parbo nah",
        "parbo na",
        "parchi na",
        "dite parbo na",
    ],
)
def test_document_skip_phrases(message: str) -> None:
    assert is_supporting_document_skip_message(message)


def test_famil_program_hobe_reason_is_non_sick() -> None:
    from chat.services.leave_draft_utils import (
        canonicalize_leave_reason,
        reason_indicates_non_sick_leave,
        supporting_document_needed,
    )

    assert canonicalize_leave_reason("famil program hobe reason") == "family program"
    assert reason_indicates_non_sick_leave("famil program hobe reason")
    draft = {
        "reason": "famil program hobe reason",
        "leave_type": "sick",
        "start_date": "2026-06-09",
        "end_date": "2026-06-11",
        "days": 3,
        "leave_payment_category": "paid",
        "day_scope": "full",
    }
    assert not supporting_document_needed(draft)


def test_family_program_three_days_skips_document_slot() -> None:
    draft = {
        "start_date": "2026-06-09",
        "end_date": "2026-06-11",
        "leave_payment_category": "paid",
        "day_scope": "full",
        "reason": "familly program",
        "leave_type": "sick",
        "days": 3,
    }
    assert not supporting_document_needed(draft)
    assert SLOT_DOCUMENT not in get_missing_slots(draft)


def test_sick_start_family_reason_correction_full_flow_skips_doctor() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": "leave_payment_category",
        "draft": {
            "reason": "অসুস্থতা / sick leave",
            "_reason_implied": True,
            "leave_type": "sick",
            "days": 3,
        },
    }
    pack = process_leave_turn(
        workflow_state=wf,
        message="famil program hobe reason",
        entities={},
        company_id="company-a",
    )
    wf = pack["workflow_state"]
    draft = wf.get("draft") or {}
    assert draft.get("reason") == "family program"
    assert draft.get("leave_type") == "casual"

    pack = process_leave_turn(
        workflow_state=wf,
        message="paid and full day",
        entities={},
        company_id="company-a",
    )
    wf = pack["workflow_state"]

    pack = process_leave_turn(
        workflow_state=wf,
        message="agamikal",
        entities={},
        company_id="company-a",
    )
    q = pack.get("question") or ""
    assert "ডাক্তার" not in q
    draft = (pack["workflow_state"].get("draft") or {})
    assert draft.get("start_date") == "2026-06-09"
    assert draft.get("end_date") == "2026-06-11"


def test_family_program_after_dates_skips_doctor_prompt() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": "leave_dates",
        "draft": {
            "reason": "familly program",
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
    q = pack.get("question") or ""
    assert "ডাক্তার" not in q


def test_sick_three_days_still_needs_document_slot() -> None:
    draft = {
        "start_date": "2026-06-09",
        "end_date": "2026-06-11",
        "leave_payment_category": "paid",
        "day_scope": "full",
        "reason": "onek osusto",
        "leave_type": "sick",
        "days": 3,
    }
    assert supporting_document_needed(draft)
    assert SLOT_DOCUMENT in get_missing_slots(draft)


def test_apply_wizard_document_refusal_waives_not_stores_text() -> None:
    draft: dict = {"reason": "onek osusto", "start_date": "2026-06-09"}
    apply_wizard_answer(
        draft, pending_slot=SLOT_DOCUMENT, message="nah parbo nah"
    )
    assert draft.get("supporting_document_waived") is True
    assert "document_text" not in draft
    assert not has_real_supporting_document(draft)


def test_review_summary_shows_waived_not_attached() -> None:
    draft = {
        "leave_payment_category": "paid",
        "day_scope": "full",
        "start_date": "2026-06-09",
        "end_date": "2026-06-11",
        "reason": "onek osusto",
        "supporting_document_waived": True,
    }
    summary = build_leave_review_summary(draft)
    assert "সংযুক্তি: আছে" not in summary
    assert "এখন নেই" in summary


def test_llm_document_waive_novel_banglish() -> None:
    draft: dict = {"reason": "onek osusto", "start_date": "2026-06-09"}
    llm_out = {
        "intent": "waive",
        "document_text": None,
        "confidence": 0.95,
    }
    with patch("chat.services.leave.document_turn_parser.LLMClient") as mock_cls:
        mock_cls.return_value.is_configured.return_value = True
        mock_cls.return_value.chat_json.return_value = llm_out
        applied = apply_document_answer(
            draft,
            "amar kache akhon kono chit nei manager dekhun",
            trace_id="t-doc-llm",
            use_llm=True,
        )
    assert applied
    assert draft.get("supporting_document_waived") is True
    assert "document_text" not in draft


def test_document_step_reason_correction_to_family_skips_attachment_in_review() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": SLOT_DOCUMENT,
        "draft": {
            "reason": "onek osusto",
            "leave_type": "sick",
            "days": 3,
            "start_date": "2026-06-09",
            "end_date": "2026-06-11",
            "leave_payment_category": "paid",
            "day_scope": "full",
        },
    }
    llm_doc = {
        "intent": "provide",
        "document_text": "reason ta family program hobe",
        "confidence": 0.95,
    }
    with patch("chat.services.leave.document_turn_parser.LLMClient") as mock_cls:
        mock_cls.return_value.is_configured.return_value = True
        mock_cls.return_value.chat_json.return_value = llm_doc
        pack = process_leave_turn(
            workflow_state=wf,
            message="reason ta family program hobe",
            entities={},
            company_id="company-a",
            trace_id="t-doc-reason-fix",
        )
    draft = read_leave_state(pack["workflow_state"]).get("draft") or {}
    assert draft.get("reason") == "family program"
    assert draft.get("leave_type") == "casual"
    assert "document_text" not in draft
    summary = build_leave_review_summary(draft)
    assert "সংযুক্তি" not in summary
    q = pack.get("question") or ""
    assert "yes" in q.lower() or "জমা" in q


def test_process_leave_turn_document_refusal_goes_to_review_without_fake_attachment() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": SLOT_DOCUMENT,
        "draft": {
            "reason": "onek osusto",
            "leave_type": "sick",
            "days": 3,
            "start_date": "2026-06-09",
            "end_date": "2026-06-11",
            "leave_payment_category": "paid",
            "day_scope": "full",
        },
    }
    pack = process_leave_turn(
        workflow_state=wf,
        message="nah parbo nah",
        entities={},
        company_id="company-a",
    )
    draft = read_leave_state(pack["workflow_state"]).get("draft") or {}
    assert draft.get("supporting_document_waived") is True
    assert "document_text" not in draft
    q = pack.get("question") or ""
    assert "সংযুক্তি: আছে" not in q
