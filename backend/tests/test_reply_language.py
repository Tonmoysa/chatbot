"""Reply language detection and policy answer alignment."""

from unittest.mock import patch

import pytest

from chat.services.translator import (
    align_policy_answer_language,
    align_workflow_answer_language,
    detect_content_language,
    detect_explicit_reply_language,
    detect_reply_language,
    detect_user_language,
    is_translation_request,
    is_weak_language_signal,
    resolve_reply_language,
    strip_policy_footer,
)
from knowledge_base.services.prompts import grounded_user_prompt, reply_language_instruction


def test_detect_reply_language_english():
    assert detect_reply_language("let me know the Termination Policy?") == "en"


def test_detect_reply_language_bangla_script():
    assert detect_reply_language("ছুটি নীতি বলুন") == "bn"


def test_detect_reply_language_banglish():
    assert detect_reply_language("amake leave policy ta bolo") == "banglish"


def test_explicit_bangla_overrides_banglish_words():
    assert detect_explicit_reply_language("explain in bangla") == "bn"
    assert detect_reply_language("explain in bangla") == "bn"
    assert detect_reply_language("termination policy ta explain koro banglai") == "bn"


def test_weak_language_signal_keeps_stored_reply_language():
    assert is_weak_language_signal("yes")
    assert is_weak_language_signal("submit")
    assert is_weak_language_signal("summary")
    assert not is_weak_language_signal("It's for bus travel expense")
    assert resolve_reply_language("yes", "bn") == "bn"
    assert resolve_reply_language("yes", "en") == "en"
    assert resolve_reply_language("submit", "en") == "en"
    assert resolve_reply_language(
        "I have to go to Cumilla and it will cost 3000",
        "bn",
    ) == "en"


def test_align_workflow_answer_translates_to_english():
    bn_prompt = "**3000 টাকা** খরচের তথ্য পেয়েছি — কিন্তু ধরন স্পষ্ট নয়।"
    with patch("chat.services.translator.translate_text") as m_tr:
        m_tr.return_value = (
            "Got the **3000 Tk** expense — but the category is not clear.",
            True,
        )
        out = align_workflow_answer_language(
            bn_prompt,
            user_message=(
                "Okay. I have to go a side visit in Cumilla and it will cost around 3000"
            ),
            stored_lang="en",
            trace_id="t-exp-en",
        )
    assert "3000" in out
    assert "category" in out.lower() or "clear" in out.lower()
    m_tr.assert_called_once()
    assert m_tr.call_args.kwargs["target_lang"] == "en"


def test_is_translation_request_explain_in_bangla():
    assert is_translation_request("explain in bangla") == "bn"
    assert is_translation_request("termination policy ta explain koro banglai") == "bn"


def test_strip_policy_footer():
    body = "Termination Policy\n\n- Gross misconduct\n\n_(Answers come from your uploaded policies.)_"
    assert "uploaded policies" not in strip_policy_footer(body)
    assert "Gross misconduct" in strip_policy_footer(body)


def test_detect_user_language_legacy_maps_banglish_to_bn():
    assert detect_user_language("amake leave policy ta bolo") == "bn"


def test_detect_content_language_bengali_script():
    assert detect_content_language("**বিকাল নীতি**\n\nকিছু বিবরণ") == "bn"


def test_reply_language_instruction_english_mentions_termination():
    assert "English" in reply_language_instruction("en")
    assert "Termination" in reply_language_instruction("en")


def test_grounded_user_prompt_includes_reply_language():
    prompt = grounded_user_prompt(
        user_query="Termination policy?",
        evidence_blocks=["[Policy]\nTermination process..."],
        reply_language="en",
    )
    assert "REPLY_LANGUAGE: English" in prompt


def test_align_policy_answer_translates_bn_to_en_when_user_asked_english():
    bn_answer = "**বিকাল নীতি**\n\n- Gross misconduct"
    with patch("chat.services.translator.translate_text") as m_tr:
        m_tr.return_value = (
            "**Termination Policy**\n\n- Gross misconduct",
            True,
        )
        out = align_policy_answer_language(
            bn_answer,
            user_message="let me know the Termination Policy?",
            trace_id="t-align",
        )
    assert out.startswith("**Termination")
    m_tr.assert_called_once()
    assert m_tr.call_args.kwargs["target_lang"] == "en"


@pytest.mark.django_db
def test_rag_pipeline_passes_english_reply_language(settings):
    settings.KB_RAG_ENABLED = True
    from unittest.mock import MagicMock

    from knowledge_base.services.rag_pipeline import try_hr_policy_rag

    hit = MagicMock()
    hit.payload = {
        "chunk_text": "Termination: gross misconduct, security breach.",
        "section_title": "Termination Policy",
    }
    hit.score = 0.9
    captured: dict = {}

    def _capture_json(**kwargs):
        captured["user_prompt"] = kwargs.get("user_prompt") or ""
        return {
            "answer": "**Termination Policy**\n\n- Gross misconduct",
            "insufficient_evidence": False,
        }

    with patch(
        "knowledge_base.services.rag_pipeline.retrieve_for_query",
        return_value=([hit], 1),
    ):
        with patch("knowledge_base.services.rag_pipeline.LLMClient") as m_llm:
            inst = m_llm.return_value
            inst.is_configured.return_value = True
            inst.chat_json.side_effect = _capture_json
            out = try_hr_policy_rag(
                "let me know the Termination Policy?",
                "t-term-en",
                company_id="company-a",
            )
    assert out and out.get("hit")
    assert "REPLY_LANGUAGE: English" in captured.get("user_prompt", "")
