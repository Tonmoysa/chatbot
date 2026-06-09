"""P1/P2: rules-only hot paths, post-submit edit block, LLM hardening."""

import pytest

from chat.services.expense.session_action_memory import (
    format_submitted_expense_edit_blocked_answer,
    looks_like_submitted_expense_correction_attempt,
    record_expense_submitted,
)
from chat.services.expense.wizard_commands import (
    wants_expense_done_command,
    wants_expense_done_command_rules_only,
)
from chat.services.llm_client import (
    LLMClient,
    _JSON_HTTP_FAILURES,
    _JSON_CIRCUIT_THRESHOLD,
    clear_llm_trace_state,
)


def test_done_command_rules_only_without_llm(monkeypatch):
    calls: list[str] = []

    def fake_llm(self, **kwargs):
        calls.append("llm")
        return {"finish_collecting": True, "confidence": 0.9}

    monkeypatch.setattr(
        "chat.services.expense.done_intent_llm.LLMClient.chat_json",
        fake_llm,
    )
    msg = "everything seems perfect now"
    assert wants_expense_done_command_rules_only("done")
    assert not wants_expense_done_command(msg, use_llm=False)
    assert calls == []


def test_submitted_correction_attempt_detected():
    wf = record_expense_submitted(
        {},
        items=[{"category": "Bus", "amount": 500}],
        reference_id="EXP-2026-C7A365",
        incurred_date_iso="2026-06-09",
    )
    wf["expense_last_submission"] = {
        "reference_id": "EXP-2026-C7A365",
        "items": [{"category": "Bus", "amount": 500}],
    }
    assert looks_like_submitted_expense_correction_attempt(
        wf, "ok, use 400 instead of 4000"
    )
    assert not looks_like_submitted_expense_correction_attempt(
        wf, "can i edit it after submit?"
    )


def test_submitted_correction_blocked_answer():
    wf = record_expense_submitted(
        {},
        items=[{"category": "Bus", "amount": 500}],
        reference_id="EXP-2026-C7A365",
        incurred_date_iso="2026-06-09",
    )
    wf["expense_last_submission"] = {"reference_id": "EXP-2026-C7A365"}
    ans = format_submitted_expense_edit_blocked_answer(wf, lang="en")
    assert "EXP-2026-C7A365" in ans
    assert "cannot be edited" in ans.lower()


def test_llm_json_circuit_breaker(monkeypatch):
    clear_llm_trace_state("trace-circuit")
    _JSON_HTTP_FAILURES["trace-circuit"] = _JSON_CIRCUIT_THRESHOLD

    def boom(self, **kwargs):
        raise RuntimeError("should not be called")

    monkeypatch.setattr("chat.services.llm_client.LLMClient.is_configured", lambda self: True)
    monkeypatch.setattr("chat.services.llm_client.LLMClient._complete", boom)

    out = LLMClient().chat_json(
        system_prompt="sys",
        user_prompt="hi",
        trace_id="trace-circuit",
    )
    assert out is None


def test_llm_json_second_attempt_without_response_format(monkeypatch):
    clear_llm_trace_state("trace-fallback")
    calls: list[bool] = []

    def fake_complete(self, system_prompt, user_prompt, trace_id, attempt, *, json_format=True):
        calls.append(json_format)
        if json_format:
            return None
        return '{"ok": true}'

    monkeypatch.setattr("chat.services.llm_client.LLMClient.is_configured", lambda self: True)
    monkeypatch.setattr("chat.services.llm_client.LLMClient._complete", fake_complete)

    out = LLMClient().chat_json(
        system_prompt="sys",
        user_prompt="hi",
        trace_id="trace-fallback",
    )
    assert out == {"ok": True}
    assert calls == [True, False]
