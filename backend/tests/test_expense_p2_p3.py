"""P2/P3 regression: LLM context, leave copy, interrupt sync."""

from chat.services.expense.llm_context import build_wizard_llm_context
from chat.services.leave_copy import build_confirmation_prompt_body, lang_from_draft
from chat.services.leave_confirm import build_confirmation_prompt
from chat.services.turn_classifier import TURN_NEW_WORKFLOW, classify_workflow_turn
from chat.services.wizard_interrupt_classifier import classify_active_wizard_interrupt


def test_llm_context_includes_last_question_and_pending():
    ctx = build_wizard_llm_context(
        [{"category": "Lunch", "amount": 100}],
        stage="collecting",
        pending_step="category",
        pending_line={"amount": 50, "category": "", "from_location": "mirpur", "to_location": "badda"},
        last_question="100 Tk — category ki?",
    )
    assert "Pending step: category" in ctx
    assert "mirpur" in ctx
    assert "Last assistant question" in ctx
    assert "category ki" in ctx


def test_leave_confirmation_english():
    draft = {
        "reply_language": "en",
        "start_date": "2026-06-05",
        "end_date": "2026-06-05",
        "day_scope": "full",
        "leave_payment_category": "paid",
        "reason": "family program",
    }
    prompt = build_confirmation_prompt(draft)
    assert "Submit this leave request" in prompt
    assert "family program" in prompt


def test_leave_copy_banglish_footer():
    draft = {"reply_language": "banglish", "start_date": "2026-06-05", "day_scope": "full", "reason": "test"}
    body = build_confirmation_prompt_body(
        draft, lang=lang_from_draft(draft), select_leave_label="Paid Leave"
    )
    assert "submit korben" in body.lower()


def test_interrupt_sync_turn_classifier_matches_classifier():
    msg = "amar ajke lunch 100 taka bus 20 taka"
    wf = {
        "active_flow": "leave",
        "status": "active",
        "review_pending": True,
        "draft": {
            "start_date": "2026-06-04",
            "end_date": "2026-06-04",
            "leave_payment_category": "paid",
            "day_scope": "full",
            "reason": "family",
        },
    }
    intr = classify_active_wizard_interrupt(
        msg,
        workflow_state=wf,
        leave_active=True,
        leave_review_pending=True,
        use_llm=False,
    )
    turn = classify_workflow_turn(
        msg,
        leave_active=True,
        expense_active=False,
        leave_review_pending=True,
    )
    if intr.maps_to_turn == TURN_NEW_WORKFLOW:
        assert turn == TURN_NEW_WORKFLOW
