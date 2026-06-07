"""Enterprise expense workflow: extraction, corrections, confirmation."""

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM
from chat.services.expense_extraction import (
    extract_expense_items,
    parse_category_token,
    parse_from_to_locations,
)
from chat.services.expense_workflow import (
    format_expense_summary,
    is_expense_collecting,
    process_expense_turn,
)
from chat.services.orchestrator import ChatOrchestrator

COMPANY_ID = "company-a"


def test_process_expense_turn_tracks_reply_language():
    wf: dict = {}
    r1 = process_expense_turn(
        workflow_state=wf,
        message="Okay. I have to go to Cumilla and it will cost around 3000",
    )
    block = r1["workflow_state"].get("expense_request") or {}
    assert block.get("reply_language") == "en"

    r2 = process_expense_turn(
        workflow_state=r1["workflow_state"],
        message="yes",
    )
    block2 = r2["workflow_state"].get("expense_request") or {}
    assert block2.get("reply_language") == "en"

    r3 = process_expense_turn(
        workflow_state={},
        message="amar ajke bus vara 30 taka",
    )
    block3 = r3["workflow_state"].get("expense_request") or {}
    assert block3.get("reply_language") == "banglish"


def test_parse_office_to_road_7_keeps_full_location():
    assert parse_from_to_locations("office to road 7") == ("office", "road 7")


def test_parse_from_to_ignores_preamble_in_long_clause():
    """Full-clause scan must not treat expense preamble as the From location."""
    clause = "ajke amar expense hoyeche 100 taka bus mirpur to badda"
    assert parse_from_to_locations(clause) == ("mirpur", "badda")


def test_parse_from_to_prefers_route_near_travel_category():
    msg = "amar ajke 100 taka bus mirpur to badda lunch 50"
    assert parse_from_to_locations(msg) == ("mirpur", "badda")


def test_parse_hyphen_route_mirpur_sectors():
    assert parse_from_to_locations("mirpur 1-mirpur 10") == ("mirpur 1", "mirpur 10")


def test_rickshaw_comma_separated_route_in_one_message():
    ext = extract_expense_items("rickshaw 10 taka,office to road 7")
    assert len(ext.items) == 1
    row = ext.items[0]
    assert row.category == "Rickshaw"
    assert row.amount == 10.0
    assert row.from_location == "office"
    assert row.to_location == "road 7"
    assert ext.malformed == []


def test_rail_shorthand_extracted_as_metro_rail():
    msg = "lunch 100, bus 200, rail 400"
    ext = extract_expense_items(msg)
    by_cat = {row.category: row.amount for row in ext.items}
    assert by_cat == {"Lunch": 100.0, "Bus": 200.0, "Metro Rail": 400.0}
    assert parse_category_token("rail") == "Metro Rail"


def test_compound_reentry_after_partial_route_no_duplicate_lunch():
    """Re-sending lunch 100, bus 200, rail 400 must not duplicate Lunch or re-add Bus."""
    msg = "lunch 100, bus 200, rail 400"
    wf: dict = {}
    r1 = process_expense_turn(workflow_state=wf, message=msg)
    r2 = process_expense_turn(
        workflow_state=r1["workflow_state"],
        message="office theke badda",
    )
    items_before = list(r2["items"])
    assert sum(1 for r in items_before if r.get("category") == "Lunch") == 1

    r3 = process_expense_turn(workflow_state=r2["workflow_state"], message=msg)
    items_after = list(r3["items"])
    assert sum(1 for r in items_after if r.get("category") == "Lunch") == 1
    q = r3.get("question") or ""
    assert "unchanged" in q.lower() or "duplicate" in q.lower() or "abar" in q.lower()


