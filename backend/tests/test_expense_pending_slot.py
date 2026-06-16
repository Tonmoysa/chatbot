"""Pending travel slot: amount+route in one message, resume without ack replay."""

from chat.services.expense_workflow import format_expense_resume_message, process_expense_turn


def _turn(wf, msg):
    pack = process_expense_turn(workflow_state=wf, message=msg)
    return pack["workflow_state"], pack.get("question") or "", pack["items"]


def test_pending_bike_amount_then_route_amount_completes():
    wf, _, _ = _turn(
        {},
        "bus 100 mirpur to badda, lunch 100, bike 120, snack 50, metro 60 uttora to mirpur",
    )
    wf, q, items = _turn(wf, "bike 130 koro")
    pending = wf["expense_request"].get("pending_line") or {}
    assert str(pending.get("category") or "").lower() == "bike"
    assert float(pending.get("amount") or 0) == 130.0

    wf, q, items = _turn(wf, "bike gulshan to baridhara 145 taka")
    bikes = [
        r
        for r in items
        if str(r.get("category") or "").lower() == "bike"
    ]
    assert any(
        round(float(b.get("amount") or 0)) == 145
        and "gulshan" in str(b.get("from_location") or "").lower()
        and "baridhara" in str(b.get("to_location") or "").lower()
        for b in bikes
    )
    assert not wf["expense_request"].get("pending_line")


def test_resume_no_ack_replay():
    wf, _, _ = _turn(
        {},
        "bus 100 mirpur to badda, lunch 100, bike 120, snack 50, metro 60 uttora to mirpur",
    )
    wf, _, _ = _turn(wf, "bike 130 koro")
    resume = format_expense_resume_message(wf, user_message="expense e back koro")
    assert resume
    assert "Recorded for" not in resume
    assert "Noted for" not in resume
    assert resume.lower().count("bike") <= 1
