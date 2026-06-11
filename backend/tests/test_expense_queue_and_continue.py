"""Queue-and-continue during open From/To slot + bare amount disambiguation."""

from chat.services.expense_workflow import process_expense_turn


def _start_bus_from_to_pending():
    pack = process_expense_turn(
        workflow_state={},
        message="ajke bus e 100 taka",
    )
    block = pack["workflow_state"]["expense_request"]
    block["incurred_date_iso"] = "2026-06-11"
    block["reply_language"] = "banglish"
    assert block.get("pending_step") == "from_to"
    assert float((block.get("pending_line") or {}).get("amount") or 0) == 100.0
    return pack


def test_lunch_while_bus_from_to_pending_queues_new_line():
    pack = _start_bus_from_to_pending()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch 150 taka",
    )
    items = pack["items"]
    block = pack["workflow_state"]["expense_request"]
    assert any(r.get("category") == "Lunch" and float(r.get("amount") or 0) == 150 for r in items)
    assert block.get("pending_step") == "from_to"
    assert float((block.get("pending_line") or {}).get("amount") or 0) == 100.0
    q = pack.get("question") or ""
    assert "Lunch" in q or "150" in q
    assert "From" in q or "office theke" in q


def test_snack_while_bus_from_to_pending():
    pack = _start_bus_from_to_pending()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="nasta 50 taka",
    )
    items = pack["items"]
    assert any(r.get("category") == "Snack" and float(r.get("amount") or 0) == 50 for r in items)


def test_bare_amount_correction_single_pending_target():
    pack = _start_bus_from_to_pending()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="amount ta 200 kore dao",
    )
    block = pack["workflow_state"]["expense_request"]
    pending = block.get("pending_line") or {}
    assert float(pending.get("amount") or 0) == 200.0
    assert block.get("pending_step") == "from_to"
    q = pack.get("question") or ""
    assert "200" in q


def test_bare_amount_correction_multiple_targets_asks_which():
    pack = _start_bus_from_to_pending()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch 150 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="amount ta 200 kore dao",
    )
    q = pack.get("question") or ""
    assert "200" in q
    assert "konta" in q.lower() or "কোন" in q or "which" in q.lower()
    assert "Lunch" in q or "Bus" in q
    pending = (pack["workflow_state"]["expense_request"].get("pending_line") or {})
    assert float(pending.get("amount") or 0) == 100.0


def test_specific_bus_amount_correction_after_disambiguation():
    pack = _start_bus_from_to_pending()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch 150 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="bus 200 hobe",
    )
    pending = (pack["workflow_state"]["expense_request"].get("pending_line") or {})
    assert float(pending.get("amount") or 0) == 200.0


def test_lunch_er_amount_replaces_not_duplicates():
    pack = _start_bus_from_to_pending()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch 150 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch er amount ta 200 koro",
    )
    items = pack["items"]
    lunch = [r for r in items if str(r.get("category") or "").lower() == "lunch"]
    assert len(lunch) == 1
    assert float(lunch[0].get("amount") or 0) == 200.0


def test_lunch_update_keeps_pending_bus_in_summary():
    pack = _start_bus_from_to_pending()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch 200 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="snack 200 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="amount ta 300 kore dao",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch",
    )
    q = pack.get("question") or ""
    items = pack["items"]
    lunch = next(r for r in items if str(r.get("category") or "").lower() == "lunch")
    assert float(lunch.get("amount") or 0) == 300.0
    assert "Bus" in q or "bus" in q.lower()
    assert "From" in q or "theke" in q.lower()
    assert "Is the information correct" not in q


def test_lunch_reply_after_bare_amount_disambiguation_sets_first_line():
    """Index 0 must work — ``lunch`` picks the first committed line."""
    pack = process_expense_turn(
        workflow_state={},
        message="ajke lunch 150 snack 200 bus 100",
    )
    wf = pack["workflow_state"]
    wf["expense_request"]["incurred_date_iso"] = "2026-06-11"
    pack = process_expense_turn(workflow_state=wf, message="office theke basha")
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="amount ta 200 kore dao",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch",
    )
    items = pack["items"]
    lunch = next(r for r in items if str(r.get("category") or "").lower() == "lunch")
    bus = next(r for r in items if str(r.get("category") or "").lower() == "bus")
    assert float(lunch.get("amount") or 0) == 200.0
    assert float(bus.get("amount") or 0) == 100.0


def test_snack_reply_after_bare_amount_disambiguation_updates_not_bus_slot():
    """``snack`` after amount disambiguation must not fill bus from/to slot."""
    pack = _start_bus_from_to_pending()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch 150 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="nasta 50 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="amount ta 200 kore dao",
    )
    block = pack["workflow_state"]["expense_request"]
    assert block.get("amount_correction_pending")
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="snack",
    )
    items = pack["items"]
    snack = [r for r in items if str(r.get("category") or "").lower() == "snack"]
    assert len(snack) == 1
    assert float(snack[0].get("amount") or 0) == 200.0
    assert float((block.get("pending_line") or {}).get("amount") or 0) == 100.0


