import pytest

from chat.constants import INTENT_HR_POLICY
from chat.services.intent_detector import IntentDetector
from chat.services.policy_intent_helpers import is_irrelevant_answer_complaint


@pytest.mark.parametrize(
    "message",
    [
        "but amar question er sathe toh ei ans gular kono relation nai",
        "ei answer amar question er sathe related na",
        "wrong answer — not related to my question",
        "ei dhoroner kono besoy toh policy te nai....tahole tumi eta kivabe pele?",
    ],
)
def test_irrelevant_answer_complaint_detected(message: str) -> None:
    assert is_irrelevant_answer_complaint(message)


def test_expense_rules_budget_intent_is_hr_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    det = IntentDetector()
    monkeypatch.setattr(det._llm, "is_configured", lambda: False)
    msg = "expense er rules gula amake bolo....like amar daily budget koto?"
    r = det.detect(msg, "tid-exp-rules")
    assert r["intent"] == INTENT_HR_POLICY


@pytest.mark.django_db
def test_policy_complaint_does_not_resume_leave_wizard() -> None:
    from chat.services.orchestrator import ChatOrchestrator

    orch = ChatOrchestrator()
    emp = "policy-complaint-pytest"
    first = orch.run_chat(
        company_id="company-a",
        message="kalke chuti lagbe",
        session_id=None,
        employee_id=emp,
        trace_id="pc-leave-1",
    )
    sid = first["_session_id"]
    pack = orch.run_chat(
        company_id="company-a",
        message="but amar question er sathe toh ei ans gular kono relation nai",
        session_id=sid,
        employee_id=emp,
        trace_id="pc-complaint-1",
    )
    body = pack["response"]["message"] or ""
    assert "ছুটি আবেদন — নিচে উত্তর দিন" not in body
    assert "কোন তারিখ" not in body
    assert "মিলছিল না" in body or "could not find" in body.lower()