def test_joma_daw_keeps_rail_line_in_pending_queue():
    wf: dict = {}
    r1 = process_expense_turn(
        workflow_state=wf,
        message="lunch 100, bus 200, rail 400",
    )
    block = r1["workflow_state"]["expense_request"]
    queue = list(block.get("pending_queue") or [])
    assert any(
        row.get("category") == "Metro Rail" and float(row.get("amount") or 0) == 400.0
        for row in queue
    )
    q1 = r1.get("question") or ""
    assert "Metro Rail" in q1 or "400" in q1
    r2 = process_expense_turn(
        workflow_state=r1["workflow_state"],
        message="joma daw",
    )
    q2 = r2.get("question") or ""
    assert "Metro Rail" in q2 or "400" in q2
    assert "submit" in q2.lower() or "জমা" in q2 or "joma" in q2.lower()


def test_rickshaw_hyphen_route_not_dropped_from_pending():
    """Digits in sector-style routes must not reset the pending From/To line."""
    wf = {"expense_request": {
        "active": True,
        "stage": "collecting",
        "reply_language": "en",
        "items": [
            {"category": "Lunch", "amount": 100},
            {"category": "Bus", "amount": 50, "from_location": "mirpur", "to_location": "motejheel"},
            {"category": "Train", "amount": 100, "from_location": "motijheel", "to_location": "utora"},
        ],
        "pending_line": {
            "amount": 10,
            "category": "Rickshaw",
            "from_location": "",
            "to_location": "",
        },
        "pending_step": "from_to",
    }}
    r = process_expense_turn(workflow_state=wf, message="mirpur 1-mirpur 10")
    items = r["items"]
    rickshaws = [x for x in items if x["category"] == "Rickshaw"]
    assert len(rickshaws) == 1
    assert rickshaws[0]["amount"] == 10
    assert rickshaws[0]["from_location"] == "mirpur 1"
    assert rickshaws[0]["to_location"] == "mirpur 10"


def test_transcript_lunch_bus_train_rickshaw_summary():
    wf: dict = {}
    msgs = [
        "ami ajke 100 taka lunch ,50 taka bus ,and 100 taka train e expense hoyeche",
        "mirpur to motejheel",
        "motijheel to utora",
        "rickshaw 10 taka,office to road 7",
        "summery",
    ]
    for m in msgs:
        wf = process_expense_turn(workflow_state=wf, message=m)["workflow_state"]
    items = (wf.get("expense_request") or {}).get("items") or []
    cats = {r["category"]: r for r in items}
    assert "Rickshaw" in cats
    assert cats["Rickshaw"]["amount"] == 10
    assert cats["Rickshaw"]["from_location"] == "office"
    assert cats["Rickshaw"]["to_location"] == "road 7"
    assert sum(float(r["amount"]) for r in items) == 260


def test_parse_other_category_token():
    assert parse_category_token("other") == "Other"


def test_format_expense_summary_banglish_has_no_llm_parentheticals():
    """Structured line items must not get conversational LLM junk like (keno 70 Tk)."""
    items = [
        {"category": "Bus", "amount": 50, "from_location": "office", "to_location": "motejheel"},
        {"category": "Train", "amount": 100, "from_location": "mirpur", "to_location": "uttora"},
        {"category": "Snack", "amount": 40, "from_location": "", "to_location": ""},
    ]
    msg = format_expense_summary(items, incurred_date_iso="2026-05-24", lang="banglish")
    assert "keno" not in msg.lower()
    assert "ki ki" not in msg.lower()
    assert "(70" not in msg
    assert "Bus" in msg
    assert "50" in msg
    assert "Snack" in msg
    assert "40" in msg


def test_format_expense_summary_english():
    items = [{"category": "Lunch", "amount": 100, "from_location": "", "to_location": ""}]
    msg = format_expense_summary(items, incurred_date_iso="2026-05-24", lang="en")
    assert "review" in msg.lower()
    assert "Total" in msg
    assert "Yes" in msg


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


def test_parse_banglish_location_theke_location():
    assert parse_from_to_locations("uttora theke mirpur") == ("uttora", "mirpur")
    assert parse_from_to_locations("office theke badda") == ("office", "badda")
    assert parse_from_to_locations("uttora thke mirpur") == ("uttora", "mirpur")
    assert parse_from_to_locations("from office to motijheel") == (
        "office",
        "motijheel",
    )


