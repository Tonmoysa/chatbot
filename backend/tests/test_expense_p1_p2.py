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
import httpx

from chat.services.llm_client import (
    LLMClient,
    _JSON_HTTP_FAILURES,
    _JSON_CIRCUIT_THRESHOLD,
    _maybe_trip_rate_limit,
    _rate_limit_tripped,
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


def _make_429() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://x/chat/completions")
    response = httpx.Response(429, request=request, text="rate limit reached")
    return httpx.HTTPStatusError("429", request=request, response=response)


def test_rate_limit_trips_circuit_for_trace():
    clear_llm_trace_state("trace-429")
    assert not _rate_limit_tripped("trace-429")
    _maybe_trip_rate_limit("trace-429", _make_429())
    assert _rate_limit_tripped("trace-429")
    # Non-429 errors must not trip the rate-limit circuit.
    clear_llm_trace_state("trace-other")
    _maybe_trip_rate_limit("trace-other", RuntimeError("boom"))
    assert not _rate_limit_tripped("trace-other")


def test_rate_limited_trace_short_circuits_json_and_text(monkeypatch):
    clear_llm_trace_state("trace-429b")
    _maybe_trip_rate_limit("trace-429b", _make_429())

    def boom(*args, **kwargs):
        raise AssertionError("LLM should not be called after rate-limit trip")

    monkeypatch.setattr(
        "chat.services.llm_client.LLMClient.is_configured", lambda self: True
    )
    monkeypatch.setattr("chat.services.llm_client.LLMClient._complete", boom)
    monkeypatch.setattr("httpx.Client.post", boom)

    assert (
        LLMClient().chat_json(
            system_prompt="sys", user_prompt="hi", trace_id="trace-429b"
        )
        is None
    )
    assert (
        LLMClient().chat_text(
            system_prompt="sys", user_prompt="hi", trace_id="trace-429b"
        )
        is None
    )


def test_clear_trace_state_resets_rate_limit():
    clear_llm_trace_state("trace-429c")
    _maybe_trip_rate_limit("trace-429c", _make_429())
    assert _rate_limit_tripped("trace-429c")
    clear_llm_trace_state("trace-429c")
    assert not _rate_limit_tripped("trace-429c")
