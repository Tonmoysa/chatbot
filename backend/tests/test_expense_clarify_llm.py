"""Tests for clarify reply LLM fallback (hybrid rules + LLM)."""

from unittest.mock import patch

from chat.services.expense.clarify import (
    apply_clarification_reply,
    collect_clarification_issues,
)
from chat.services.expense.clarify_llm_parser import (
    ClarifyLlmAnswer,
    ClarifyLlmReplyResult,
    apply_clarify_llm_result,
    clarify_llm_enabled,
    llm_json_to_clarify_reply,
)


def test_llm_json_to_clarify_reply_parses_answers():
    data = {
        "answers": [
            {"issue_index": 2, "value": "Metro Rail"},
            {"issue_index": 1, "value": "yes"},
        ],
        "needs_disambiguation": False,
    }
    result = llm_json_to_clarify_reply(data)
    assert result is not None
    assert len(result.answers) == 2
    assert result.answers[0].issue_index == 2


def test_clarify_llm_enabled_when_configured():
    with patch("chat.services.expense.clarify_llm_parser.LLMClient") as mock_llm:
        mock_llm.return_value.is_configured.return_value = True
        assert clarify_llm_enabled(use_llm=True)
    with patch("chat.services.expense.clarify_llm_parser.LLMClient") as mock_llm:
        mock_llm.return_value.is_configured.return_value = False
        assert not clarify_llm_enabled(use_llm=True)


def test_apply_clarify_llm_result_sets_category_only():
    items = [
        {
            "category": "Metro Rail",
            "amount": 80,
            "from_location": "motejhil",
            "to_location": "mirpur",
        }
    ]
    pending = [{"amount": 100, "category": "", "from_location": "mirpur", "to_location": "motejhil"}]
    issues = collect_clarification_issues(items, pending)
    llm = ClarifyLlmReplyResult(
        answers=[
            ClarifyLlmAnswer(
                issue_index=2, action="set_category", value="Metro Rail"
            )
        ],
    )
    out_items, out_pending, unresolved, disambig = apply_clarify_llm_result(
        llm, issues, [dict(x) for x in items], [dict(x) for x in pending], {0, 1}
    )
    assert out_pending[0]["category"] == "Metro Rail"
    assert len(unresolved) == 1
    assert unresolved[0].kind == "location_typo"
    assert not disambig


@patch("chat.services.expense.clarify_llm_parser.parse_clarify_reply_llm")
def test_apply_clarification_reply_llm_fallback(mock_parse_llm):
    mock_parse_llm.return_value = ClarifyLlmReplyResult(
        answers=[
            ClarifyLlmAnswer(
                issue_index=1, action="confirm_typo", value="motejheel"
            )
        ],
    )
    items = [
        {
            "category": "Metro Rail",
            "amount": 80,
            "from_location": "motejhil",
            "to_location": "mirpur",
        }
    ]
    pending = [{"amount": 100, "category": "", "from_location": "mirpur", "to_location": "motejhil"}]
    issues = collect_clarification_issues(items, pending)

    out_items, out_pending, unresolved, disambig, _ = apply_clarification_reply(
        "ekta ekta kori",
        items,
        issues,
        pending,
        trace_id="test-clarify-llm",
        use_llm=True,
        last_question="confirm typos",
    )
    mock_parse_llm.assert_called_once()
    assert not disambig
    assert out_items[0]["from_location"] == "motejheel"
    assert len(unresolved) == 1
    assert unresolved[0].kind == "missing_category"


@patch("chat.services.expense.clarify_llm_parser.clarify_llm_enabled", return_value=True)
@patch("chat.services.expense.clarify_llm_parser.parse_clarify_reply_llm")
def test_rules_skip_llm_when_fully_resolved(mock_parse_llm, _mock_enabled):
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
    _, pending_out, unresolved, _, _ = apply_clarification_reply(
        "mirpur, snack",
        items,
        issues,
        pending,
        trace_id="test",
        use_llm=True,
    )
    mock_parse_llm.assert_not_called()
    assert pending_out[0]["category"] == "Snack"
    assert unresolved == []
