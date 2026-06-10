"""Leave review: compound Banglish corrections (multi-day sick without start date)."""

from unittest.mock import patch

from chat.services.leave.reason_value import (
    extract_compound_review_reason,
    extract_reason_value,
    is_boilerplate_leave_reason,
    looks_like_health_leave_reason,
)
from chat.services.leave.entity_pipeline import LeaveEntityPipeline
from chat.services.leave.review_turn_parser import (
    is_review_compound_correction,
    try_apply_review_compound_update,
)
from chat.services.leave_fsm import read_leave_state
from chat.services.leave_slots import SLOT_DATES
from chat.services.leave_workflow import process_leave_turn

REVIEW_COMPOUND_MSG = (
    "ami 3 diner jonno ekta sick leave apply korte chacchi.."
    "ami onek osusto..and eta paid leave hobe.."
)


# Golden rows — leave reason extraction (owner: leave/reason_value.py, R01/R02)
LEAVE_LAGBE_OSUSTO_MSG = "amar ajke leave lagbe onek osusto tai leave lagbe"
OSUSTO_BEFORE_TAI_MSG = "ami onek osusto tai ajke leave lagbe"


def test_g_reason_leave_lagbe_osusto_not_full_application() -> None:
    """R01 compound + R02: ``leave lagbe … osusto tai leave`` → short health reason."""
    assert looks_like_health_leave_reason(LEAVE_LAGBE_OSUSTO_MSG)
    reason = extract_reason_value(LEAVE_LAGBE_OSUSTO_MSG)
    assert reason == "onek osusto"
    assert "leave lagbe" not in (reason or "").lower()
    assert extract_compound_review_reason(LEAVE_LAGBE_OSUSTO_MSG) == "onek osusto"


def test_g_reason_osusto_before_tai_still_works() -> None:
    reason = extract_reason_value(OSUSTO_BEFORE_TAI_MSG)
    assert reason == "onek osusto"


def test_g_reason_mixed_capture_is_boilerplate() -> None:
    assert is_boilerplate_leave_reason("amar ajke leave lagbe onek osusto")


def test_entity_pipeline_rules_only_leave_lagbe_osusto() -> None:
    """LLM-off path must still extract ``onek osusto`` (no orchestrator override)."""
    pipeline = LeaveEntityPipeline()
    result = pipeline.extract(
        LEAVE_LAGBE_OSUSTO_MSG,
        intent="LEAVE_REQUEST",
        context_lines=[],
        trace_id="t-reason-rules-only",
        use_llm=False,
    )
    assert result.entities.get("reason") == "onek osusto"


def test_extract_compound_review_reason_osusto() -> None:
    reason = extract_compound_review_reason(REVIEW_COMPOUND_MSG)
    assert reason
    assert "osusto" in reason.lower() or "অসুস্থ" in reason


def test_extract_reason_value_not_full_application_sentence() -> None:
    reason = extract_reason_value(REVIEW_COMPOUND_MSG, edit_context=True)
    assert reason
    assert "apply korte chacchi" not in reason.lower()
    assert len(reason) < len(REVIEW_COMPOUND_MSG)


def test_is_review_compound_correction() -> None:
    assert is_review_compound_correction(REVIEW_COMPOUND_MSG)


def test_try_apply_review_compound_clears_stale_date() -> None:
    draft = {
        "start_date": "2026-06-08",
        "end_date": "2026-06-08",
        "leave_payment_category": "paid",
        "day_scope": "full",
        "reason": "ekta leave apply korte chacchi",
    }
    changed = try_apply_review_compound_update(
        draft, REVIEW_COMPOUND_MSG, use_llm=False
    )
    assert changed
    assert draft.get("days") == 3
    assert draft.get("leave_type") == "sick"
    assert "start_date" not in draft
    assert "end_date" not in draft
    assert "osusto" in str(draft.get("reason") or "").lower() or "অসুস্থ" in str(
        draft.get("reason") or ""
    )


def test_pipeline_entities_primary_for_compound_review() -> None:
    draft = {
        "start_date": "2026-06-08",
        "end_date": "2026-06-08",
        "leave_payment_category": "paid",
        "day_scope": "full",
        "reason": "ekta leave apply korte chacchi",
    }
    entities = {
        "days": 3,
        "reason": "onek osusto",
        "leave_type": "sick",
        "leave_payment_category": "paid",
    }
    changed = try_apply_review_compound_update(
        draft,
        REVIEW_COMPOUND_MSG,
        entities=entities,
        use_llm=False,
    )
    assert changed
    assert draft.get("days") == 3
    assert draft.get("reason") == "onek osusto"
    assert "start_date" not in draft


def test_llm_primary_when_rules_miss_novel_phrasing() -> None:
    msg = "actually amar panch din er medical chuti lagbe, besh weak feel korchi, paid hobe"
    draft = {
        "start_date": "2026-06-08",
        "end_date": "2026-06-08",
        "leave_payment_category": "paid",
        "day_scope": "full",
        "reason": "old",
    }
    llm_out = {
        "days": 5,
        "reason": "weak feel korchi",
        "leave_type": "sick",
        "leave_payment_category": "paid",
        "day_scope": None,
        "start_date": None,
        "clear_dates": True,
        "confidence": 0.95,
    }
    with patch(
        "chat.services.leave.review_turn_parser.LLMClient"
    ) as mock_cls:
        mock_cls.return_value.is_configured.return_value = True
        mock_cls.return_value.chat_json.return_value = llm_out
        changed = try_apply_review_compound_update(
            draft, msg, entities={}, use_llm=True, trace_id="t-review-llm"
        )
    assert changed
    assert draft.get("days") == 5
    assert draft.get("reason") == "weak feel korchi"
    assert "start_date" not in draft


def test_process_leave_turn_review_compound_asks_start_date() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "review_pending": True,
        "draft": {
            "start_date": "2026-06-08",
            "end_date": "2026-06-08",
            "leave_payment_category": "paid",
            "day_scope": "full",
            "reason": "ekta leave apply korte chacchi",
        },
    }
    with patch(
        "chat.services.leave.review_turn_parser.LLMClient"
    ) as mock_cls:
        mock_cls.return_value.is_configured.return_value = False
        pack = process_leave_turn(
            workflow_state=wf,
            message=REVIEW_COMPOUND_MSG,
            entities={},
            company_id="company-a",
        )
    st = read_leave_state(pack["workflow_state"])
    draft = st.get("draft") or {}
    assert not st.get("review_pending")
    assert st.get("step") == SLOT_DATES
    assert draft.get("days") == 3
    assert "start_date" not in draft
    assert draft.get("leave_type") == "sick"
    assert pack.get("question")
    assert "apply korte chacchi" not in str(draft.get("reason") or "").lower()
