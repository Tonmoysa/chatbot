"""Select Leave display — paid/unpaid only (CRM Select Leave dropdown)."""

from chat.services.leave_draft_utils import format_select_leave_label


def test_select_leave_paid():
    assert format_select_leave_label({"leave_payment_category": "paid"}) == "paid"


def test_select_leave_unpaid():
    assert format_select_leave_label({"leave_payment_category": "lwop"}) == "unpaid"


def test_select_leave_ignores_sick_category_in_display():
    assert (
        format_select_leave_label(
            {"leave_type": "sick", "leave_payment_category": "paid"}
        )
        == "paid"
    )
