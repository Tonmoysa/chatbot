"""P2/P3 tests — clarify copy variants, language guard, golden replies."""

from unittest.mock import patch

import pytest

from chat.services.expense.clarify import (
    apply_clarification_reply,
    collect_clarification_issues,
    format_clarification_followup_prompt,
    format_clarification_prompt,
)
from chat.services.expense.clarify_copy import ClarifyPromptContext, clarify_intro
from chat.services.expense.clarify_polish import clarify_polish_language_ok
from chat.services.expense.clarify_praise import (
    clarify_praise_ack_template,
    looks_like_clarify_praise_message,
)
from chat.services.expense_message_facts import (
    build_clarify_envelope,
    build_clarify_praise_review_envelope,
    clarify_facts_preserved,
    clarify_praise_facts_preserved,
)
from chat.services.expense_workflow import _try_advance_to_review


def test_clarify_intro_variants_by_context():
    bn_initial = clarify_intro(
        lang="bn",
        context=ClarifyPromptContext(variant="initial", total_issues=2),
    )
    bn_follow = clarify_intro(
        lang="bn",
        context=ClarifyPromptContext(
            variant="followup", total_issues=2, resolved_count=1
        ),
    )
    assert "পর্যালোচনা" in bn_initial or "confirm" in bn_initial.lower()
    assert bn_initial != bn_follow
    assert "ধন্যবাদ" in bn_follow or "open" in bn_follow.lower()


def test_clarify_polish_language_rejects_en_flip():
    template = "পর্যালোচনার আগে কিছু তথ্য নিশ্চিত করতে হবে:\n1. **100 Tk**"
    polished_en = "Thanks for your input. 100 Tk — what category?"
    assert not clarify_polish_language_ok("bn", polished_en, template=template)
    assert clarify_polish_language_ok(
        "bn",
        "ধন্যবাদ — **100 Tk** category ki?",
        template=template,
    )


def test_clarify_facts_preserved_rejects_language_flip():
    issues = collect_clarification_issues(
        [],
        [{"amount": 100, "category": ""}],
    )
    template = format_clarification_prompt(issues, lang="bn")
    envelope = build_clarify_envelope(
        issues, template=template, lang="bn", prompt_variant="initial"
    )
    assert not clarify_facts_preserved(
        envelope, "Thanks — 100 Tk still needs a category."
    )
    assert clarify_facts_preserved(
        envelope, template.replace("পর্যালোচনার", "Review er age")
    )


def _two_issue_setup():
    items = [
        {
            "category": "Metro Rail",
            "amount": 80,
            "from_location": "motejhil",
            "to_location": "mirpur",
        }
    ]
    pending = [{"amount": 100, "category": "", "from_location": "mirpur", "to_location": "motejhil"}]
    issues = collect_clarification_issues(items, pending)
    return items, pending, issues


GOLDEN_AFFIRMATIVES = [
    "ha",
    "hae",
    "han",
    "ji",
    "yes",
    "thik",
    "ok",
    "hoy",
    "ঠিক",
]

GOLDEN_CATEGORIES = [
    "bus",
    "lunch",
    "metro rail",
    "snack",
    "rickshaw",
]


@pytest.mark.parametrize("reply", GOLDEN_AFFIRMATIVES)
def test_golden_affirmative_confirms_typo_not_location(reply):
    items, pending, issues = _two_issue_setup()
    with patch("chat.services.expense.clarify_llm_parser.LLMClient") as mock_llm:
        mock_llm.return_value.is_configured.return_value = False
        out_items, _, unresolved, _, _ = apply_clarification_reply(
            reply, items, issues, pending
        )
    assert out_items[0]["from_location"] == "motejheel"
    assert out_items[0]["from_location"].lower() != reply.lower()
    assert len(unresolved) == 1


@pytest.mark.parametrize("cat", GOLDEN_CATEGORIES)
def test_golden_category_on_open_missing(cat):
    items, pending, issues = _two_issue_setup()
    with patch("chat.services.expense.clarify_llm_parser.LLMClient") as mock_llm:
        mock_llm.return_value.is_configured.return_value = False
        # First confirm typo
        apply_clarification_reply("ha", items, issues, pending)
        # Only missing category left
        issues2 = collect_clarification_issues(items, pending)
        issues2 = [i for i in issues2 if i.kind == "missing_category"]
        _, pending_out, unresolved, _, _ = apply_clarification_reply(
            cat, items, issues2, pending
        )
    assert pending_out[0]["category"]
    assert unresolved == [] or cat.lower() in pending_out[0]["category"].lower()


def test_followup_prompt_uses_followup_variant():
    items, pending, issues = _two_issue_setup()
    prompt = format_clarification_followup_prompt(
        [issues[1]],
        lang="bn",
        total_issues=2,
        resolved_count=1,
    )
    assert "100" in prompt
    assert "ধন্যবাদ" in prompt or "open" in prompt.lower() or "item" in prompt.lower()


def test_clarify_praise_message_detected():
    praise = "awesome...tumi valo vabei analysis korcho...."
    assert looks_like_clarify_praise_message(praise)
    assert not looks_like_clarify_praise_message("motejheel")
    assert not looks_like_clarify_praise_message("2 bus")


