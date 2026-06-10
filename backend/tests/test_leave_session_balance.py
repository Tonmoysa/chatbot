"""Per-type session leave balance."""

from chat.services.crm.mock_crm import MockCRMAdapter
from chat.services.leave.session_ledger import (
    build_session_leave_balance,
    detect_balance_leave_type,
    format_leave_balance_message,
)


def test_detect_balance_leave_type_sick():
    assert detect_balance_leave_type("sick leave koto din ache") == "sick"


def test_detect_balance_leave_type_annual():
    assert detect_balance_leave_type("annual leave baki koto") == "annual"


def test_session_balance_after_submit():
    crm = MockCRMAdapter()
    company = "company-a"
    emp = "emp-bal"
    sid = "sess-bal-1"

    crm.create_request(
        company_id=company,
        employee_id=emp,
        session_id=sid,
        intent="LEAVE_REQUEST",
        entities={
            "leave_type": "sick",
            "day_scope": "full",
            "start_date": "2026-06-10",
            "end_date": "2026-06-12",
            "leave_payment_category": "paid",
        },
        decision={"outcome": "SUBMITTED"},
    )

    pack = build_session_leave_balance(
        crm,
        company_id=company,
        employee_id=emp,
        session_id=sid,
        leave_type_filter="sick",
    )
    sick = pack["balances_by_type"]["sick"]
    assert sick["allocated"] == 12.0
    assert sick["used_session"] == 3.0
    assert sick["remaining"] == 9.0

    msg = format_leave_balance_message(
        pack["balances_by_type"],
        leave_type_filter="sick",
    )
    assert "9" in msg
    assert "Sick leave" in msg
