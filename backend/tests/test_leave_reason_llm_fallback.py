"""Rules-first reason correction + sick/non-sick bucket with LLM fallback."""

from unittest.mock import MagicMock, patch

from chat.services.leave.reason_bucket_classifier import apply_leave_semantic_reconcile, classify_leave_bucket
from chat.services.leave.reason_correction_parser import try_apply_reason_correction
from chat.services.leave_slots import SLOT_DOCUMENT, SLOT_LEAVE_TYPE, get_missing_slots
from chat.services.leave_workflow import process_leave_turn


def test_rules_reason_ta_tour_not_osusto() -> None:
    draft: dict = {
        "reason": "onek osusto",
        "leave_type": "sick",
        "days": 3,
    }
    applied = try_apply_reason_correction(
        draft,
        "reaon ta tour hobe osusto nah",
        trace_id="",
        use_llm=False,
    )
    assert applied
    assert draft.get("reason") == "travel"
    apply_leave_semantic_reconcile(draft, message="reaon ta tour hobe osusto nah")
    assert draft.get("leave_type") is None
    assert SLOT_LEAVE_TYPE in get_missing_slots(draft)


def test_llm_reason_correction_when_rules_miss_negation_wrapper() -> None:
    draft: dict = {
        "reason": "onek osusto",
        "leave_type": "sick",
        "days": 3,
    }
    llm_out = {"reason": "travel", "confidence": 0.94}
    with patch("chat.services.leave.reason_correction_parser.LLMClient") as mock_cls:
        mock_cls.return_value.is_configured.return_value = True
        mock_cls.return_value.chat_json.return_value = llm_out
        applied = try_apply_reason_correction(
            draft,
            "reason ta ghurte jabo hobe osusto na",
            trace_id="t-tour-llm",
            use_llm=True,
        )
    assert applied
    assert draft.get("reason") == "travel"


def test_llm_reason_correction_typo_family_program() -> None:
    draft: dict = {
        "reason": "অসুস্থতা / sick leave",
        "_reason_implied": True,
        "leave_type": "sick",
        "days": 3,
    }
    llm_out = {"reason": "family program", "confidence": 0.93}
    with patch("chat.services.leave.reason_correction_parser.LLMClient") as mock_cls:
        mock_cls.return_value.is_configured.return_value = True
        mock_cls.return_value.chat_json.return_value = llm_out
        applied = try_apply_reason_correction(
            draft,
            "fmly progrm hobe reason",
            trace_id="t-reason-llm",
            use_llm=True,
        )
    assert applied
    assert draft.get("reason") == "family program"
    assert draft.get("_reason_implied") is None


def test_llm_bucket_typo_family_skips_doctor_document() -> None:
    draft = {
        "reason": "fmly progrm",
        "leave_type": "sick",
        "days": 3,
        "start_date": "2026-06-09",
        "end_date": "2026-06-11",
        "leave_payment_category": "paid",
        "day_scope": "full",
    }
    llm_out = {
        "bucket": "other",
        "normalized_reason": "family program",
        "confidence": 0.91,
        "clarify_question_bn": None,
    }
    with patch("chat.services.leave.reason_bucket_classifier.LLMClient") as mock_cls:
        mock_cls.return_value.is_configured.return_value = True
        mock_cls.return_value.chat_json.return_value = llm_out
        result = classify_leave_bucket(
            draft,
            message="fmly progrm hobe reason",
            trace_id="t-bucket-llm",
            use_llm=True,
        )
    assert result.bucket == "other"
    assert draft.get("leave_type") is None
    assert SLOT_LEAVE_TYPE in get_missing_slots(draft)
    assert SLOT_DOCUMENT not in get_missing_slots(draft)


def test_full_flow_tour_not_osusto_correction() -> None:
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
        message="reaon ta tour hobe osusto nah",
        entities={},
        company_id="company-a",
        trace_id="t-tour-rules",
    )
    draft = (pack["workflow_state"].get("draft") or {})
    assert draft.get("reason") == "travel"
    assert draft.get("leave_type") is None
    assert SLOT_LEAVE_TYPE in get_missing_slots(draft)
    q = pack.get("question") or ""
    assert "travel" in q or "tour" in q.lower() or "কারণ" in q
    assert "reaon ta tour" not in q


def test_full_flow_typo_reason_llm_skips_doctor() -> None:
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
    reason_llm = {"reason": "family program", "confidence": 0.93}
    bucket_llm = {
        "bucket": "other",
        "normalized_reason": "family program",
        "confidence": 0.91,
        "clarify_question_bn": None,
    }
    client = MagicMock()
    client.is_configured.return_value = True
    client.chat_json.side_effect = [reason_llm, bucket_llm, bucket_llm]

    with patch("chat.services.leave.reason_correction_parser.LLMClient", return_value=client), patch(
        "chat.services.leave.reason_bucket_classifier.LLMClient", return_value=client
    ):
        pack = process_leave_turn(
            workflow_state=wf,
            message="fmly progrm hobe reason",
            entities={},
            company_id="company-a",
            trace_id="t-flow-llm",
        )
    wf = pack["workflow_state"]
    draft = wf.get("draft") or {}
    assert draft.get("reason") == "family program"
    assert draft.get("leave_type") is None
    assert SLOT_LEAVE_TYPE in get_missing_slots(draft)

    pack = process_leave_turn(
        workflow_state=wf,
        message="paid and full day",
        entities={},
        company_id="company-a",
        trace_id="t-flow-llm",
    )
    wf = pack["workflow_state"]

    pack = process_leave_turn(
        workflow_state=wf,
        message="agamikal",
        entities={},
        company_id="company-a",
        trace_id="t-flow-llm",
    )
    q = pack.get("question") or ""
    assert "ডাক্তার" not in q
