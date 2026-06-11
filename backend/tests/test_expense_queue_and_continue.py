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
