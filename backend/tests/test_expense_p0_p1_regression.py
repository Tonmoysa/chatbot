"""P0/P1 regression tests for expense clarify, corrections, and ingest guards."""

from unittest.mock import patch

from chat.services.expense.clarify import (
    apply_clarification_reply,
    collect_clarification_issues,
    parse_clarification_partial_confirm,
)
from chat.services.expense.command_executor import execute_correction_plan
from chat.services.expense.command_parser import parse_correction_plan
from chat.services.expense.entity_merge import explicit_category_mentions, fill_parser_gaps_with_llm
from chat.services.expense.expense_confirm import (
    looks_like_expense_correction,
    parse_category_slot_answer,
)
from chat.services.expense.reconcile import (
    apply_category_hobe_correction,
    drop_conflicting_travel_lines,
)
from chat.services.expense_extraction import ExpenseLineItem, ExtractionResult, extract_expense_items
from chat.services.expense_workflow import process_expense_turn


USER_MSG = (
    "amar ajke cost hoyeche mirpur to motejhil 100 taka tarpor lunch e 100 taka "
    "tarpor motejhil to mirpur 80 taka metro rail e expense hoyeche"
)


def test_user_message_regex_no_invented_bus():
    ext = extract_expense_items(USER_MSG)
    cats = {it.category for it in ext.items if it.category}
    assert "Bus" not in cats
    assert "Lunch" in cats
    assert "Metro Rail" in cats
    pending_route = [
        it
        for it in ext.items
        if not it.category and it.from_location and it.to_location
    ]
    assert len(pending_route) == 1
    assert pending_route[0].amount == 100.0


def test_partial_clarify_only_confirms_one_issue():
    items = [
        {
            "category": "Metro Rail",
            "amount": 80,
            "from_location": "motijheel",
            "to_location": "irpur",
        }
    ]
    pending = [
        {
            "amount": 100,
            "category": "",
            "source_clause": "metroral e expense hoyeche 100 taka",
        }
    ]
    issues = collect_clarification_issues(items, pending)
    assert len(issues) >= 2
    idxs = parse_clarification_partial_confirm("2 option thik ache", len(issues))
    assert idxs is not None
    assert 1 in idxs
    _, pending_out, unresolved, _, _ = apply_clarification_reply(
        "2 option thik ache", items, issues, pending
    )
    assert pending_out[0]["category"] == "Metro Rail"
    assert any(i.kind == "location_typo" for i in unresolved)


def test_metro_rail_hobe_parsed():
    assert parse_category_slot_answer("metro rail hobe") == "Metro Rail"
    assert looks_like_expense_correction("metro rail hobe")


def test_category_assign_during_multi_issue_clarify_skips_typo():
    """``metrorail motejhil to mirpur hobe`` must not resolve typo or duplicate metro."""
    items = [
        {"category": "Lunch", "amount": 100},
        {
            "category": "Metro Rail",
            "amount": 80,
            "from_location": "motejhil",
            "to_location": "mirpur",
        },
    ]
    pending = [
        {
            "amount": 100,
            "category": "",
            "from_location": "mirpur",
            "to_location": "motejhil",
            "source_clause": "mirpur to motejhil 100 taka",
        }
    ]
    issues = collect_clarification_issues(items, pending)
    assert len(issues) == 2
    with patch("chat.services.expense.clarify_llm_parser.LLMClient") as mock_llm:
        mock_llm.return_value.is_configured.return_value = False
        items_out, pending_out, unresolved, disambig, _ = apply_clarification_reply(
            "metrorail motejhil to mirpur hobe", items, issues, pending
        )
    assert not disambig
    assert pending_out[0]["category"] == "Metro Rail"
    assert items_out[1]["from_location"] == "motejhil"
    assert len(unresolved) == 1
    assert unresolved[0].kind == "location_typo"
    metro_lines = [r for r in items_out if r.get("category") == "Metro Rail"]
    assert len(metro_lines) == 1


def test_numbered_clarify_answer_targets_single_issue():
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
    items_out, pending_out, unresolved, _, _ = apply_clarification_reply(
        "2 metro rail", items, issues, pending
    )
    assert pending_out[0]["category"] == "Metro Rail"
    assert len(unresolved) == 1
    assert unresolved[0].kind == "location_typo"
    assert items_out[0]["from_location"] == "motejhil"


def test_apply_clarify_ha_confirms_typo_only():
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
    items_out, pending_out, unresolved, disambig, _ = apply_clarification_reply(
        "ha", items, issues, pending
    )
    assert not disambig
    assert items_out[0]["from_location"] == "motejheel"
    assert len(unresolved) == 1
    assert unresolved[0].kind == "missing_category"
    assert not pending_out[0].get("category")


def test_looks_like_clarify_reply_signal_ha():
    from chat.services.expense.clarify import looks_like_clarify_reply_signal

    assert looks_like_clarify_reply_signal("ha")
    assert looks_like_clarify_reply_signal("yes")
    assert not looks_like_clarify_reply_signal("kemon acho")