@patch("chat.services.expense.clarify_praise_llm.LLMClient")
def test_resolve_praise_llm_primary(mock_llm_cls):
    mock_llm_cls.return_value.is_configured.return_value = True
    mock_llm_cls.return_value.chat_json.return_value = {
        "is_praise_or_meta": True,
        "ack_text": "ধন্যবাদ! Analysis ta helpful mone hocche apnar.",
    }
    from chat.services.expense.clarify_praise import resolve_clarify_praise_for_review

    ctx = resolve_clarify_praise_for_review(
        "tumi onek bhalo analysis koro",
        lang="banglish",
        trace_id="t-praise",
    )
    assert ctx is not None
    assert ctx.is_praise
    assert ctx.source == "llm"
    assert "ধন্যবাদ" in ctx.ack_text or "analysis" in ctx.ack_text.lower()


@patch("chat.services.expense.clarify_praise_llm.LLMClient")
def test_resolve_praise_llm_rejects_non_praise(mock_llm_cls):
    mock_llm_cls.return_value.is_configured.return_value = True
    mock_llm_cls.return_value.chat_json.return_value = {
        "is_praise_or_meta": False,
        "ack_text": "",
    }
    from chat.services.expense.clarify_praise import resolve_clarify_praise_for_review

    ctx = resolve_clarify_praise_for_review("motejheel", lang="bn", trace_id="t-no")
    assert ctx is None


def test_advance_to_review_prepends_praise_ack():
    items = [
        {
            "category": "Bus",
            "amount": 100,
            "from_location": "mirpur",
            "to_location": "motejheel",
        },
        {
            "category": "Metro Rail",
            "amount": 80,
            "from_location": "motejheel",
            "to_location": "mirpur",
        },
        {"category": "Lunch", "amount": 100},
    ]
    block = {"stage": "collecting", "reply_language": "banglish"}
    wf = {"expense_request": block}
    praise = "awesome...tumi valo vabei analysis korcho...."
    from chat.services.expense.clarify_praise import ClarifyPraiseContext

    result = _try_advance_to_review(
        wf,
        block,
        items,
        inc_iso="2026-06-09",
        day_logged_total=0,
        daily_cap=5000,
        message=praise,
        praise_ctx=ClarifyPraiseContext(
            is_praise=True,
            ack_text=clarify_praise_ack_template("banglish", seed=praise),
            source="regex",
        ),
    )
    assert result is not None
    question = result.get("question") or ""
    assert "দৈনিক খরচ" in question or "review" in question.lower()
    assert question.index(clarify_praise_ack_template("banglish", seed=praise)) == 0
    facts = result.get("message_facts") or {}
    assert facts.get("message_type") == "expense_clarify_praise_review"
    assert facts.get("fixed_part")
    assert facts.get("polishable_part")


def test_clarify_praise_envelope_language_guard():
    template = clarify_praise_ack_template("bn", seed="x")
    envelope = build_clarify_praise_review_envelope(
        praise_template=template,
        summary_template="দৈনিক খরচ — পর্যালোচনা",
        items=[{"category": "Lunch", "amount": 100}],
        incurred_date_iso="2026-06-09",
        warnings=[],
        lang="bn",
    )
    assert clarify_praise_facts_preserved(
        envelope, "ধন্যবাদ! আপনার প্রশংসা পেয়ে ভালো লাগলো।"
    )
    assert not clarify_praise_facts_preserved(
        envelope, "Thanks only in English with no Bengali."
    )


@patch("chat.services.expense.clarify_praise_llm.LLMClient")
@patch("chat.services.expense.clarify_llm_parser.LLMClient")
def test_apply_clarify_returns_llm_praise_ctx(mock_clarify_llm, mock_praise_llm):
    items = [
        {
            "category": "Bus",
            "amount": 100,
            "from_location": "mirpur",
            "to_location": "motejhil",
        }
    ]
    pending: list[dict] = []
    issues = collect_clarification_issues(items, pending)
    mock_clarify_llm.return_value.is_configured.return_value = True
    mock_praise_llm.return_value.is_configured.return_value = True
    mock_clarify_llm.return_value.chat_json.return_value = {
        "answers": [{"issue_index": 1, "action": "confirm_typo", "value": "motejheel"}],
        "user_sent_praise_or_meta": True,
    }
    mock_praise_llm.return_value.chat_json.return_value = {
        "is_praise_or_meta": True,
        "ack_text": "Awesome! Khub bhalo laglo je typo ta dhore felte perechi.",
    }
    meta = "awesome...tumi valo vabei analysis korcho...."
    items_out, _, unresolved, disambig, praise_ctx = apply_clarification_reply(
        meta, items, issues, pending, trace_id="t-ctx"
    )
    assert not disambig
    assert unresolved == []
    assert items_out[0]["to_location"] == "motejheel"
    assert praise_ctx is not None
    assert praise_ctx.is_praise
    assert "llm" in praise_ctx.source
    assert "Awesome" in praise_ctx.ack_text or "bhalo" in praise_ctx.ack_text.lower()


@patch("chat.services.expense.clarify_observability.log_step")
def test_clarify_resolver_logs(mock_log_step):
    items, pending, issues = _two_issue_setup()
    with patch("chat.services.expense.clarify_llm_parser.LLMClient") as mock_llm:
        mock_llm.return_value.is_configured.return_value = False
        apply_clarification_reply(
            "hae", items, issues, pending, trace_id="trace-clarify-test"
        )
    mock_log_step.assert_called_once()
    assert mock_log_step.call_args[0][1] == "expense_clarify_resolver"
