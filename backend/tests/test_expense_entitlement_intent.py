"""Allowance / TA-DA entitlement questions must not start the expense claim wizard."""

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM, INTENT_HR_POLICY
from chat.services.intent_detector import IntentDetector
from chat.services.policy_intent_helpers import is_expense_entitlement_query


@pytest.mark.parametrize(
    "message",
    [
        "amar daily allowance koto?",
        "amar ta/da koto per day?",
        "amar TA DA koto protidin",
        "দৈনিক ভাতা কত",
        "expense er rules gula amake bolo....like amar daily budget koto?",
    ],
)
def test_expense_entitlement_detected(message: str) -> None:
    assert is_expense_entitlement_query(message)


@pytest.mark.parametrize(
    "message",
    [
        "lunch 100, bus 50",
        "350 taka cost hoyeche",
        "amar total cost koto hoyeche",
        "ajke koto khoroch hoyeche",
    ],
)
def test_expense_entitlement_not_claim_or_summary(message: str) -> None:
    assert not is_expense_entitlement_query(message)


def test_allowance_koto_intent_is_hr_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: False)
    r = det.detect("amar daily allowance koto?", "tid-allowance")
    assert r["intent"] == INTENT_HR_POLICY
    assert r.get("source") == "rules_override_entitlement"


def test_allowance_koto_not_expense_claim_when_llm_says_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: True)
    monkeypatch.setattr(
        det._llm,
        "chat_json",
        lambda **kwargs: {"intent": "EXPENSE_CLAIM", "confidence": 0.9},
    )
    r = det.detect("amar daily allowance koto?", "tid-allowance-llm")
    assert r["intent"] == INTENT_HR_POLICY
    assert r.get("source") == "rules_override_entitlement"


@pytest.mark.django_db
def test_allowance_question_does_not_prompt_expense_collect() -> None:
    from chat.services.orchestrator import ChatOrchestrator

    orch = ChatOrchestrator()
    pack = orch.run_chat(
        company_id="company-a",
        message="amar daily allowance koto?",
        session_id=None,
        employee_id="entitlement-intent-pytest",
        trace_id="ent-allow-1",
    )
    assert pack["intent"] == INTENT_HR_POLICY
    assert pack["intent"] != INTENT_EXPENSE_CLAIM
    body = pack["response"]["message"] or ""
    assert "আজকের খরচ বলুন" not in body
    assert "lunch 100, bus 50" not in body
