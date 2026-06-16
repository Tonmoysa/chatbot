"""Ack items + numbered route correction."""

from chat.services.expense.command_executor import execute_correction_plan
from chat.services.expense.command_parser import parse_correction_plan
from chat.services.expense.command_schema import CorrectionCommandPlan
from chat.services.expense.conversation_manager import ExpenseConversationManager
from chat.services.expense.slots import SLOT_MORE_LINES
from chat.services.expense_workflow import process_expense_turn
from chat.services.expense.slots import STAGE_REVIEW


def test_ack_shows_only_new_ingest_lines_not_tail_window():
    mgr = ExpenseConversationManager()
    block = {
        "stage": "collecting",
        "incurred_date_iso": "2026-06-15",
        "reply_language": "banglish",
        "ack_items": [
            {"category": "Bus", "amount": 45, "from_location": "gulshan", "to_location": "mirpur"},
            {"category": "Bus", "amount": 35, "from_location": "mirpur", "to_location": "uttora"},
        ],
    }
    items = [
        {"category": "Bus", "amount": 100, "from_location": "mirpur", "to_location": "badda"},
        {"category": "Lunch", "amount": 100},
        {"category": "Snack", "amount": 50},
        {"category": "Metro Rail", "amount": 60, "from_location": "uttora", "to_location": "mirpur"},
        {"category": "Bus", "amount": 45, "from_location": "gulshan", "to_location": "mirpur"},
        {"category": "Bus", "amount": 35, "from_location": "mirpur", "to_location": "uttora"},
    ]
    q = mgr.build_follow_up(
        block,
        items,
        primary_slot=SLOT_MORE_LINES,
        missing=[SLOT_MORE_LINES],
        lang="banglish",
        incurred_date_iso="2026-06-15",
    )
    assert "gulshan" in q and "mirpur" in q
    assert "Lunch" not in q
    assert "badda" not in q


def test_ordinal_route_correction_updates_only_first_bus():
    items = [
        {"category": "Bus", "amount": 100, "from_location": "mirpur", "to_location": "badda"},
        {"category": "Lunch", "amount": 100},
        {"category": "Bus", "amount": 45, "from_location": "gulshan", "to_location": "mirpur"},
    ]
    plan = parse_correction_plan(
        "first bus ta modify kore gulistan to dhanmondi kore daw",
        item_count=len(items),
    )
    assert plan.set_routes_by_index == [(0, "gulistan", "dhanmondi")]
    result = execute_correction_plan(items, plan)
    assert result.changed
    assert result.items[0]["from_location"] == "gulistan"
    assert result.items[0]["to_location"] == "dhanmondi"
    assert result.items[2]["from_location"] == "gulshan"


def test_ambiguous_bus_route_modify_prompts_numbered_choice():
    items = [
        {"category": "Bus", "amount": 100, "from_location": "mirpur", "to_location": "badda"},
        {"category": "Lunch", "amount": 100},
        {"category": "Bus", "amount": 45, "from_location": "gulshan", "to_location": "mirpur"},
        {"category": "Bus", "amount": 35, "from_location": "mirpur", "to_location": "uttora"},
    ]
    wf = {
        "expense_request": {
            "active": True,
            "stage": STAGE_REVIEW,
            "items": [dict(x) for x in items],
            "incurred_date_iso": "2026-06-15",
        }
    }
    out = process_expense_turn(
        workflow_state=wf,
        message="bus ta modify kore gulistan to dhanmondi kore daw",
    )
    assert "1." in out["question"] or "1 " in out["question"]
    assert out["items"][0]["from_location"] == "mirpur"
    block = out["workflow_state"]["expense_request"]
    assert block.get("route_correction_pending")


def test_compound_bus_claim_adds_lines_not_amount_edit():
    """Two new bus lines must not shrink the first bus from 100 to 60 Tk."""
    from chat.services.expense_workflow import process_expense_turn

    wf = {}
    wf = process_expense_turn(
        workflow_state=wf,
        message=(
            "amar ajke expense hoyeche 100 taka bus mirpur to badda then lunch 100 taka,"
            "then 120 taka bike then 50 taka snack then 60 taka metro rail uttora to mirpur"
        ),
    )["workflow_state"]
    wf = process_expense_turn(workflow_state=wf, message="office to badda")[
        "workflow_state"
    ]
    out = process_expense_turn(
        workflow_state=wf,
        message="bus 60 taka gulistan to dhanmondi then bus 30 taka ecb to kurmitola",
    )
    items = out["items"]
    assert items[0]["amount"] == 100
    assert items[0]["from_location"] == "mirpur"
    buses = [r for r in items if r.get("category") == "Bus"]
    assert len(buses) == 3
    assert any(
        r.get("amount") == 60 and r.get("from_location") == "gulistan"
        for r in buses
    )
    assert any(
        r.get("amount") == 30 and r.get("from_location") == "ecb"
        for r in buses
    )
    ack = out["workflow_state"]["expense_request"].get("ack_items") or []
    assert len(ack) == 2
    assert all(r.get("category") == "Bus" for r in ack)

    summary_out = process_expense_turn(
        workflow_state=out["workflow_state"],
        message="expense er summery ta daw",
    )
    assert "520" in summary_out["question"]
    assert summary_out["items"][0]["amount"] == 100


def test_route_pending_reply_by_number_applies_correct_line():
    items = [
        {"category": "Bus", "amount": 100, "from_location": "mirpur", "to_location": "badda"},
        {"category": "Lunch", "amount": 100},
        {"category": "Bus", "amount": 45, "from_location": "gulshan", "to_location": "mirpur"},
        {"category": "Bus", "amount": 35, "from_location": "mirpur", "to_location": "uttora"},
    ]
    wf = {
        "expense_request": {
            "active": True,
            "stage": STAGE_REVIEW,
            "items": [dict(x) for x in items],
            "incurred_date_iso": "2026-06-15",
            "route_correction_pending": {
                "category": "Bus",
                "from_location": "gulistan",
                "to_location": "dhanmondi",
            },
        }
    }
    out = process_expense_turn(workflow_state=wf, message="2")
    assert out["items"][2]["from_location"] == "gulistan"
    assert out["items"][2]["to_location"] == "dhanmondi"
    assert out["items"][0]["from_location"] == "mirpur"
