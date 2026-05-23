"""Outbound message formatting for chat UI."""

from chat.services.message_polish import (
    collapse_pdf_line_breaks,
    format_leave_submitted_message,
    polish_policy_answer,
)


def test_collapse_pdf_line_breaks_merges_orphan_words():
    raw = "●\nপাবলিক\nওয়াইফাই\nব্যবহার\nকরার\nনিষিদ্ধ।"
    out = collapse_pdf_line_breaks(raw)
    assert "পাবলিক" in out
    assert "ওয়াইফাই" in out
    assert out.count("\n") < raw.count("\n")


def test_polish_policy_answer_uses_markdown_bullets():
    raw = "ছুটির নিয়ম\n●\nআবেদন\n●\nঅনুমতি"
    out = polish_policy_answer(raw)
    assert "- " in out or "**" in out


def test_format_leave_submitted_bn_card():
    msg = format_leave_submitted_message(
        entities={
            "start_date": "2026-05-22",
            "end_date": "2026-05-22",
            "leave_type": "sick",
            "leave_payment_category": "paid",
            "day_scope": "half",
        },
        decision={
            "requested_ledger_days": 0.5,
            "balance_days": 12,
            "remaining_balance_days": 11.5,
        },
        reference_id="PHP-LEAVE-ABC",
        deduped=False,
        lang="bn",
    )
    assert "**ছুটি আবেদন জমা হয়েছে**" in msg
    assert "PHP-LEAVE-ABC" in msg
    assert "0.5" in msg
    assert "Your leave request has been submitted" not in msg