def test_duplicate_snack_add_asks_which_line():
    pack = _start_bus_from_to_pending()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch 150 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="nasta 50 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="snack 200 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="snack e 200 add koro",
    )
    q = pack.get("question") or ""
    assert "2" in q or "dui" in q.lower() or "multiple" in q.lower() or "ekadhik" in q.lower()
    items = pack["items"]
    snacks = [r for r in items if str(r.get("category") or "").lower() == "snack"]
    assert len(snacks) == 2
    assert sorted(float(r.get("amount") or 0) for r in snacks) == [50.0, 200.0]


def test_duplicate_snack_add_resolves_with_amount_hint():
    pack = _start_bus_from_to_pending()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch 150 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="nasta 50 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="snack 200 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="snack e 200 add koro",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="50 taka otate",
    )
    items = pack["items"]
    snacks = sorted(
        float(r.get("amount") or 0)
        for r in items
        if str(r.get("category") or "").lower() == "snack"
    )
    assert snacks == [200.0, 250.0]


def test_second_bus_while_first_bus_from_to_pending_queues_route():
    """Same category+amount must still add another bus line (unlimited lines)."""
    pack = _start_bus_from_to_pending()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch 150 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="nasta 50 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch er amount ta 200 koro",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="bus 100 taka",
    )
    block = pack["workflow_state"]["expense_request"]
    queue = list(block.get("pending_queue") or [])
    pending = block.get("pending_line") or {}
    assert float(pending.get("amount") or 0) == 100.0
    assert str(pending.get("category") or "").lower() == "bus"
    assert any(
        str(row.get("category") or "").lower() == "bus"
        and float(row.get("amount") or 0) == 100.0
        for row in queue
    )


def test_second_bus_after_first_routed_adds_new_line():
    wf = {}
    pack = process_expense_turn(workflow_state=wf, message="ajke bus e 100 taka")
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="office theke motijheel",
    )
    items = pack["items"]
    buses_before = [r for r in items if str(r.get("category") or "").lower() == "bus"]
    assert len(buses_before) == 1
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="bus 100 taka",
    )
    block = pack["workflow_state"]["expense_request"]
    items = pack["items"]
    buses = [r for r in items if str(r.get("category") or "").lower() == "bus"]
    pending = block.get("pending_line") or {}
    queue = list(block.get("pending_queue") or [])
    assert len(buses) == 1
    assert (
        float(pending.get("amount") or 0) == 100.0
        and str(pending.get("category") or "").lower() == "bus"
    ) or any(
        str(row.get("category") or "").lower() == "bus"
        and float(row.get("amount") or 0) == 100.0
        for row in queue
    )


def test_triple_bus_ack_shows_all_pending_lines():
    pack = _start_bus_from_to_pending()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch 150 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="nasta 50 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch er amount ta 200 koro",
    )
    for msg in ("bus 100 taka", "again bus 100 taka", "bus 100 taka add koro"):
        pack = process_expense_turn(workflow_state=pack["workflow_state"], message=msg)
    q = pack.get("question") or ""
    assert q.count("Bus") >= 3 or q.lower().count("bus") >= 3
    block = pack["workflow_state"]["expense_request"]
    queue = list(block.get("pending_queue") or [])
    pending = block.get("pending_line") or {}
    assert str(pending.get("category") or "").lower() == "bus"
    assert len(queue) >= 2


def test_router_expense_edit_during_leave_not_p13():
    from chat.services.session_snapshot import build_session_snapshot
    from chat.services.session_turn_router import route_session_turn
    from chat.constants import INTENT_EXPENSE_CLAIM

    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": "leave_type",
        "draft": {"start_date": "2026-08-15"},
        "suspended_expense": {
            "expense_request": {
                "active": True,
                "stage": "collecting",
                "items": [{"category": "Lunch", "amount": 200}],
                "pending_line": {
                    "amount": 100,
                    "category": "Bus",
                    "from_location": "",
                    "to_location": "",
                },
                "pending_step": "from_to",
            }
        },
    }
    decision = route_session_turn(
        build_session_snapshot("bus update korte chacchi", workflow_state=wf),
        workflow_state=wf,
    )
    assert decision.reason == "P10_expense_correction"
    assert decision.intent == INTENT_EXPENSE_CLAIM


def test_ordinal_amount_confirm_before_apply():
    wf = {}
    pack = process_expense_turn(workflow_state=wf, message="ajke bus e 100 taka")
    wf = pack["workflow_state"]
    wf["expense_request"]["incurred_date_iso"] = "2026-06-11"
    pack = process_expense_turn(workflow_state=wf, message="office theke basha")
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch 150 taka",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="prothom ta 200 tk kore dao",
    )
    block = pack["workflow_state"]["expense_request"]
    pending = block.get("ordinal_amount_confirm_pending") or {}
    assert pending.get("amount") == 200.0
    assert pending.get("index") == 0
    q = pack.get("question") or ""
    assert "200" in q
    assert float(pack["items"][0].get("amount") or 0) == 100.0


