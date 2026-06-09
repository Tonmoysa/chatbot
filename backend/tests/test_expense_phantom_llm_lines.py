"""Regression: LLM must not invent Lunch duplicates or junk bus routes."""

import pytest

from chat.services.expense.wizard_commands import wants_expense_submit_command
from chat.services.expense_workflow import (
    process_expense_turn,
    wants_expense_summary,
)

VOICE_MSG = (
    "আজকে সকালে মিরপুর থেকে মতিঝিল গিয়েছি ১১০ টাকা, অফিসে পৌঁছে নাস্তা করেছি ৫৫ টাকা, "
    "তারপর মতিঝিল থেকে কমলাপুর মেট্রোরেলে ৪০ টাকা, দুপুরে ১৩০ টাকা খরচ হয়েছে, "
    "কমলাপুর থেকে বিমানবন্দর ১২০ টাকা, বিকেলে চা নাস্তা ৪৫ টাকা, "
    "বিমানবন্দর থেকে উত্তরা বাইকে ৮৫ টাকা, একটা পানির বোতল কিনেছি ২৫ টাকা, "
    "উত্তরা থেকে মিরপুর ৭৫ টাকা, রাতে আবার ৬০ টাকা খরচ হয়েছে।"
)
CLARIFY_ANS = (
    "110 taka bus,120 taka train,75 taka bus,130 taka lunch,25 taka snack,60 taka snack"
)


def test_voice_claim_no_phantom_lunch_before_clarify(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    pack = process_expense_turn(workflow_state={}, message=VOICE_MSG)
    block = pack["workflow_state"]["expense_request"]
    items = block.get("items") or []
    lunches = [i for i in items if i.get("category") == "Lunch"]
    assert not lunches
    snacks = [i for i in items if i.get("category") == "Snack"]
    assert {round(float(s["amount"]), 2) for s in snacks} >= {55.0, 45.0}


def test_clarify_reply_no_phantom_lunch_duplicates(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    p1 = process_expense_turn(workflow_state={}, message=VOICE_MSG)
    p2 = process_expense_turn(
        workflow_state=p1["workflow_state"],
        message=CLARIFY_ANS,
    )
    items = p2.get("items") or []
    lunch_55 = [i for i in items if i.get("category") == "Lunch" and i.get("amount") == 55]
    lunch_45 = [i for i in items if i.get("category") == "Lunch" and i.get("amount") == 45]
    assert not lunch_55
    assert not lunch_45
    for row in items:
        if row.get("category") in ("Lunch", "Snack"):
            assert not row.get("from_location")
            assert not row.get("to_location")
    buses = [i for i in items if i.get("category") == "Bus"]
    assert buses
    frm = str(buses[0].get("from_location") or "").lower()
    assert "সকালে" not in frm and "গিয়েছি" not in frm
    assert "mirpur" in frm


def test_submit_command_not_expense_summary():
    msg = "okay...ekhon expense ta submit koro"
    assert wants_expense_submit_command(msg)
    assert not wants_expense_summary(msg)


@pytest.mark.parametrize(
    "msg",
    [
        "ekhon submit koro",
        "okay...ekhon expense ta submit koro",
        "yes..everything is perfect...now submit it",
    ],
)
def test_review_submit_advances(msg, monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    p1 = process_expense_turn(workflow_state={}, message="lunch 100, done")
    p2 = process_expense_turn(
        workflow_state=p1["workflow_state"],
        message=msg,
    )
    stage = (p2["workflow_state"].get("expense_request") or {}).get("stage")
    assert stage == "submit_confirm"
