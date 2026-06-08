"""Phase B — LLM message polish guardrails and wiring."""

from unittest.mock import MagicMock

import pytest

from chat.constants import INTENT_EXPENSE_CLAIM, INTENT_LEAVE_REQUEST
from chat.services.message_polish import polish_outbound_message
from chat.services.message_polish_llm import (
    extract_locked_facts,
    facts_preserved,
    is_llm_message_polish_enabled,
    leave_facts_preserved,
    polish_leave_wizard_message,
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


def test_polish_outbound_leave_wizard_collecting(monkeypatch):
    monkeypatch.setattr(
        "chat.services.message_polish_llm.polish_leave_wizard_message",
        lambda msg, **kwargs: msg.replace(
            "এখন জানাবেন", "দয়া করে জানাবেন"
        ),
    )
    base = (
        "**3 দিনের** ছুটি — নোট করা হয়েছে।\n"
        "কারণ: **onek osusto** — নোট করা হয়েছে।\n\n"
        "এখন জানাবেন:\n"
        "• **Paid** নাকি **unpaid**?\n"
        "• **Full Day** নাকি **Half Day**?"
    )
    out = polish_outbound_message(
        base,
        intent=INTENT_LEAVE_REQUEST,
        outcome="NEEDS_CLARIFICATION",
        user_message="3 diner sick leave",
        decision={"rules_applied": ["LEAVE_WORKFLOW_COLLECTING"]},
        trace_id="leave-polish-1",
    )
    assert "দয়া করে জানাবেন" in out
    assert "**onek osusto**" in out
    assert "**Paid**" in out


def test_leave_facts_preserved_document_prompt_rephrase():
    original = (
        "ছুটির তারিখ **2026-06-09** থেকে **2026-06-11** — আমার কাছে আছে।\n"
        "কারণ: onek osusto — নোট করা হয়েছে।\n\n"
        "এই ছুটির জন্য **ডাক্তারের চিট** বা কাগজ দিতে পারেন?\n"
        "আপলোড/পেস্ট করুন, অথবা এখন না হলে **skip** / **parbo na** লিখুন।"
    )
    polished = (
        "আপনার ছুটির তারিখ **2026-06-09** থেকে **2026-06-11** নোট করা হয়েছে।\n"
        "কারণ: onek osusto।\n\n"
        "অনুগ্রহ করে ডাক্তারের প্রেসক্রিপশন বা সংশ্লিষ্ট কাগজ আপলোড করুন। "
        "এখন সম্ভব না হলে skip বা parbo na লিখুন — ম্যানেজার রিভিউ নেবেন।"
    )
    assert leave_facts_preserved(original, polished)


def test_polish_leave_wizard_document_prompt_uses_llm(monkeypatch):
    monkeypatch.setattr(
        "chat.services.message_polish_llm.is_llm_message_polish_enabled",
        lambda: True,
    )
    llm = MagicMock()
    llm.is_configured.return_value = True
    llm.chat_text.return_value = (
        "অনুগ্রহ করে ডাক্তারের প্রেসক্রিপশন বা সংশ্লিষ্ট কাগজ আপলোড করুন। "
        "এখন সম্ভব না হলে skip বা parbo na লিখুন — ম্যানেজার রিভিউ নেবেন।"
    )
    base = (
        "এই ছুটির জন্য **ডাক্তারের চিট** বা কাগজ দিতে পারেন?\n"
        "আপলোড/পেস্ট করুন, অথবা এখন না হলে **skip** / **parbo na** লিখুন — ম্যানেজার দেখবেন।"
        "_(ছুটি আবেদন — নিচে উত্তর দিন)_"
    )
    out = polish_leave_wizard_message(
        base,
        user_message="agamikal theke",
        trace_id="leave-doc-polish",
        llm=llm,
    )
    assert "প্রেসক্রিপশন" in out or "ডাক্তার" in out
    assert "skip" in out.lower()
    assert out != base
    assert out.endswith("_(ছুটি আবেদন — নিচে উত্তর দিন)_")


def test_polish_leave_wizard_preserves_footer_marker(monkeypatch):
    monkeypatch.setattr(
        "chat.services.message_polish_llm.is_llm_message_polish_enabled",
        lambda: True,
    )
    llm = MagicMock()
    llm.is_configured.return_value = True
    llm.chat_text.return_value = (
        "আপনার **3 দিনের** ছুটির অনুরোধ নোট করা হয়েছে।\n"
        "কারণ: **onek osusto**।\n\n"
        "দয়া করে জানাবেন — **Paid** নাকি **unpaid**?"
    )
    from chat.services.message_polish_llm import polish_leave_wizard_message

    base = (
        "**3 দিনের** ছুটি — নোট করা হয়েছে।\n"
        "কারণ: **onek osusto** — নোট করা হয়েছে।\n\n"
        "এখন জানাবেন:\n"
        "• **Paid** নাকি **unpaid**?"
        "_(ছুটি আবেদন — নিচে উত্তর দিন)_"
    )
    out = polish_leave_wizard_message(
        base,
        user_message="3 diner sick",
        trace_id="leave-marker",
        llm=llm,
    )
    assert out.endswith("_(ছুটি আবেদন — নিচে উত্তর দিন)_")
    assert "**onek osusto**" in out


def test_polish_outbound_submit_confirm_uses_envelope(monkeypatch):
    from chat.services.expense_copy import submit_confirm_prompt
    from chat.services.expense_message_facts import build_submit_confirm_envelope

    base = submit_confirm_prompt("bn")
    envelope = build_submit_confirm_envelope(base, lang="bn")

    monkeypatch.setattr(
        "chat.services.message_polish_llm.polish_expense_message_with_envelope",
        lambda env, **kwargs: "POLISHED_INTRO\n\n**Expense CRM-এ জমা দেব?**",
    )
    out = polish_outbound_message(
        base,
        intent=INTENT_EXPENSE_CLAIM,
        outcome="NEEDS_CLARIFICATION",
        user_message="yes",
        entities={"expense_message_facts": envelope},
        decision={"rules_applied": ["EXPENSE_WORKFLOW_SUBMIT_CONFIRM"]},
        trace_id="exp-polish-2",
    )
    assert out.startswith("POLISHED_INTRO")
    assert "CRM" in out
