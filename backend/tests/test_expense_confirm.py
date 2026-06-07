"""Expense confirm gate and inline corrections."""

from chat.services.expense.expense_confirm import (
    apply_corrections,
    dedupe_expense_items,
    is_confirmation_no,
    is_confirmation_yes,
    looks_like_duplicate_expense_reentry,
    looks_like_expense_correction,
)
from chat.services.expense_workflow import process_expense_turn


def test_confirmation_yes_no():
    assert is_confirmation_yes("yes")
    assert is_confirmation_yes("হ্যাঁ")
    assert is_confirmation_no("no")
    assert is_confirmation_no("না")
    assert not is_confirmation_yes("bus 50")


def test_looks_like_expense_correction_bengali_na():
    assert looks_like_expense_correction("bus 50 না 70 হবে")


def test_dedupe_expense_items():
    items = [
        {"category": "Lunch", "amount": 100},
        {"category": "Lunch", "amount": 100},
        {"category": "Bus", "amount": 50},
    ]
    out = dedupe_expense_items(items)
    assert len(out) == 2


def test_apply_corrections_update_amount():
    items = [{"category": "Bus", "amount": 50, "from_location": "a", "to_location": "b"}]
    out, changed = apply_corrections(items, "bus 70")
    assert changed
    assert out[0]["amount"] == 70.0


def test_apply_corrections_remove_category():
    items = [
        {"category": "Lunch", "amount": 100},
        {"category": "Bus", "amount": 50},
    ]
    out, changed = apply_corrections(items, "bus remove")
    assert changed
    assert len(out) == 1
    assert out[0]["category"] == "Lunch"


def test_apply_corrections_transfer_bus_to_bike():
    items = [
        {"category": "Bus", "amount": 200, "from_location": "mirpur", "to_location": "motejheel"},
        {"category": "Bike", "amount": 100, "from_location": "motejheel", "to_location": "mirpur"},
    ]
    out, changed = apply_corrections(
        items, "bus er khorose 50 taka baad diye bike add koro", extract_lines=None
    )
    assert changed
    assert len(out) == 2
    by_cat = {r["category"]: r["amount"] for r in out}
    assert by_cat["Bus"] == 150.0
    assert by_cat["Bike"] == 150.0


def test_apply_corrections_lunch_baad():
    items = [
        {"category": "Lunch", "amount": 50},
        {"category": "Bus", "amount": 100},
    ]
    out, changed = apply_corrections(items, "lunch baad diye daw", extract_lines=None)
    assert changed
    assert len(out) == 1
    assert out[0]["category"] == "Bus"


def test_apply_corrections_remove_travel_group():
    items = [
        {"category": "Bus", "amount": 50, "from_location": "mirpur", "to_location": "motejheel"},
        {"category": "Bike", "amount": 150, "from_location": "motejheel", "to_location": "mirpur"},
        {"category": "Lunch", "amount": 50},
    ]
    out, changed = apply_corrections(items, "travel cost remove koro", extract_lines=None)
    assert changed
    assert len(out) == 1
    assert out[0]["category"] == "Lunch"


def test_apply_corrections_replace_bike_with_train():
    items = [
        {"category": "Bus", "amount": 50},
        {"category": "Bike", "amount": 150, "from_location": "motejheel", "to_location": "mirpur"},
        {"category": "Lunch", "amount": 50},
    ]
    out, changed = apply_corrections(
        items,
        "bus remove koro, bike er poriborte tumi train add koro",
        extract_lines=None,
    )
    assert changed
    cats = {r["category"] for r in out}
    assert "Bus" not in cats
    assert "Train" in cats
    assert "Lunch" in cats
    train = next(r for r in out if r["category"] == "Train")
    assert train["amount"] == 150.0


def test_travel_remove_failure_notice_when_no_travel_lines():
    from chat.services.expense.expense_confirm import build_correction_failure_notice

    items = [{"category": "Lunch", "amount": 50}]
    note = build_correction_failure_notice("travel cost remove koro", items)
    assert note
    assert "travel" in note.lower() or "Travel" in note


def test_replace_failure_notice_when_source_missing():
    from chat.services.expense.expense_confirm import build_correction_failure_notice

    items = [{"category": "Lunch", "amount": 50}]
    note = build_correction_failure_notice("bike er poriborte train add koro", items)
    assert note
    assert "Bike" in note or "bike" in note.lower()


def test_multi_line_correction_not_duplicate():
    items = [
        {"category": "Bus", "amount": 100, "from_location": "mirpur", "to_location": "motejheel"},
        {"category": "Bike", "amount": 100, "from_location": "motejheel", "to_location": "mirpur"},
        {"category": "Lunch", "amount": 50},
    ]
    msg = "bus 50 taka hobe and bike 150 taka hobe"
    assert looks_like_expense_correction(msg)
    assert not looks_like_duplicate_expense_reentry(msg, items)
    out, changed = apply_corrections(items, msg, extract_lines=None)
    assert changed
    by_cat = {r["category"]: r["amount"] for r in out}
    assert by_cat["Bus"] == 50.0
    assert by_cat["Bike"] == 150.0


def test_bos_typo_correction():
    items = [
        {"category": "Bus", "amount": 100},
        {"category": "Bike", "amount": 100},
        {"category": "Lunch", "amount": 50},
    ]
    msg = "eikhane bos er expense 50 taka hobe"
    assert looks_like_expense_correction(msg)
    out, changed = apply_corrections(items, msg, extract_lines=None)
    assert changed
    bus = next(r for r in out if r["category"] == "Bus")
    assert bus["amount"] == 50.0


def test_bike_and_lunch_correction_at_review():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "incurred_date_iso": "2026-06-07",
            "items": [
                {"category": "Bus", "amount": 50},
                {"category": "Bike", "amount": 100},
                {"category": "Lunch", "amount": 50},
            ],
        }
    }
    pack = process_expense_turn(
        workflow_state=wf,
        message="bike 150 taka and lunch 70 taka hobe",
    )
    assert "duplicate" not in (pack.get("question") or "").lower()
    items = pack["items"]
    by_cat = {r["category"]: r["amount"] for r in items}
    assert by_cat["Bike"] == 150.0
    assert by_cat["Lunch"] == 70.0


def test_duplicate_reentry_at_review_shows_notice_not_merge():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "incurred_date_iso": "2026-06-07",
            "items": [
                {"category": "Bus", "amount": 100, "from_location": "mirpur", "to_location": "motejheel"},
                {"category": "Bike", "amount": 100, "from_location": "motejheel", "to_location": "mirpur"},
                {"category": "Lunch", "amount": 50},
            ],
        }
    }
    msg = (
        "amar ajke expense hoyeche 100 taka bus e mirpur to motejheel "
        "then bike e 100 taka cost hoyeche motejheel to mirpur "
        "then lunch e 50 taka expense hoyeche"
    )
    pack = process_expense_turn(workflow_state=wf, message=msg)
    bus = next(r for r in pack["items"] if r["category"] == "Bus")
    assert bus["amount"] == 100
    assert "duplicate" in (pack.get("question") or "").lower() or "submit" in (pack.get("question") or "").lower()