def test_extract_bus_with_banglish_route_prefix():
    ext = extract_expense_items("amar uttora thke mirpur bus vara 50 taka")
    assert len(ext.items) == 1
    bus = ext.items[0]
    assert bus.category == "Bus"
    assert bus.amount == 50.0
    assert bus.from_location == "uttora"
    assert bus.to_location == "mirpur"


def test_from_to_step_accepts_banglish_route():
    wf: dict = {}
    r1 = process_expense_turn(workflow_state=wf, message="bus 50 taka")
    assert (r1["workflow_state"].get("expense_request") or {}).get("pending_step") == "from_to"
    r2 = process_expense_turn(
        workflow_state=r1["workflow_state"],
        message="uttora theke mirpur",
    )
    items = r2["items"]
    assert len(items) == 1
    assert items[0]["category"] == "Bus"
    assert items[0]["from_location"] == "uttora"
    assert items[0]["to_location"] == "mirpur"


def test_route_amount_without_category():
    ext = extract_expense_items("ami uttora theke mirpur 60 taka")
    assert len(ext.items) == 1
    row = ext.items[0]
    assert row.amount == 60.0
    assert row.category == ""
    assert row.from_location == "uttora"
    assert row.to_location == "mirpur"
    assert parse_from_to_locations("ami uttora theke mirpur 60 taka") == (
        "uttora",
        "mirpur",
    )


def test_loose_expense_amount_queued_after_travel_from_to():
    """30 taka without category must be asked after bike From/To is filled."""
    msg = "amar ajker expense 30 taka,lunch 100 taka and bike 100 taka"
    wf: dict = {}
    r1 = process_expense_turn(workflow_state=wf, message=msg)
    er1 = r1["workflow_state"].get("expense_request") or {}
    assert er1.get("pending_step") == "from_to"
    assert any(r["category"] == "Lunch" and r["amount"] == 100 for r in r1["items"])
    queue = list(er1.get("pending_queue") or [])
    assert any(float(e.get("amount") or 0) == 30 for e in queue)

    r2 = process_expense_turn(
        workflow_state=r1["workflow_state"],
        message="uttora to ajimpur",
    )
    q2 = r2.get("question") or ""
    assert "30" in q2
    assert "ধরন" in q2 or "category" in q2.lower()
    bikes = [r for r in r2["items"] if r["category"] == "Bike"]
    assert len(bikes) == 1
    assert bikes[0]["from_location"] == "uttora"
    assert bikes[0]["to_location"] == "ajimpur"

    r3 = process_expense_turn(
        workflow_state=r2["workflow_state"],
        message="snack",
    )
    assert any(r["category"] == "Snack" and r["amount"] == 30 for r in r3["items"])


def test_compound_route_lunch_and_loose_amount(monkeypatch):
    """User transcript: route+60, lunch 100, loose 50 — clarify then assign categories."""
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    msg = "ami uttora theke mirpur 60 taka ,lunch 100 taka,then 50 taka cost hoyeche"
    wf: dict = {}
    r1 = process_expense_turn(workflow_state=wf, message=msg)
    er1 = r1["workflow_state"].get("expense_request") or {}
    assert er1.get("pending_step") == "clarify"
    assert any(r["category"] == "Lunch" and r["amount"] == 100 for r in r1["items"])
    issue_amounts = {
        float(i.get("amount") or 0)
        for i in (er1.get("clarification_issues") or [])
        if i.get("kind") == "missing_category"
    }
    assert 60.0 in issue_amounts
    assert 50.0 in issue_amounts

    r2 = process_expense_turn(workflow_state=r1["workflow_state"], message="bus, snack")
    er2 = r2["workflow_state"].get("expense_request") or {}
    buses = [r for r in r2["items"] if r["category"] == "Bus"]
    snacks = [r for r in r2["items"] if r["category"] == "Snack"]
    assert len(buses) == 1
    assert buses[0]["amount"] == 60
    assert buses[0]["from_location"] == "uttora"
    assert buses[0]["to_location"] == "mirpur"
    assert len(snacks) == 1
    assert snacks[0]["amount"] == 50
    assert er2.get("stage") == "review"