def test_workflow_clarify_ha_reply_advances_typo():
    r1 = process_expense_turn(workflow_state={}, message=USER_MSG)
    assert r1["workflow_state"]["expense_request"].get("pending_step") == "clarify"
    r2 = process_expense_turn(workflow_state=r1["workflow_state"], message="ha")
    q = (r2.get("question") or "").lower()
    assert "expense form in progress" not in q
    assert "category" in q
    items = r2.get("items") or []
    metro = [x for x in items if x.get("category") == "Metro Rail"]
    assert len(metro) == 1
    assert metro[0]["from_location"] == "motejheel"


def test_workflow_clarify_category_hobe_does_not_duplicate_metro():
    r1 = process_expense_turn(workflow_state={}, message=USER_MSG)
    er = r1["workflow_state"].get("expense_request") or {}
    assert er.get("pending_step") == "clarify"
    r2 = process_expense_turn(
        workflow_state=r1["workflow_state"],
        message="metrorail motejhil to mirpur hobe",
    )
    q = (r2.get("question") or "").lower()
    items = r2.get("items") or []
    metro = [x for x in items if x.get("category") == "Metro Rail"]
    assert len(metro) == 2
    assert {float(x["amount"]) for x in metro} == {80.0, 100.0}
    assert metro[0]["from_location"] == "motejhil"
    assert "motejheel" in q or "motejhil" in q
    assert q.count("metro rail") <= 1 or q.count("80") <= 1
    assert "100 tk" not in q or "category" not in q
    assert r2["workflow_state"]["expense_request"].get("pending_step") == "clarify"


def test_drop_conflicting_bus_when_assigning_metro():
    items = [
        {
            "category": "Bus",
            "amount": 100,
            "from_location": "mirpur",
            "to_location": "motijheel",
        },
        {
            "category": "Metro Rail",
            "amount": 80,
            "from_location": "motijheel",
            "to_location": "mirpur",
        },
    ]
    pending = {
        "amount": 100,
        "from_location": "mirpur",
        "to_location": "motijheel",
    }
    out = drop_conflicting_travel_lines(items, pending, "Metro Rail")
    assert len(out) == 1
    assert out[0]["category"] == "Metro Rail"


def test_apply_category_hobe_replaces_wrong_bus_line():
    items = [
        {
            "category": "Bus",
            "amount": 100,
            "from_location": "mirpur",
            "to_location": "motijheel",
        },
        {
            "category": "Metro Rail",
            "amount": 80,
            "from_location": "motijheel",
            "to_location": "mirpur",
        },
        {
            "category": "Lunch",
            "amount": 100,
        },
    ]
    out, changed = apply_category_hobe_correction(items, "Metro Rail")
    assert changed
    metro_100 = [
        r
        for r in out
        if r.get("category") == "Metro Rail" and float(r.get("amount") or 0) == 100
    ]
    assert len(metro_100) == 1
    assert "Bus" not in {r.get("category") for r in out}


def test_remove_metro_rail_disambiguation_when_two_lines():
    items = [
        {
            "category": "Metro Rail",
            "amount": 80,
            "from_location": "motijheel",
            "to_location": "mirpur",
        },
        {
            "category": "Metro Rail",
            "amount": 100,
            "from_location": "mirpur",
            "to_location": "motijheel",
        },
    ]
    plan = parse_correction_plan("remove metro rail")
    result = execute_correction_plan(items, plan)
    assert not result.changed
    plan_amt = parse_correction_plan("remove metro rail 80")
    result2 = execute_correction_plan(items, plan_amt)
    assert result2.changed
    assert len(result2.items) == 1
    assert result2.items[0]["amount"] == 100


def test_llm_gap_fill_skips_invented_bus_without_explicit_mention():
    parser = ExtractionResult(
        items=[
            ExpenseLineItem(
                category="",
                amount=100,
                from_location="mirpur",
                to_location="motijheel",
            ),
            ExpenseLineItem(category="Lunch", amount=100),
            ExpenseLineItem(
                category="Metro Rail",
                amount=80,
                from_location="motijheel",
                to_location="mirpur",
            ),
        ],
        malformed=[],
    )
    llm_entities = {
        "expense_lines": [
            {
                "category": "Bus",
                "amount": 100,
                "from_location": "mirpur",
                "to_location": "motijheel",
            }
        ]
    }
    merged, _ = fill_parser_gaps_with_llm(
        parser, llm_entities, USER_MSG, llm_used=True
    )
    cats = {it.category for it in merged.items if it.category}
    assert "Bus" not in cats


def test_explicit_category_mentions_metro_not_bus():
    explicit = explicit_category_mentions(USER_MSG)
    assert "Metro Rail" in explicit
    assert "Bus" not in explicit


def test_workflow_starts_clarify_not_bus_for_user_message():
    r = process_expense_turn(workflow_state={}, message=USER_MSG)
    items = r.get("items") or []
    cats = {row.get("category") for row in items if row.get("category")}
    assert "Bus" not in cats
    er = r["workflow_state"].get("expense_request") or {}
    assert er.get("pending_step") in ("clarify", "category", "from_to") or r.get("question")
