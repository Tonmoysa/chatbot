"""Expense pre-review clarification (D) and inline review flags (E)."""

from chat.services.expense.clarify import (
    apply_clarification_reply,
    collect_clarification_issues,
    format_clarification_prompt,
)
from chat.services.expense_workflow import (
    format_expense_summary,
    process_expense_turn,
    _format_line_display,
)


def test_collect_clarification_issues_typo_and_category():
    items = [
        {
            "category": "Metro Rail",
            "amount": 30,
            "from_location": "uttora",
            "to_location": "irpur",
        }
    ]
    pending = [{"amount": 20, "category": ""}]
    issues = collect_clarification_issues(items, pending)
    kinds = {i.kind for i in issues}
    assert "location_typo" in kinds
    assert "missing_category" in kinds


def test_format_clarification_prompt_lists_both_issues():
    items = [{"category": "Metro Rail", "amount": 30, "to_location": "irpur", "from_location": "uttora"}]
    pending = [{"amount": 20, "category": ""}]
    issues = collect_clarification_issues(items, pending)
    prompt = format_clarification_prompt(issues)
    assert "mirpur" in prompt.lower()
    assert "20" in prompt
    assert "category" in prompt.lower()


def test_apply_clarification_reply_comma_separated():
    items = [
        {
            "category": "Metro Rail",
            "amount": 30,
            "from_location": "uttora",
            "to_location": "irpur",
        }
    ]
    pending = [{"amount": 20, "category": ""}]
    issues = collect_clarification_issues(items, pending)
    items, pending, unresolved = apply_clarification_reply(
        "mirpur, snack", items, issues, pending
    )
    assert items[0]["to_location"] == "mirpur"
    assert pending[0]["category"] == "Snack"
    assert unresolved == []


def test_inline_review_flags_on_summary():
    items = [
        {
            "category": "Metro Rail",
            "amount": 30,
            "from_location": "uttora",
            "to_location": "irpur",
        }
    ]
    summary = format_expense_summary(
        items,
        incurred_date_iso="2026-06-06",
        line_flags={0: ["⚠️ (mirpur?)"]},
    )
    assert "⚠️" in summary
    assert "irpur" in summary


def test_format_line_display_inline_flags():
    row = {"category": "Metro Rail", "amount": 30, "from_location": "uttora", "to_location": "irpur"}
    line = _format_line_display(row, inline_flags=["⚠️ (mirpur?)"])
    assert "⚠️" in line
    assert "irpur" in line


def test_workflow_starts_clarify_for_compound_message():
    msg = (
        "ajke amar expense hoyeche 100 taka bus mirpur to badda "
        "then lunch 100 taka then metro rail 30 taka uttora to irpur.."
        "then 20 taka"
    )
    r = process_expense_turn(workflow_state={}, message=msg)
    er = r["workflow_state"].get("expense_request") or {}
    assert er.get("pending_step") == "clarify"
    assert "mirpur" in (r.get("question") or "").lower()
    assert "20" in (r.get("question") or "")
    assert "Other" not in {row.get("category") for row in r.get("items") or []}
