"""Enterprise expense workflow: extraction, corrections, confirmation."""

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM
from chat.services.expense_extraction import extract_expense_items, parse_category_token
from chat.services.expense_workflow import (
    format_expense_summary,
    is_expense_collecting,
    process_expense_turn,
)
from chat.services.orchestrator import ChatOrchestrator

COMPANY_ID = "company-a"


def test_parse_other_category_token():
    assert parse_category_token("other") == "Other"


def test_extract_bus_vara_then_sequence():
    """Banglish: bus vara 30, lunch 100, then bus 40 — must not pair 30 with lunch."""
    msg = (
        "ami ajke bus vara 30 taka lunch 100 taka "
        "then abar bus 40 taka cost hoyeche.."
    )
    ext = extract_expense_items(msg)
    pairs = {(i.category, i.amount) for i in ext.items}
    assert ("Bus", 30.0) in pairs
    assert ("Lunch", 100.0) in pairs
    assert ("Bus", 40.0) in pairs
    assert ("Lunch", 30.0) not in pairs
    assert sum(i.amount for i in ext.items) == 170


def test_travel_route_after_amount():
    ext = extract_expense_items("bus 50 office to badda, rickshaw 20 badda to motijheel")
    by_cat = {i.category: i for i in ext.items}
    assert by_cat["Bus"].from_location == "office"
    assert by_cat["Bus"].to_location == "badda"
    assert by_cat["Rickshaw"].from_location == "badda"
    assert by_cat["Rickshaw"].to_location == "motijheel"


def test_extract_multi_item_bangla():
    msg = (
        "আজ lunch এ 100 টাকা খরচ করেছি, "
        "bus এ 50 টাকা, rickshaw এ 20 টাকা"
    )
    ext = extract_expense_items(msg)
    cats = {i.category for i in ext.items}
    assert "Lunch" in cats
    assert "Bus" in cats
    assert "Rickshaw" in cats
    assert sum(i.amount for i in ext.items) == 170


def test_stepped_amount_then_lunch_no_from_to():
    wf: dict = {}
    r1 = process_expense_turn(workflow_state=wf, message="ajke 40 taka cost hoyeche")
    assert "40" in (r1.get("question") or "")
    assert "খরচ" in (r1.get("question") or "") or "lunch" in (r1.get("question") or "").lower()
    assert r1["items"] == []

    r2 = process_expense_turn(
        workflow_state=r1["workflow_state"],
        message="lunch e",
    )
    items = r2["items"]
    assert len(items) == 1
    assert items[0]["category"] == "Lunch"
    assert items[0]["amount"] == 40
    assert not items[0].get("from_location")


def test_bus_requires_from_to_before_review():
    wf: dict = {}
    pack = process_expense_turn(workflow_state=wf, message="bus 50 taka")
    assert "From" in (pack.get("question") or "") or "theke" in (pack.get("question") or "")


def test_review_lunch_update_no_duplicate():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "items": [
                {"category": "Bus", "amount": 40, "from_location": "office", "to_location": "mirpur"},
                {"category": "Lunch", "amount": 60},
                {"category": "Bike", "amount": 100, "from_location": "mirpur", "to_location": "motijheel"},
            ],
        }
    }
    pack = process_expense_turn(workflow_state=wf, message="lunch 70 taka hobe")
    lunches = [r for r in pack["items"] if r["category"] == "Lunch"]
    assert len(lunches) == 1
    assert lunches[0]["amount"] == 70
    assert sum(r["amount"] for r in pack["items"]) == 210


def test_remove_one_duplicate_lunch():
    wf = {
        "expense_request": {
            "active": True,
            "stage": "review",
            "items": [
                {"category": "Bus", "amount": 40},
                {"category": "Lunch", "amount": 70},
                {"category": "Lunch", "amount": 70},
                {"category": "Bike", "amount": 100},
            ],
        }
    }
    pack = process_expense_turn(workflow_state=wf, message="ekta lunch baad jabe")
    lunches = [r for r in pack["items"] if r["category"] == "Lunch"]
    assert len(lunches) == 1


def test_correction_update_amount():
    wf = {"expense_request": {"active": True, "stage": "review", "items": [
        {"category": "Bus", "amount": 50},
        {"category": "Lunch", "amount": 100},
    ]}}
    pack = process_expense_turn(workflow_state=wf, message="bus 50 না 70 হবে")
    items = pack["items"]
    bus = next(r for r in items if r["category"] == "Bus")
    assert bus["amount"] == 70


def test_correction_remove_item():
    wf = {"expense_request": {"active": True, "stage": "review", "items": [
        {"category": "Lunch", "amount": 100},
        {"category": "Bus", "amount": 50},
    ]}}
    pack = process_expense_turn(workflow_state=wf, message="lunch remove করো")
    cats = [r["category"] for r in pack["items"]]
    assert "Lunch" not in cats
    assert "Bus" in cats


@pytest.mark.django_db
def test_transcript_multi_then_lunch_and_no_other():
    """User-style: bus+lunch+uncategorized 100 — lunch kept, 100 asks category not Other."""
    wf: dict = {}
    r1 = process_expense_turn(
        workflow_state=wf,
        message="amar 40 taka cost hoyeche bus e",
    )
    assert (r1["workflow_state"].get("expense_request") or {}).get("pending_step") == "from_to"

    r2 = process_expense_turn(
        workflow_state=r1["workflow_state"],
        message=(
            "first cost office to mirpur bus e 40 taka"
            "...then lunch e 60 taka cost hoyeche"
            "...then abar 100 taka cost hoyeche"
        ),
    )
    items = r2["items"]
    cats = {r["category"]: r["amount"] for r in items}
    assert cats.get("Bus") == 40
    assert cats.get("Lunch") == 60
    assert "Other" not in cats
    assert (r2["workflow_state"].get("expense_request") or {}).get("pending_step") == "category"


@pytest.mark.django_db
def test_orchestrator_expense_confirm_submit():
    orch = ChatOrchestrator()
    emp = "expense-wf-pytest"
    first = orch.run_chat(
        company_id=COMPANY_ID,
        message="lunch 100, bus 50 office to badda, rickshaw 20 badda to motijheel",
        session_id=None,
        employee_id=emp,
        trace_id="exp-wf-1",
    )
    assert first["intent"] == INTENT_EXPENSE_CLAIM
    msg1 = first["response"]["message"]
    assert "পর্যালোচনা" in msg1 or "মোট" in msg1
    assert is_expense_collecting(
        orch.memory.get_or_create_session(
            company_id=COMPANY_ID, employee_id=emp, session_id=first["_session_id"]
        ).workflow_state
    )

    second = orch.run_chat(
        company_id=COMPANY_ID,
        message="হ্যাঁ",
        session_id=first["_session_id"],
        employee_id=emp,
        trace_id="exp-wf-2",
    )
    assert second["decision"]["outcome"] == "NEEDS_CLARIFICATION"
    assert "জমা" in second["response"]["message"] or "submit" in second["response"]["message"].lower()

    third = orch.run_chat(
        company_id=COMPANY_ID,
        message="হ্যাঁ",
        session_id=first["_session_id"],
        employee_id=emp,
        trace_id="exp-wf-3",
    )
    assert third["decision"]["outcome"] == "SUBMITTED"
    assert "জমা" in third["response"]["message"] or "Reference" in third["response"]["message"]
    ref = third["response"]["request_id"] or ""
    msg3 = third["response"]["message"] or ""
    assert ref.startswith("EXP-") or "EXP-" in msg3
