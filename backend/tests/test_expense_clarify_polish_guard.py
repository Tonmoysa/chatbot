"""Clarify polish must not expand to every line in the user's voice dump."""

import pytest

from chat.services.expense.clarify import ClarificationIssue
from chat.services.expense_message_facts import (
    build_clarify_envelope,
    clarify_facts_preserved,
)
from chat.services.expense_workflow import process_expense_turn
from tests.test_bangla_expense_claim_intent import BANGLA_TEN_LINE_VOICE_DUMP

THREE_LINE_MSG = (
    "আজকে সকালে উত্তরা থেকে গুলশান গিয়েছি ১৪৫ টাকা, অফিসে পৌঁছে নাস্তা করেছি ৭০ টাকা, "
    "তারপর গুলশান থেকে কারওয়ান বাজার মেট্রোরেলে ৫০ টাকা।"
)


def test_clarify_polish_rejects_extra_amounts():
    issues = [
        ClarificationIssue(kind="missing_category", pending_index=0, amount=145.0),
        ClarificationIssue(kind="missing_category", pending_index=1, amount=135.0),
    ]
    template = (
        "পর্যালোচনার আগে কিছু তথ্য নিশ্চিত করতে হবে:\n\n"
        "1. **145 Tk** — category ki?\n"
        "2. **135 Tk** — category ki?\n"
    )
    envelope = build_clarify_envelope(issues, template=template, lang="bn")
    bad_polish = (
        "আজকে সকালে 145 Tk খরচ করেছেন, কোন বিভাগে?\n"
        "নাস্তা 70 Tk, কোন বিভাগে?\n"
        "মেট্রোরেলে 50 Tk, কোন বিভাগে?\n"
        "135 Tk ট্রেনে?\n"
    )
    assert not clarify_facts_preserved(envelope, bad_polish)
    good_polish = (
        "ভালো — আরও দুটো তথ্য দরকার:\n"
        "1. **145 Tk** — category ki?\n"
        "2. **135 Tk** — category ki?\n"
    )
    assert clarify_facts_preserved(envelope, good_polish)


def test_three_line_clarify_pack_includes_expense_clarify_facts(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    pack = process_expense_turn(workflow_state={}, message=THREE_LINE_MSG)
    facts = pack.get("message_facts") or {}
    assert facts.get("message_type") == "expense_clarify"
    issue_amounts = {
        round(float(row.get("amount") or 0))
        for row in (facts.get("facts") or {}).get("issues") or []
    }
    assert issue_amounts == {145}
    items = pack.get("items") or []
    assert {round(float(i["amount"])) for i in items if i.get("category")} == {70, 50}
    q = pack.get("question") or ""
    for amt in (70, 50):
        assert f"{amt} Tk" not in q
    assert "145" in q


def test_bangla_voice_dump_clarify_only_missing_categories(monkeypatch):
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    pack = process_expense_turn(workflow_state={}, message=BANGLA_TEN_LINE_VOICE_DUMP)
    block = pack["workflow_state"]["expense_request"]
    items = pack.get("items") or []
    assert {round(float(i["amount"])) for i in items if i.get("category")} == {
        70,
        50,
        55,
        95,
    }
    q = pack.get("question") or ""
    for amt in (70, 50, 55, 95):
        assert f"{amt} Tk" not in q
    assert "145" in q
    assert "165" in q
    assert block.get("pending_step") == "clarify"
