"""Bangla / Banglish / EN expense claims must not route to day-summary recap."""

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM, INTENT_EXPENSE_DAY_SUMMARY
from chat.services.expense_extraction import (
    extract_expense_items,
    message_contains_expense_claim_lines,
)
from chat.services.expense_workflow import (
    process_expense_turn,
    wants_expense_spend_recap_query,
    wants_resume_or_show_expense,
)
from chat.services.intent_detector import IntentDetector, _strong_expense_claim

BANGLA_TEN_LINE_VOICE_DUMP = (
    "আজকে সকালে উত্তরা থেকে গুলশান গিয়েছি ১৪৫ টাকা, অফিসে পৌঁছে নাস্তা করেছি ৭০ টাকা, "
    "তারপর গুলশান থেকে কারওয়ান বাজার মেট্রোরেলে ৫০ টাকা, দুপুরে ১৬৫ টাকা খরচ হয়েছে, "
    "কারওয়ান বাজার থেকে কমলাপুর ১৩৫ টাকা, বিকেলে চা নাস্তা ৫৫ টাকা, "
    "কমলাপুর থেকে যাত্রাবাড়ী বাইকে ৯৫ টাকা, একটা পানির বোতল কিনেছি ৩০ টাকা, "
    "যাত্রাবাড়ী থেকে উত্তরা ৯০ টাকা, রাতে আবার ৮০ টাকা খরচ হয়েছে।"
)

BANGLA_NINE_LINE_CLAIM = (
    "আজকে খরচ হয়েছে মিরপুর থেকে মতিঝিল বাসে একশ টাকা, তারপর নাস্তা করেছি ষাট টাকা, "
    "মতিঝিল থেকে কমলাপুর মেট্রোরেলে চল্লিশ টাকা, কমলাপুর থেকে বিমানবন্দর ট্রেনে একশ বিশ টাকা, "
    "বিমানবন্দর থেকে উত্তরা বাইকে নব্বই টাকা, লাঞ্চ একশ টাকা, বিকেলে চা নাস্তা পঞ্চাশ টাকা, "
    "উত্তরা থেকে মিরপুর মেট্রোরেলে আশি টাকা, রাতে আবার নাস্তা ত্রিশ টাকা।"
)

BANGLISH_ADD_KORE_DAW_CLAIM = (
    "amar ajke expense hoyeche 100 taka bus e mirpur to motejheel then 50 taka expense "
    "hoyeche uttora to mirpur metro rail e..then 100 taka lunch e expense hoyeche "
    "..eta tumi expense e add kore daw"
)


