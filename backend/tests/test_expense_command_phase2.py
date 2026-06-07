"""Phase 2: typed expense command parser + executor."""

from chat.services.expense.command_executor import (
    apply_message_corrections,
    execute_correction_plan,
)
from chat.services.expense.command_parser import (
    parse_correction_plan,
    parse_wizard_flow_plan,
)
from chat.services.expense.expense_confirm import apply_corrections


def test_parse_correction_plan_multi_set():
    plan = parse_correction_plan("bus 50 taka hobe and bike 150 taka hobe")
    assert plan.has_any_correction()
    cats = {c for c, _ in plan.set_amounts + plan.cat_er_amounts + plan.update_amounts}
    assert "Bus" in cats or any("bus" in str(c).lower() for c in cats)


def test_parse_remove_train_plan():
    plan = parse_correction_plan("remove train")
    assert plan.remove_verb_first == ["Train"] or plan.remove_category_suffix


def test_executor_matches_legacy_apply_corrections():
    items = [
        {"category": "Bus", "amount": 100},
        {"category": "Bike", "amount": 100},
        {"category": "Lunch", "amount": 50},
    ]
    msg = "bus 50 taka hobe and bike 150 taka hobe"
    legacy_out, legacy_changed = apply_corrections(items, msg, extract_lines=None)
    plan = parse_correction_plan(msg)
    exec_out = execute_correction_plan(items, plan)
    assert legacy_changed == exec_out.changed
    assert {r["category"]: r["amount"] for r in legacy_out} == {
        r["category"]: r["amount"] for r in exec_out.items
    }


def test_apply_message_corrections_remove_train():
    items = [
        {"category": "Lunch", "amount": 100},
        {"category": "Train", "amount": 400},
    ]
    result = apply_message_corrections(items, "remove train", extract_lines=None)
    assert result.changed
    assert len(result.items) == 1
    assert result.items[0]["category"] == "Lunch"


def test_wizard_flow_plan_joma_daw():
    plan = parse_wizard_flow_plan("joma daw")
    assert plan.finish_collecting
    assert plan.submit_draft
