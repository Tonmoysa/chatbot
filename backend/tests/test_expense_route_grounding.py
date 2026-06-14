"""From/To must come only from the user message — never LLM or prompt examples."""

from __future__ import annotations

from chat.services.expense.entity_merge import _row_to_item, fill_parser_gaps_with_llm
from chat.services.expense_extraction import (
    ExpenseLineItem,
    ExtractionResult,
    extract_expense_items,
    route_explicit_in_user_message,
    strip_ungrounded_travel_routes,
)
from chat.services.expense_workflow import process_expense_turn


def test_route_not_explicit_when_locations_absent():
    assert not route_explicit_in_user_message(
        "bike 120 taka, lunch 200 taka submit kore daw",
        "office",
        "badda",
    )


def test_route_explicit_when_user_stated_it():
    msg = "bus 50 office to badda"
    assert route_explicit_in_user_message(msg, "office", "badda")


def test_row_to_item_rejects_ungrounded_llm_route():
    row = {
        "category": "Bus",
        "amount": 100,
        "from_location": "office",
        "to_location": "badda",
        "notes": "bus 100",
    }
    item = _row_to_item(row, message="ajke bus e 100 taka")
    assert item is not None
    assert not item.from_location
    assert not item.to_location


def test_fill_parser_gaps_skips_ungrounded_llm_route():
    parser = extract_expense_items("ajke bus e 100 taka")
    llm = {
        "expense_lines": [
            {
                "category": "Bus",
                "amount": 100,
                "from_location": "office",
                "to_location": "badda",
            }
        ]
    }
    filled, sources = fill_parser_gaps_with_llm(
        parser, llm, "ajke bus e 100 taka", llm_used=True
    )
    bus = next(it for it in filled.items if it.category == "Bus")
    assert not bus.from_location
    assert not bus.to_location
    assert "line_0_route" not in sources


def test_strip_ungrounded_travel_routes():
    items = [
        ExpenseLineItem(
            category="Bus",
            amount=100,
            from_location="office",
            to_location="badda",
        ),
        ExpenseLineItem(category="Lunch", amount=200),
    ]
    out = strip_ungrounded_travel_routes(
        items, "bike 120 taka, lunch 200 taka submit kore daw"
    )
    bus = next(it for it in out if it.category == "Bus")
    assert not bus.from_location
    assert not bus.to_location


def test_parse_two_routes_from_one_message():
    from chat.services.expense.pending_routes import parse_route_segments

    msg = "mirpur to motejheel and motejheel to mirpur"
    pairs = parse_route_segments(msg)
    assert len(pairs) == 2
    assert pairs[0][0].lower() == "mirpur"
    assert pairs[1][0].lower() == "motejheel"


def test_apply_two_routes_to_pending_bus_and_bike():
    from chat.services.expense.pending_routes import try_apply_pending_routes

    block = {
        "pending_line": {"category": "Bus", "amount": 100.0},
        "pending_step": "from_to",
        "pending_queue": [{"category": "Bike", "amount": 120.0}],
    }
    items = [{"category": "Lunch", "amount": 200.0}]
    result = try_apply_pending_routes(
        block,
        items,
        "mirpur to motejheel and motejheel to mirpur",
    )
    assert result.applied_count == 2
    assert not block.get("pending_line")
    cats = {(r["category"], r.get("from_location", "").lower()) for r in result.items}
    assert ("Bus", "mirpur") in cats
    assert ("Bike", "motejheel") in cats


def test_numbered_route_assignments():
    from chat.services.expense.pending_routes import (
        parse_numbered_route_assignments,
        try_apply_pending_routes,
    )

    numbered = parse_numbered_route_assignments(
        "#3 mirpur to motijheel, #4 motijheel to mirpur"
    )
    assert numbered.get(3) == ("mirpur", "motijheel")
    assert numbered.get(4) == ("motijheel", "mirpur")

    block = {
        "pending_line": {"category": "Bus", "amount": 100.0},
        "pending_step": "from_to",
        "pending_queue": [{"category": "Bike", "amount": 120.0}],
    }
    items = [
        {"category": "Lunch", "amount": 200.0},
        {"category": "Bus", "amount": 50.0, "from_location": "a", "to_location": "b"},
    ]
    result = try_apply_pending_routes(
        block,
        items,
        "#3 mirpur to motijheel, #4 motijheel to mirpur",
    )
    assert result.applied_count == 2


def test_process_expense_turn_applies_both_routes_after_submit_block():
    wf: dict = {}
    r1 = process_expense_turn(workflow_state=wf, message="ajke bus e 100 taka")
    r2 = process_expense_turn(
        workflow_state=r1["workflow_state"],
        message="bike 120 taka ,lunch 200 taka submit kore daw",
    )
    r3 = process_expense_turn(
        workflow_state=r2["workflow_state"],
        message="mirpur to motejheel and motejheel to mirpur",
    )
    items = r3.get("items") or []
    travel = [
        r
        for r in items
        if str(r.get("category") or "").lower() in ("bus", "bike")
        and r.get("from_location")
        and r.get("to_location")
    ]
    assert len(travel) >= 2
    routes = {(r["category"].lower(), r["from_location"].lower()) for r in travel}
    assert ("bus", "mirpur") in routes
    assert ("bike", "motejheel") in routes
    for r in items:
        frm = str(r.get("from_location") or "").lower()
        to = str(r.get("to_location") or "").lower()
        assert not (frm == "office" and to == "badda")


def test_bus_without_route_stays_pending_not_saved_with_office_badda():
    wf: dict = {}
    r1 = process_expense_turn(workflow_state=wf, message="ajke bus e 100 taka")
    block = r1["workflow_state"]["expense_request"]
    assert block.get("pending_step") == "from_to"
    for row in r1["items"]:
        frm = str(row.get("from_location") or "").strip().lower()
        to = str(row.get("to_location") or "").strip().lower()
        assert not (frm == "office" and to == "badda")

    r2 = process_expense_turn(
        workflow_state=r1["workflow_state"],
        message="bike 120 taka ,lunch 200 taka submit kore daw",
    )
    for row in r2["items"]:
        frm = str(row.get("from_location") or "").strip().lower()
        to = str(row.get("to_location") or "").strip().lower()
        assert not (frm == "office" and to == "badda"), (
            f"invented route on saved line: {row}"
        )
