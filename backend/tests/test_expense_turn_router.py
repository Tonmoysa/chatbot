"""Unified expense turn router — lunch→snack and conversation replay."""

from unittest.mock import patch

from chat.services.expense.command_executor import apply_message_corrections
from chat.services.expense.command_parser import parse_correction_plan
from chat.services.expense.expense_confirm import looks_like_expense_correction
from chat.services.expense.turn_parser import (
    looks_like_draft_edit_signal,
    parse_turn_rules,
    resolve_expense_turn,
)
from chat.services.expense.turn_schema import TURN_EDIT_DRAFT
from chat.services.expense_workflow import process_expense_turn


def _draft_items():
    return [
        {"category": "Lunch", "amount": 100},
        {
            "category": "Bus",
            "amount": 200,
            "from_location": "mirpur",
            "to_location": "motejhil",
        },
        {
            "category": "Metro Rail",
            "amount": 400,
            "from_location": "uttora",
            "to_location": "mirpur",
        },
    ]


def test_regex_detects_jaigai_replace():
    msg = "lunch er jaigai snack hobe"
    assert looks_like_expense_correction(msg)
    plan = parse_correction_plan(msg)
    assert ("Lunch", "Snack") in plan.replacements


def test_regex_detects_take_kore_daw_replace():
    msg = "lunch take snack kore daw"
    assert looks_like_expense_correction(msg)
    plan = parse_correction_plan(msg)
    assert ("Lunch", "Snack") in plan.replacements


def test_regex_detects_lunch_ke_snack_kore_daw():
    msg = "lunch ke snack kore daw"
    assert looks_like_expense_correction(msg)
    plan = parse_correction_plan(msg)
    assert ("Lunch", "Snack") in plan.replacements


def test_regex_detects_ta_hobe_replace():
    msg = "bus ta bike hobe"
    assert looks_like_expense_correction(msg)
    plan = parse_correction_plan(msg)
    assert ("Bus", "Bike") in plan.replacements


def test_regex_detects_ke_koro_replace():
    msg = "bus ke bike koro.."
    assert looks_like_expense_correction(msg)
    plan = parse_correction_plan(msg)
    assert ("Bus", "Bike") in plan.replacements


def test_regex_detects_setake_replace():
    msg = "ami chaici expense je bus ta ache setake bike koro"
    assert looks_like_expense_correction(msg)
    plan = parse_correction_plan(msg)
    assert ("Bus", "Bike") in plan.replacements


def test_apply_correction_ke_koro_during_from_to_pending():
    wf = {}
    pack = process_expense_turn(
        workflow_state=wf,
        message="lunch 100, bus 200, rail 400",
    )
    block = pack["workflow_state"]["expense_request"]
    block["incurred_date_iso"] = "2026-06-08"

    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="bus ke bike koro",
    )
    block = pack["workflow_state"]["expense_request"]
    pending = block.get("pending_line") or {}
    out_items = pack["items"]
    assert pending.get("category") == "Bike"
    assert pending.get("amount") == 200.0
    assert all(r.get("category") != "Bus" for r in out_items)
    q = pack.get("question") or ""
    assert "Bike" in q or "bike" in q.lower()
    assert "From and To" in q or "office theke" in q or "route" in q.lower()


def test_apply_correction_setake_long_message_during_from_to_pending():
    wf = {}
    pack = process_expense_turn(
        workflow_state=wf,
        message="lunch 100, bus 200, rail 400",
    )
    block = pack["workflow_state"]["expense_request"]
    block["incurred_date_iso"] = "2026-06-08"

    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="ami chaici expense je bus ta ache setake bike koro",
    )
    pending = (pack["workflow_state"]["expense_request"].get("pending_line") or {})
    assert pending.get("category") == "Bike"
    q = pack.get("question") or ""
    assert "Bus" not in q or "Bike" in q


def test_apply_correction_ta_hobe_during_from_to_pending():
    wf = {}
    pack = process_expense_turn(
        workflow_state=wf,
        message="lunch 100, bus 200, rail 400",
    )
    block = pack["workflow_state"]["expense_request"]
    block["incurred_date_iso"] = "2026-06-08"

    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="bus ta bike hobe",
    )
    pending = (pack["workflow_state"]["expense_request"].get("pending_line") or {})
    assert pending.get("category") == "Bike"
    q = pack.get("question") or ""
    assert "From and To" in q or "office theke" in q or "route" in q.lower()


