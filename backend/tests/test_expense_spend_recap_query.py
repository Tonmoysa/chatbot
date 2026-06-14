"""Expense spend recap queries — intent + session summary routing."""

import pytest

from chat.constants import INTENT_EXPENSE_DAY_SUMMARY, INTENT_EXPENSE_CLAIM
from chat.services.expense.session_ledger import wants_pending_expense_query
from chat.services.expense_workflow import (
    wants_expense_spend_recap_query,
    wants_expense_summary,
)
from chat.services.intent_detector import IntentDetector, _strong_expense_day_summary


@pytest.mark.parametrize(
    "message",
    [
        "amar expense koto ajke?",
        "amar ajker expense ta bolo",
        "expense summery ta bolo",
        "amake ajker expense er list ta daw",
        "amar ajke total cost koto hoyeche",
        "pending kono expense ache tomar kache?",
        "amar kache pending expense ache ki?",
        "pending expense ta daw amake",
        "kono pending kharcha ache?",
    ],
)
def test_wants_expense_spend_recap_query(message):
    assert wants_expense_spend_recap_query(message)


@pytest.mark.parametrize(
    "message",
    [
        "pending kono expense ache tomar kache?",
        "amar kache pending expense ache ki?",
        "pending draft expense ki ache?",
    ],
)
def test_wants_pending_expense_query(message):
    assert wants_pending_expense_query(message)


def test_pending_expense_query_not_new_claim():
    assert not wants_pending_expense_query("lunch 100, bus 50 office to badda")
    assert not wants_pending_expense_query("pending bus 50 office to badda")


def test_recap_query_not_new_claim_line():
    assert not wants_expense_spend_recap_query("lunch 100, bus 50 office to badda")
    assert not wants_expense_spend_recap_query("expense 200 taka lunch")


def test_motejheel_not_session_ledger_add_query():
    from chat.services.expense.session_ledger import wants_session_expense_ledger_query

    msg = (
        "amar ajke expense hoyeche 100 taka bus e mirpur to motejheel then 50 taka "
        "expense hoyeche uttora to mirpur metro rail e..then 100 taka lunch e "
        "expense hoyeche ..eta tumi expense e add kore daw"
    )
    assert not wants_session_expense_ledger_query(msg)


def test_strong_day_summary_amar_expense_koto_ajke():
    assert _strong_expense_day_summary("amar expense koto ajke")
    assert _strong_expense_day_summary("amar ajker expense ta bolo")


def test_intent_detector_recap_not_claim(monkeypatch):
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: False)
    for msg in (
        "amar expense koto ajke",
        "amar ajker expense ta bolo",
        "pending kono expense ache tomar kache?",
    ):
        r = det.detect(msg, "tid-recap")
        assert r["intent"] == INTENT_EXPENSE_DAY_SUMMARY, msg


def test_intent_detector_llm_claim_overridden_to_recap(monkeypatch):
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: True)
    monkeypatch.setattr(
        det._llm,
        "chat_json",
        lambda **kwargs: {"intent": "EXPENSE_CLAIM", "confidence": 0.95},
    )
    r = det.detect("amar expense koto ajke", "tid-llm-recap")
    assert r["intent"] == INTENT_EXPENSE_DAY_SUMMARY
    assert r["source"] in ("rules_override", "rules_override_recap")


def test_new_claim_still_expense_claim(monkeypatch):
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: False)
    r = det.detect("lunch 100, bus 50 office to badda", "tid-claim")
    assert r["intent"] == INTENT_EXPENSE_CLAIM