def _build_five_line_draft_with_pending_buses():
    """Lunch 200, Snack 50, Lunch 120 + two Bus 100 (one pending, one queued)."""
    pack = process_expense_turn(workflow_state={}, message="lunch 200 taka")
    wf = pack["workflow_state"]
    wf["expense_request"]["incurred_date_iso"] = "2026-06-11"
    wf["expense_request"]["reply_language"] = "en"
    for msg in (
        "snack 50 taka",
        "lunch 120 taka",
        "bus 100 taka",
        "bus 100 taka",
    ):
        pack = process_expense_turn(workflow_state=wf, message=msg)
        wf = pack["workflow_state"]
    block = wf["expense_request"]
    items = pack["items"]
    assert len(items) == 3
    assert block.get("pending_step") == "from_to"
    queue = list(block.get("pending_queue") or [])
    assert len(queue) >= 1
    return pack


def test_g37_delete_disambiguation_no_partial_review_summary():
    pack = _build_five_line_draft_with_pending_buses()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="delete koro",
    )
    q = pack.get("question") or ""
    assert "Which entry should I delete" in q or "delete" in q.lower()
    assert q.count("Bus") >= 2 or q.lower().count("bus") >= 2
    assert "Total: 370 Tk" not in q
    assert "Is the information above correct" not in q


def test_g37_format_expense_summary_includes_pending_queue():
    from chat.services.expense_workflow import format_expense_summary

    pack = _build_five_line_draft_with_pending_buses()
    block = pack["workflow_state"]["expense_request"]
    items = pack["items"]
    summary = format_expense_summary(
        items,
        block=block,
        incurred_date_iso="2026-06-11",
        lang="en",
    )
    assert summary.lower().count("bus") >= 2
    assert "570" in summary.replace(",", "")


def test_g38_delete_koro_then_lunch_asks_which_line():
    pack = _build_five_line_draft_with_pending_buses()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="delete koro",
    )
    block = pack["workflow_state"]["expense_request"]
    assert block.get("delete_disambiguation_pending")
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch",
    )
    q = pack.get("question") or ""
    assert "Multiple" in q or "multiple" in q.lower() or "ekadhik" in q.lower()
    assert "200" in q and "120" in q
    items = pack["items"]
    assert sum(1 for r in items if str(r.get("category") or "").lower() == "lunch") == 2


def test_g38_delete_lunch_200_after_disambiguation():
    pack = _build_five_line_draft_with_pending_buses()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="delete koro",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch",
    )
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch 200 baad daw",
    )
    items = pack["items"]
    lunches = sorted(
        float(r.get("amount") or 0)
        for r in items
        if str(r.get("category") or "").lower() == "lunch"
    )
    assert lunches == [120.0]
    q = pack.get("question") or ""
    assert "Removed" in q or "removed" in q.lower() or "মুছ" in q


def test_g38_lunch_200_delete_not_pending_bus_discard():
    pack = _build_five_line_draft_with_pending_buses()
    pack = process_expense_turn(
        workflow_state=pack["workflow_state"],
        message="lunch-200 delete koro",
    )
    q = pack.get("question") or ""
    assert "incomplete expense" not in q.lower()
    assert "route not set" not in q.lower()
    items = pack["items"]
    lunches = sorted(
        float(r.get("amount") or 0)
        for r in items
        if str(r.get("category") or "").lower() == "lunch"
    )
    assert lunches == [120.0]


def test_g37_duplicate_lunch_amount_correction_by_line_hint():
    pack = _build_five_line_draft_with_pending_buses()
    wf = pack["workflow_state"]
    for msg in ("mirpur to motijheel", "mirpur to motijheel"):
        pack = process_expense_turn(workflow_state=wf, message=msg)
        wf = pack["workflow_state"]
    items = pack["items"]
    lunches = [
        float(r.get("amount") or 0)
        for r in items
        if str(r.get("category") or "").lower() == "lunch"
    ]
    assert 200.0 in lunches
    assert 120.0 in lunches
    pack = process_expense_turn(
        workflow_state=wf,
        message="lunch 200 taka hobe",
    )
    wf = pack["workflow_state"]
    pack = process_expense_turn(workflow_state=wf, message="2 tatei koro")
    items = pack["items"]
    lunches = sorted(
        float(r.get("amount") or 0)
        for r in items
        if str(r.get("category") or "").lower() == "lunch"
    )
    assert lunches.count(200.0) == 2
    pack = process_expense_turn(
        workflow_state=wf,
        message="Lunch · 120 Tk eta 200 hobe",
    )
    items = pack["items"]
    lunches = sorted(
        float(r.get("amount") or 0)
        for r in items
        if str(r.get("category") or "").lower() == "lunch"
    )
    assert lunches == [200.0, 200.0]