def test_apply_correction_ta_hobe_all_items_committed():
    items = _draft_items()
    result = apply_message_corrections(
        items,
        "lunch er jaigai snack hobe",
        extract_lines=None,
        use_llm=False,
        review_stage=True,
    )
    assert result.changed
    cats = {r["category"] for r in result.items}
    assert "Snack" in cats
    assert "Lunch" not in cats
    assert "Bus" in cats
    assert "Metro Rail" in cats


def test_apply_correction_take_snack_preserves_all_travel():
    items = _draft_items()
    result = apply_message_corrections(
        items,
        "lunch take snack kore daw",
        extract_lines=None,
        use_llm=False,
        review_stage=True,
    )
    assert result.changed
    assert len(result.items) == 3
    snack = next(r for r in result.items if r["category"] == "Snack")
    assert snack["amount"] == 100.0


def test_turn_rules_edit_draft_collecting():
    items = _draft_items()
    decision = parse_turn_rules(
        "lunch er jaigai snack hobe",
        items=items,
        stage="collecting",
        pending_step="",
        has_pending_line=False,
    )
    assert decision.turn_type == TURN_EDIT_DRAFT
    assert decision.plan.has_any_correction()


def test_draft_edit_signal_for_banglish_variants():
    items = _draft_items()
    assert looks_like_draft_edit_signal("lunch er jaigai snack hobe", items)
    assert looks_like_draft_edit_signal("lunch take snack kore daw", items)


def test_collecting_turn_jaigai_replaces_lunch_not_clarify():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "incurred_date_iso": "2026-06-07",
            "items": _draft_items(),
        }
    }
    pack = process_expense_turn(workflow_state=wf, message="lunch er jaigai snack hobe")
    items = pack["items"]
    cats = {r["category"] for r in items}
    assert "Snack" in cats
    assert "Lunch" not in cats
    assert "Metro Rail" in cats
    q = pack.get("question") or ""
    assert "বোঝাচ্ছেন" not in q
    assert "নিশ্চিত করতে" not in q
    assert "আপডেট" in q


def test_collecting_turn_take_snack_no_unrelated_clarify():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "collecting",
            "incurred_date_iso": "2026-06-07",
            "items": _draft_items(),
        }
    }
    pack = process_expense_turn(workflow_state=wf, message="lunch take snack kore daw")
    items = pack["items"]
    assert any(r["category"] == "Snack" for r in items)
    assert sum(1 for r in items if r["category"] == "Metro Rail") == 1
    q = pack.get("question") or ""
    assert "বোঝাচ্ছেন" not in q
    assert "নিশ্চিত করতে" not in q


def test_conversation_replay_lunch_snack_variants():
    """Replay:user adds lines, routes, then corrects lunch→snack."""
    wf: dict = {}
    pack = process_expense_turn(
        workflow_state=wf,
        message="lunch 100, bus 200, rail 400",
    )
    block = pack["workflow_state"]["expense_request"]
    block["incurred_date_iso"] = "2026-06-07"

    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="mirpur to motejhil",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="uttora to mirpur",
    )
    for variant in (
        "lunch er jaigai snack hobe",
        "lunch take snack kore daw",
        "lunch ke snack kore daw",
    ):
        pack = process_expense_turn(
            workflow_state=pack["workflow_state"],
            message=variant,
        )
        items = pack["items"]
        cats = [r["category"] for r in items]
        assert "Snack" in cats, variant
        assert "Lunch" not in cats, variant
        assert cats.count("Metro Rail") == 1, variant


def test_llm_fallback_edit_when_regex_misses_novel_phrase():
    items = _draft_items()
    msg = "lunch ta snack banay de"
    llm_payload = {
        "turn_type": "edit_draft",
        "confidence": 0.88,
        "operations": {
            "replacements": [{"from_category": "Lunch", "to_category": "Snack"}],
            "remove_travel_group": False,
            "transfers": [],
            "partial_deducts": [],
            "remove_categories": [],
            "set_amounts": [],
            "add_amounts": [],
        },
    }
    with patch(
        "chat.services.expense.turn_parser.LLMClient.is_configured",
        return_value=True,
    ), patch(
        "chat.services.expense.turn_parser.LLMClient.chat_json",
        return_value=llm_payload,
    ):
        decision = resolve_expense_turn(
            msg,
            items=items,
            stage="collecting",
            trace_id="turn-test",
            use_llm=True,
        )
    assert decision.turn_type == TURN_EDIT_DRAFT
    assert decision.plan.replacements == [("Lunch", "Snack")]
