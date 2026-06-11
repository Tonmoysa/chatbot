"""
Classify what the user wants during an active leave wizard (information vs slot vs apply).

Predicates only — routing priority lives in ``session_turn_router``.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from chat.services.leave_balance_intent import is_leave_balance_query
from chat.services.leave_meta_queries import (
    wants_leave_session_summary,
    wants_leave_submission_status,
)
from chat.services.leave.session_action_memory import wants_leave_meta_question


class UserGoal(str, Enum):
    QUERY_BALANCE = "query_balance"
    QUERY_META = "query_meta"
    QUERY_SUMMARY = "query_summary"
    QUERY_POLICY = "query_policy"
    ANSWER_SLOT = "answer_slot"
    CORRECT_DRAFT = "correct_draft"
    APPLY_LEAVE = "apply_leave"
    UNKNOWN = "unknown"


_QUESTION_MARKERS_RE = re.compile(
    r"(?:"
    r"\?|"
    r"\b(koyta|koy\s*ta|koto\s*ta|kotota|koy|koto|kotodin|koydin|kondin)\b|"
    r"\b(balance|remaining|left|baki|baaki)\b|"
    r"\b(ache|ase|aache|থাক|কত|কয়)\b|"
    r"\bhow\s+many\b"
    r")",
    re.I | re.UNICODE,
)

_WIZARD_TOKEN_ONLY_RE = re.compile(
    r"^(?:paid|unpaid|lwop|full|half|sick|casual|annual|anual|anul|emergency|"
    r"maternity|paternity)(?:\s+leave)?(?:\s+day)?s?$",
    re.I,
)


def has_leave_question_marker(message: str) -> bool:
    return bool(_QUESTION_MARKERS_RE.search(message or ""))


def classify_leave_user_goal(
    message: str,
    *,
    leave_active: bool = False,
    pending_leave_step: str = "",
) -> UserGoal:
    raw = (message or "").strip()
    if not raw:
        return UserGoal.UNKNOWN

    if is_leave_balance_query(raw):
        return UserGoal.QUERY_BALANCE
    if wants_leave_meta_question(raw) or wants_leave_submission_status(raw):
        return UserGoal.QUERY_META
    if wants_leave_session_summary(raw):
        return UserGoal.QUERY_SUMMARY

    low = raw.lower()
    if _WIZARD_TOKEN_ONLY_RE.match(low):
        return UserGoal.ANSWER_SLOT

    if leave_active and has_leave_question_marker(raw):
        if re.search(r"\b(sick|annual|casual|paid|unpaid|full|half)\b", low):
            return UserGoal.QUERY_BALANCE

    step = (pending_leave_step or "").strip().lower()
    if leave_active and step and not has_leave_question_marker(raw):
        if len(raw) <= 64:
            return UserGoal.ANSWER_SLOT

    if re.search(
        r"(ছুটি|chuti|chhuti|leave).{0,30}(চাই|chai|lagbe|apply|নিতে)",
        raw,
        re.I | re.UNICODE,
    ):
        return UserGoal.APPLY_LEAVE

    return UserGoal.UNKNOWN


def is_informational_leave_goal(goal: UserGoal) -> bool:
    return goal in {
        UserGoal.QUERY_BALANCE,
        UserGoal.QUERY_META,
        UserGoal.QUERY_SUMMARY,
        UserGoal.QUERY_POLICY,
    }


def needs_leave_goal_clarification(
    message: str,
    *,
    leave_active: bool = False,
) -> bool:
    """True when wizard token + question cue conflict without a clear informational goal."""
    raw = (message or "").strip()
    if not raw or not leave_active:
        return False
    if is_leave_balance_query(raw):
        return False
    if wants_leave_meta_question(raw) or wants_leave_session_summary(raw):
        return False
    low = raw.lower()
    has_token = bool(
        re.search(
            r"\b(sick|annual|casual|paid|unpaid|full|half|emergency)\b",
            low,
        )
    )
    if not has_token or not has_leave_question_marker(raw):
        return False
    if _WIZARD_TOKEN_ONLY_RE.match(low):
        return False
    return True


def build_leave_goal_clarification(message: str, *, lang: str | None = None) -> str:
    low = (message or "").lower()
    if re.search(r"\bsick\b", low):
        topic = "sick leave balance"
        topic_bn = "sick leave balance"
    elif re.search(r"\bannual\b", low):
        topic = "annual leave balance"
        topic_bn = "annual leave balance"
    else:
        topic = "leave balance"
        topic_bn = "leave balance"

    if lang == "en":
        return (
            f"Do you want your **{topic}**, or are you choosing **leave type** "
            f"for the application in progress?\n\n"
            f"- Balance: e.g. `how many sick leave days left`\n"
            f"- Apply: e.g. `sick leave` or `annual leave`"
        )
    if lang == "banglish":
        return (
            f"Apni **{topic}** jante chan, naki choloman application-e **leave type** "
            f"bolchen?\n\n"
            f"- Balance: `amar sick leave koyta ache`\n"
            f"- Apply: `sick leave` / `annual leave`"
        )
    return (
        f"আপনি **{topic_bn}** জানতে চান, নাকি চলমান আবেদনে **leave type** বলছেন?\n\n"
        f"- Balance: `amar sick leave koyta ache`\n"
        f"- Apply: `sick leave` / `annual leave`"
    )