def test_travel_route_after_amount():
    ext = extract_expense_items("bus 50 office to badda, rickshaw 20 badda to motijheel")
    by_cat = {i.category: i for i in ext.items}
    assert by_cat["Bus"].from_location == "office"
    assert by_cat["Bus"].to_location == "badda"
    assert by_cat["Rickshaw"].from_location == "badda"
    assert by_cat["Rickshaw"].to_location == "motijheel"


def test_travel_route_after_reverse_amount_category():
    """Banglish: amount before category, route after — '100 taka bus mirpur to badda'."""
    ext = extract_expense_items("100 taka bus mirpur to badda")
    assert len(ext.items) == 1
    row = ext.items[0]
    assert row.category == "Bus"
    assert row.amount == 100.0
    assert row.from_location == "mirpur"
    assert row.to_location == "badda"


def test_compound_message_reverse_bus_route_with_preamble():
    msg = (
        "ajke amar expense hoyeche 100 taka bus mirpur to badda "
        "then lunch 100 taka then metro rail 30 taka"
    )
    ext = extract_expense_items(msg)
    by_cat = {i.category: i for i in ext.items}
    assert by_cat["Bus"].amount == 100.0
    assert by_cat["Bus"].from_location == "mirpur"
    assert by_cat["Bus"].to_location == "badda"
    assert by_cat["Lunch"].amount == 100.0
    assert by_cat["Metro Rail"].amount == 30.0


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
    assert (r2["workflow_state"].get("expense_request") or {}).get("pending_step") == "clarify"


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
    assert (
        "পর্যালোচনা" in msg1
        or "মোট" in msg1
        or "review" in msg1.lower()
        or "total" in msg1.lower()
    )
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


@pytest.mark.django_db
def test_orchestrator_expense_english_reply_language():
    """English expense turns should get English wizard copy from templates."""
    orch = ChatOrchestrator()
    emp = "expense-wf-en-pytest"

    first = orch.run_chat(
        company_id=COMPANY_ID,
        message=(
            "Okay. I have to go a side visit in Cumilla and it will cost around 3000"
        ),
        session_id=None,
        employee_id=emp,
        trace_id="exp-wf-en-1",
    )
    assert first["intent"] == INTENT_EXPENSE_CLAIM
    msg1 = first["response"]["message"]
    assert "3000" in msg1
    assert "category" in msg1.lower() or "clear" in msg1.lower()
    assert "keno" not in msg1.lower()

    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=first["_session_id"]
    )
    assert (session.workflow_state.get("expense_request") or {}).get("reply_language") == "en"


def test_extract_50_ta_lunch_not_100():
    ext = extract_expense_items("kalke amar 100 taka cost hoyeche...50 ta lunch")
    assert len(ext.items) == 2
    # Lunch 50 must not steal the earlier 100.
    lunch = [i for i in ext.items if i.category == "Lunch"]
    assert len(lunch) == 1
    assert lunch[0].amount == 50.0
    uncategorized = [i for i in ext.items if not (i.category or "").strip()]
    assert len(uncategorized) == 1
    assert uncategorized[0].amount == 100.0


def test_extract_cost_hoyeche_period_luch_typo():
    """User typo: 'hoyeche.luch 20' (no space, luch not lunch)."""
    ext = extract_expense_items("amar ajke 100 taka cost hoyeche.luch 20 taka")
    pairs = {(i.category, i.amount) for i in ext.items}
    assert ("Lunch", 20.0) in pairs
    assert ext.malformed
    assert "100" in ext.malformed[0]


@pytest.mark.django_db
def test_workflow_cost_hoyeche_period_luch_typo():
    pack = process_expense_turn(
        workflow_state={},
        message="amar ajke 100 taka cost hoyeche.luch 20 taka",
    )
    items = pack["items"]
    assert any(r.get("category") == "Lunch" and r.get("amount") == 20 for r in items)
    block = pack["workflow_state"].get("expense_request") or {}
    assert block.get("pending_step") == "clarify"
    assert float((block.get("pending_line") or {}).get("amount") or 0) == 100.0


