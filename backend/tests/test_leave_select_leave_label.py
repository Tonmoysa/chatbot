"""Select Leave display — sick / annual / leave without pay."""

from chat.services.leave_draft_utils import format_select_leave_label


def test_select_leave_sick():
    assert format_select_leave_label({"leave_type": "sick"}) == "sick leave"


def test_select_leave_annual():
    assert format_select_leave_label({"leave_type": "annual"}) == "annual leave"


def test_select_leave_unpaid():
    assert format_select_leave_label({"leave_type": "unpaid"}) == "leave without pay"


def test_select_leave_fallback_payment():
    assert format_select_leave_label({"leave_payment_category": "paid"}) == "paid leave"
    assert format_select_leave_label({"leave_payment_category": "lwop"}) == "leave without pay"