def test_bangla_ten_line_voice_dump_clarify_skips_detected_lines(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    pack = process_expense_turn(workflow_state={}, message=BANGLA_TEN_LINE_VOICE_DUMP)
    q = pack.get("question") or ""
    for detected_amt in (70, 50, 55, 95):
        assert f"{detected_amt} Tk" not in q
    for amt in (145, 135, 90, 165, 30, 80):
        assert str(amt) in q


def test_bangla_ten_line_voice_dump_detects_only_explicit_categories():
    """Category/route only when user said them; ambiguous amounts stay for clarify."""
    ext = extract_expense_items(BANGLA_TEN_LINE_VOICE_DUMP)
    by_amt = {round(i.amount): i for i in ext.items}
    # Explicit in message: নাস্তা, মেট্রোরেলে, চা নাস্তা, বাইকে
    assert by_amt[70].category == "Snack"
    assert by_amt[50].category == "Metro Rail"
    assert by_amt[50].from_location == "gulshan"
    assert by_amt[50].to_location == "karwan bazar"
    assert by_amt[55].category == "Snack"
    assert by_amt[95].category == "Bike"
    assert by_amt[95].from_location == "kamalapur"
    assert by_amt[95].to_location == "jatrabari"
    # No category word in message — must not guess Bus/Train/Lunch/Snack
    assert by_amt[145].category == ""
    assert by_amt[145].from_location == "uttora"
    assert by_amt[145].to_location == "gulshan"
    assert by_amt[135].category == ""
    assert len(ext.malformed) >= 3
    assert any("165" in m for m in ext.malformed)
    assert any("80" in m for m in ext.malformed)


def test_bangla_ten_line_voice_dump_asks_clarify_for_missing_categories(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    pack = process_expense_turn(workflow_state={}, message=BANGLA_TEN_LINE_VOICE_DUMP)
    er = (pack.get("workflow_state") or {}).get("expense_request") or {}
    assert er.get("stage") != "review"
    assert er.get("pending_step") == "clarify" or er.get("stage") == "collecting"


def test_bangla_compound_claim_not_recap():
    assert message_contains_expense_claim_lines(BANGLA_NINE_LINE_CLAIM)
    assert not wants_expense_spend_recap_query(BANGLA_NINE_LINE_CLAIM)
    assert _strong_expense_claim(BANGLA_NINE_LINE_CLAIM)
    assert not wants_resume_or_show_expense(BANGLA_NINE_LINE_CLAIM)


def test_banglish_add_kore_daw_claim_not_recap():
    """Regression: 'add kore daw' + motejheel must not match recap (mot substring)."""
    from chat.services.expense.session_ledger import wants_session_expense_ledger_query
    from chat.services.expense_workflow import wants_expense_summary
    from chat.services.intent_detector import _strong_expense_day_summary

    assert message_contains_expense_claim_lines(BANGLISH_ADD_KORE_DAW_CLAIM)
    assert not wants_session_expense_ledger_query(BANGLISH_ADD_KORE_DAW_CLAIM)
    assert not wants_expense_summary(BANGLISH_ADD_KORE_DAW_CLAIM)
    assert not wants_expense_spend_recap_query(BANGLISH_ADD_KORE_DAW_CLAIM)
    assert not _strong_expense_day_summary(BANGLISH_ADD_KORE_DAW_CLAIM)
    assert _strong_expense_claim(BANGLISH_ADD_KORE_DAW_CLAIM)


def test_banglish_add_kore_daw_claim_starts_wizard(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    pack = process_expense_turn(workflow_state={}, message=BANGLISH_ADD_KORE_DAW_CLAIM)
    items = pack.get("items") or []
    er = (pack.get("workflow_state") or {}).get("expense_request") or {}
    assert len(items) >= 3
    assert er.get("stage") == "review"


def test_intent_detector_banglish_add_kore_daw_not_day_summary(monkeypatch):
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: True)
    monkeypatch.setattr(
        det._llm,
        "chat_json",
        lambda **kwargs: {"intent": "EXPENSE_DAY_SUMMARY", "confidence": 0.9},
    )
    r = det.detect(BANGLISH_ADD_KORE_DAW_CLAIM, "tid-bn-add-kore-daw")
    assert r["intent"] == INTENT_EXPENSE_CLAIM


def test_bangla_compound_claim_extracts_nine_lines():
    ext = extract_expense_items(BANGLA_NINE_LINE_CLAIM)
    assert len(ext.items) == 9
    assert not ext.malformed
    assert sum(i.amount for i in ext.items) == 670.0
    cats = {i.category for i in ext.items}
    assert cats >= {"Bus", "Snack", "Metro Rail", "Train", "Bike", "Lunch"}
    bus = next(i for i in ext.items if i.category == "Bus")
    assert bus.from_location == "mirpur"
    assert bus.to_location == "motijheel"


def test_bangla_compound_claim_starts_wizard_review():
    pack = process_expense_turn(workflow_state={}, message=BANGLA_NINE_LINE_CLAIM)
    items = pack.get("items") or []
    er = (pack.get("workflow_state") or {}).get("expense_request") or {}
    assert len(items) >= 9
    assert er.get("stage") == "review"


def test_intent_detector_bangla_claim_not_day_summary(monkeypatch):
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: True)
    monkeypatch.setattr(
        det._llm,
        "chat_json",
        lambda **kwargs: {"intent": "EXPENSE_DAY_SUMMARY", "confidence": 0.9},
    )
    r = det.detect(BANGLA_NINE_LINE_CLAIM, "tid-bn-claim")
    assert r["intent"] == INTENT_EXPENSE_CLAIM


@pytest.mark.parametrize(
    "message",
    [
        "amar ajker expense ta bolo",
        "আজকের খরচ কত হয়েছে",
    ],
)
def test_pure_recap_still_day_summary(message):
    assert wants_expense_spend_recap_query(message)
    assert not message_contains_expense_claim_lines(message)
