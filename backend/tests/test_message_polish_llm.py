"""Phase B — LLM message polish guardrails and wiring."""

from unittest.mock import MagicMock

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM
from chat.services.message_polish import polish_outbound_message
from chat.services.message_polish_llm import (
    extract_locked_facts,
    facts_preserved,
    is_llm_message_polish_enabled,
    polish_template_message,
)


def test_extract_locked_facts_bold_dates_refs():
    text = "**Metro Rail** on **2026-06-05** — ref `EXP-ABC-1`"
    facts = extract_locked_facts(text)
    assert "Metro Rail" in facts
    assert "2026-06-05" in facts
    assert "EXP-ABC-1" in facts


def test_facts_preserved_requires_bold_tokens():
    original = "Scope: **leave**, **expense**, **attendance**."
    assert facts_preserved(original, "Only **leave**, **expense**, **attendance** here.")
    assert not facts_preserved(original, "Only **leave** and **expense** here.")


def test_polish_template_message_disabled_without_trace_id():
    base = "I only help with **leave** and **expense**."
    assert (
        polish_template_message(
            base,
            user_message="eid kobe",
            message_type="out_of_scope",
            trace_id=None,
        )
        == base
    )


def test_polish_template_message_fallback_when_llm_strips_facts(monkeypatch):
    monkeypatch.setattr(
        "chat.services.message_polish_llm.is_llm_message_polish_enabled",
        lambda: True,
    )
    llm = MagicMock()
    llm.is_configured.return_value = True
    llm.chat_text.return_value = "Sorry, I cannot help with that."

    base = (
        "General calendar trivia is outside scope — only **leave**, **expense**, "
        "**attendance**, and uploaded **HR policies**."
    )
    out = polish_template_message(
        base,
        user_message="eid kobe",
        message_type="out_of_scope",
        trace_id="t-fallback",
        llm=llm,
    )
    assert out == base


def test_polish_template_message_uses_llm_when_facts_preserved(monkeypatch):
    monkeypatch.setattr(
        "chat.services.message_polish_llm.is_llm_message_polish_enabled",
        lambda: True,
    )
    llm = MagicMock()
    llm.is_configured.return_value = True
    polished = (
        "I can't answer general calendar questions — I focus on **leave**, "
        "**expense**, **attendance**, and uploaded **HR policies**."
    )
    llm.chat_text.return_value = polished

    base = (
        "General calendar trivia is outside scope — only **leave**, **expense**, "
        "**attendance**, and uploaded **HR policies**."
    )
    out = polish_template_message(
        base,
        user_message="eid kobe",
        message_type="out_of_scope",
        trace_id="t-ok",
        llm=llm,
    )
    assert out == polished


@pytest.mark.django_db
def test_llm_message_polish_setting_respected(settings):
    settings.LLM_MESSAGE_POLISH = False
    assert is_llm_message_polish_enabled() is False
    settings.LLM_MESSAGE_POLISH = True
    assert is_llm_message_polish_enabled() is True


def test_polish_outbound_expense_wizard_collecting(monkeypatch):
    monkeypatch.setattr(
        "chat.services.message_polish_llm.polish_template_message",
        lambda msg, **kwargs: msg.replace("Ar kono", "Any more"),
    )
    base = (
        "নোট করেছি — **2026-06-05**:\n"
        "- **Lunch** — **50 Tk**\n\n"
        "Ar kono kharcha ache?"
    )
    out = polish_outbound_message(
        base,
        intent=INTENT_EXPENSE_CLAIM,
        outcome="NEEDS_CLARIFICATION",
        user_message="lunch 50",
        decision={"rules_applied": ["EXPENSE_WORKFLOW_COLLECTING"]},
        trace_id="exp-polish-1",
    )
    assert "Any more" in out
    assert "**50 Tk**" in out


def test_polish_outbound_skips_submit_confirm(monkeypatch):
    called = {"n": 0}

    def _polish(*_a, **_k):
        called["n"] += 1
        return "should not run"

    monkeypatch.setattr("chat.services.message_polish_llm.polish_template_message", _polish)
    base = "জমা দেবেন? **100 Tk** total."
    out = polish_outbound_message(
        base,
        intent=INTENT_EXPENSE_CLAIM,
        outcome="NEEDS_CLARIFICATION",
        user_message="yes",
        decision={"rules_applied": ["EXPENSE_WORKFLOW_SUBMIT_CONFIRM"]},
        trace_id="exp-polish-2",
    )
    assert called["n"] == 0
    assert out == base