def test_extract_cost_hoyeche_ellipsis_and_lunch():
    """User-style: '100 cost hoyeche ...and lunch 20' must not drop the 100."""
    ext = extract_expense_items("amar ajke 100 taka cost hoyeche ...and lunch 20 taka")
    assert ("Lunch", 20.0) in {(i.category, i.amount) for i in ext.items}
    assert ext.malformed
    assert "100" in ext.malformed[0]


@pytest.mark.django_db
def test_workflow_cost_hoyeche_ellipsis_and_lunch_prompts_category():
    wf: dict = {}
    pack = process_expense_turn(
        workflow_state=wf,
        message="amar ajke 100 taka cost hoyeche ...and lunch 20 taka",
    )
    items = pack["items"]
    assert any(r.get("category") == "Lunch" and r.get("amount") == 20 for r in items)
    block = pack["workflow_state"].get("expense_request") or {}
    assert block.get("pending_step") == "clarify"
    assert float((block.get("pending_line") or {}).get("amount") or 0) == 100.0


def test_kalke_cost_hoyeche_is_yesterday():
    from datetime import date

    from chat.services.expense_incurred_date import infer_expense_incurred_date_iso

    inc = infer_expense_incurred_date_iso(
        message="kalke amar 100 taka cost hoyeche",
        hints={},
        today=date(2026, 5, 24),
    )
    assert inc == "2026-05-23"


def test_kalke_lagbe_is_tomorrow():
    from datetime import date

    from chat.services.expense_incurred_date import infer_expense_incurred_date_iso

    inc = infer_expense_incurred_date_iso(
        message="amar kalker jonno 300 taka lagbe",
        hints={},
        today=date(2026, 5, 24),
    )
    assert inc == "2026-05-25"


def test_past_date_submit_allowed(monkeypatch):
    from datetime import date

    from chat.services.expense_workflow import is_expense_in_progress

    fixed = date(2026, 5, 24)

    class FixedDate(date):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr("chat.services.expense_workflow.date", FixedDate)

    wf = {
        "expense_request": {
            "active": True,
            "stage": "submit_confirm",
            "incurred_date_iso": "2026-05-23",
            "items": [{"category": "Lunch", "amount": 50}],
            "reply_language": "banglish",
        }
    }
    pack = process_expense_turn(workflow_state=wf, message="yes")
    assert pack.get("submitted")
    assert not is_expense_in_progress(pack["workflow_state"])


def test_future_date_submit_blocked_keeps_draft(monkeypatch):
    from datetime import date

    from chat.services.expense_workflow import is_expense_in_progress

    fixed = date(2026, 5, 24)

    class FixedDate(date):
        @classmethod
        def today(cls):
            return fixed

    monkeypatch.setattr("chat.services.expense_workflow.date", FixedDate)

    wf = {
        "expense_request": {
            "active": True,
            "stage": "submit_confirm",
            "incurred_date_iso": "2026-05-25",
            "items": [{"category": "Lunch", "amount": 50}],
            "reply_language": "banglish",
        }
    }
    pack = process_expense_turn(workflow_state=wf, message="yes")
    assert pack.get("validation_blocked")
    assert not pack.get("submitted")
    assert is_expense_in_progress(pack["workflow_state"])
    assert (pack["workflow_state"].get("expense_request") or {}).get("stage") == "submit_confirm"


def test_expense_submit_status_message_not_submitted():
    from chat.constants import INTENT_EXPENSE_STATUS
    from chat.services.orchestrator import _asks_recent_expense_submission
    from chat.services.response_formatter import build_user_message

    assert _asks_recent_expense_submission("amar expense ki submit hoyeche")
    assert _asks_recent_expense_submission("amar expense ki joma hoyeche")
    msg, status = build_user_message(
        intent=INTENT_EXPENSE_STATUS,
        entities={},
        decision={"outcome": "INFORMATIONAL"},
        crm_payload={"expense_wizard_active": True, "expense_wizard_stage": "submit_confirm"},
    )
    assert "জমা হয়নি" in msg
    assert status == "needs_input"
