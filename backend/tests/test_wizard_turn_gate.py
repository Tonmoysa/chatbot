"""Wizard review gate — casual side talk must not mutate leave/expense drafts."""

from __future__ import annotations

import pytest

from chat.services.wizard_turn_gate import (
    is_casual_wizard_side_statement,
    is_leave_navigation_phrase,
    looks_like_leave_review_update,
)


@pytest.mark.parametrize(
    "message",
    [
        "ajke onek gorom porche",
        "ajke onek gorom?",
        "ajke onek gorom",
        "today is very hot",
    ],
)
def test_weather_is_casual_not_leave_update(message: str) -> None:
    assert is_casual_wizard_side_statement(message)
    assert not looks_like_leave_review_update(message)


@pytest.mark.parametrize(
    "message",
    [
        "reason hobe family emergency",
        "karon change kore janaza te jabo",
        "paid full day kalke",
        "amar paye betha onek",
    ],
)
def test_leave_review_updates_detected(message: str) -> None:
    assert looks_like_leave_review_update(message)
    assert not is_casual_wizard_side_statement(message)


def test_leave_policy_stays_in_scope() -> None:
    assert not is_casual_wizard_side_statement("leave policy ta bolo amake")


def test_leave_navigation_not_review_update() -> None:
    assert is_leave_navigation_phrase("leave e back koro")
    assert not looks_like_leave_review_update("leave e back koro")
