"""P0/P1 tests for clarify affirmatives and semantic resolver."""

from unittest.mock import patch

from chat.services.expense.clarify import (
    apply_clarification_reply,
    collect_clarification_issues,
)
from chat.services.expense.clarify_affirmatives import (
    is_clarify_affirmative_only,
    is_invalid_clarify_location,
)
from chat.services.expense.clarify_llm_parser import (
    ClarifyLlmAnswer,
    ClarifyLlmReplyResult,
    apply_clarify_llm_result,
    llm_json_to_clarify_reply,
    reconcile_clarify_rules_and_llm,
)
from chat.services.expense_workflow import process_expense_turn

USER_MSG = (
    "amar ajke cost hoyeche mirpur to motejhil 100 taka tarpor lunch e 100 taka "
    "tarpor motejhil to mirpur 80 taka metro rail e expense hoyeche"
)


def test_hae_is_affirmative_not_location():
    assert is_clarify_affirmative_only("hae")
    assert is_invalid_clarify_location("hae")
    assert not is_invalid_clarify_location("motejheel")


def test_apply_hae_confirms_typo_not_location():
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
    with patch("chat.services.expense.clarify_llm_parser.LLMClient") as mock_llm:
        mock_llm.return_value.is_configured.return_value = False
        items_out, _, unresolved, disambig, _ = apply_clarification_reply(
            "hae", items, issues, pending, use_llm=True
        )
    assert not disambig
    assert items_out[0]["from_location"] == "motejheel"
    assert "hae" not in (items_out[0].get("from_location") or "").lower()
    assert len(unresolved) == 1
    assert unresolved[0].kind == "missing_category"


def test_llm_confirm_typo_action_uses_suggestion():
    items = [
        {
            "category": "Bus",
            "amount": 100,
            "from_location": "mirpur",
            "to_location": "motejhil",
        }
    ]
    pending: list[dict] = []
    issues = collect_clarification_issues(items, pending)
    result = ClarifyLlmReplyResult(
        answers=[
            ClarifyLlmAnswer(
                issue_index=1, action="confirm_typo", value="hae"
            )
        ],
        user_meant_affirmative_only=True,
    )
    out_items, _, unresolved, disambig = apply_clarify_llm_result(
        result, issues, [dict(x) for x in items], pending, {0}
    )
    assert not disambig
    assert out_items[0]["to_location"] == "motejheel"
    assert unresolved == []


def test_llm_json_parses_action_field():
    data = {
        "answers": [
            {
                "issue_index": 1,
                "action": "confirm_typo",
                "value": "motejheel",
            }
        ],
        "user_meant_affirmative_only": True,
    }
    parsed = llm_json_to_clarify_reply(data)
    assert parsed is not None
    assert parsed.answers[0].action == "confirm_typo"


@patch("chat.services.expense.clarify_llm_parser.parse_clarify_reply_llm")
def test_reconcile_llm_overrides_invalid_hae_location(mock_parse):
    items = [
        {
            "category": "Bus",
            "amount": 100,
            "from_location": "mirpur",
            "to_location": "hae",
        }
    ]
    pending: list[dict] = []
    issues = collect_clarification_issues(
        [{"category": "Bus", "amount": 100, "from_location": "mirpur", "to_location": "motejhil"}],
        pending,
    )
    mock_parse.return_value = ClarifyLlmReplyResult(
        answers=[
            ClarifyLlmAnswer(issue_index=1, action="confirm_typo", value="motejheel")
        ]
    )
    out_items, _, unresolved, disambig = reconcile_clarify_rules_and_llm(
        "hae",
        issues,
        items,
        pending,
        rules_unresolved=issues,
        rules_needs_disambig=False,
        llm_result=mock_parse.return_value,
    )
    assert not disambig
    assert out_items[0]["to_location"] == "motejheel"
    assert unresolved == []


def test_meta_typo_acknowledgment_not_stored_as_location():
    items = [
        {
            "category": "Bus",
            "amount": 100,
            "from_location": "mirpur",
            "to_location": "motejhil",
        }
    ]
    pending: list[dict] = []
    issues = collect_clarification_issues(items, pending)
    meta = "awesome..perfcetly detect korcho..ami banan vul diyechilam.."
    with patch("chat.services.expense.clarify_llm_parser.LLMClient") as mock_llm:
        mock_llm.return_value.is_configured.return_value = False
        items_out, _, unresolved, disambig, _ = apply_clarification_reply(
            meta, items, issues, pending, use_llm=True
        )
    assert not disambig
    assert items_out[0]["to_location"] == "motejheel"
    assert "detect" not in (items_out[0].get("to_location") or "").lower()
    assert unresolved == []


def test_workflow_hae_then_bus_then_hae():
    r1 = process_expense_turn(workflow_state={}, message=USER_MSG)
    assert r1["workflow_state"]["expense_request"].get("pending_step") == "clarify"

    with patch("chat.services.expense.clarify_llm_parser.LLMClient") as mock_llm:
        mock_llm.return_value.is_configured.return_value = False
        r2 = process_expense_turn(workflow_state=r1["workflow_state"], message="hae")
    assert "hae" not in str(r2.get("items") or [])
    assert "category" in (r2.get("question") or "").lower()

    r3 = process_expense_turn(workflow_state=r2["workflow_state"], message="bus")
    items = r3.get("items") or []
    bus = [x for x in items if x.get("category") == "Bus"]
    assert bus
    assert bus[0].get("category") == "Bus"

    with patch("chat.services.expense.clarify_llm_parser.LLMClient") as mock_llm:
        mock_llm.return_value.is_configured.return_value = False
        r4 = process_expense_turn(workflow_state=r3["workflow_state"], message="hae")
    items4 = r4.get("items") or []
    for row in items4:
        assert str(row.get("from_location") or "").lower() != "hae"
        assert str(row.get("to_location") or "").lower() != "hae"
