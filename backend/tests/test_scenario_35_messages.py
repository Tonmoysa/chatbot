"""
End-to-end regression: 35-message Bengali leave + expense + policy chat scenario.

Each step maps to the user's numbered transcript. Failures identify which message
breaks and what workflow state / intent was wrong.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest

from chat.constants import (
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_HR_POLICY,
    INTENT_LEAVE_REQUEST,
    INTENT_REQUEST_STATUS,
    INTENT_UNKNOWN,
)
from chat.services.expense.expense_fsm import read_expense_block
from chat.services.leave_fsm import is_awaiting_leave_confirmation, read_leave_state
from chat.services.leave_workflow import is_leave_in_progress
from chat.services.orchestrator import ChatOrchestrator
from chat.services.policy_intent_helpers import is_off_topic_for_hr_assistant
from chat.services.workflow_suspend import has_suspended_leave

COMPANY_ID = "company-a"
EMP = "scenario-35-emp"
FIXED_TODAY = dt.date(2026, 6, 10)


@dataclass
class StepExpect:
    """Per-message expectations (only fields that matter for that step)."""

    id: int
    message: str
    should_work: bool = True
    note: str = ""
    intent: str | None = None
    outcome: str | None = None
    out_of_scope: bool = False
    policy_query: bool = False
    policy_not_found: bool = False
    leave_active: bool | None = None
    expense_items_min: int | None = None
    expense_amounts: list[float] | None = None
    leave_start_date: str | None = None
    leave_days: float | None = None
    leave_reason_contains: str | None = None
    awaiting_confirmation: bool | None = None
    awaiting_delete_confirmation: bool | None = None
    body_contains: list[str] = field(default_factory=list)
    body_not_contains: list[str] = field(default_factory=list)
    custom: Callable[[dict[str, Any], dict[str, Any]], None] | None = None


SCENARIO_STEPS: list[StepExpect] = [
    StepExpect(
        1,
        "আগামী ১৫ জুন ব্যক্তিগত কারণে একদিনের ছুটি নিতে চাই।",
        note="Start leave for June 15, personal reason, 1 day",
        intent=INTENT_LEAVE_REQUEST,
        leave_active=True,
    ),
    StepExpect(
        2,
        "আজকে মিরপুর থেকে মতিঝিল বাসে ১২০ টাকা খরচ হয়েছে।",
        note="Expense: bus 120 BDT",
        intent=INTENT_EXPENSE_CLAIM,
        expense_items_min=1,
        expense_amounts=[120.0],
    ),
    StepExpect(
        3,
        "ছুটিটা ১৫ জুন না, ১৬ জুন করে দাও।",
        note="Leave date correction 15→16 June",
        leave_start_date="2026-06-16",
    ),
    StepExpect(
        4,
        "python কি?",
        should_work=False,
        note="Out-of-scope general knowledge",
        out_of_scope=True,
        body_not_contains=["Python is", "programming language"],
    ),
    StepExpect(
        5,
        "কোম্পানির ক্যাজুয়াল লিভ পলিসি কী?",
        note="Policy query — KB miss → polite not-found (LLM polish)",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        6,
        "annual leave। ছুটির কারণ ব্যক্তিগত না, পারিবারিক কাজ হবে।",
        note="Leave reason correction + explicit annual leave type",
        leave_reason_contains="পারিবারিক",
    ),
    StepExpect(
        7,
        "উত্তরা থেকে গুলশান মেট্রোরেলে ৬০ টাকা খরচ হয়েছে।",
        note="Expense: metro 60 BDT",
        intent=INTENT_EXPENSE_CLAIM,
        expense_amounts=[120.0, 60.0],
    ),
    StepExpect(
        8,
        "দৈনিক ভ্রমণ ভাতা নীতিমালা কী?",
        note="Policy query — travel allowance",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        9,
        "আমার বর্তমান expense summary দেখাও।",
        note="Session expense summary",
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        outcome="INFORMATIONAL",
        body_contains=["120", "60"],
    ),
    StepExpect(
        10,
        "বাস ভাড়া ১২০ টাকা না, ১৫০ টাকা হবে।",
        note="Expense correction bus 120→150",
        expense_amounts=[150.0, 60.0],
    ),
    StepExpect(
        11,
        "ছুটিটা একদিন না, দুইদিনের করে দাও।",
        note="Leave duration 1→2 days",
        leave_days=2.0,
    ),
    StepExpect(
        12,
        "অসুস্থতার ছুটির জন্য কী কী ডকুমেন্ট লাগে?",
        note="Policy: sick leave documents",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        13,
        "আজকে বিকেলে নাস্তা করেছি ৪৫ টাকা।",
        note="Expense: snack 45",
        intent=INTENT_EXPENSE_CLAIM,
        expense_amounts=[150.0, 60.0, 45.0],
    ),
    StepExpect(
        14,
        "আমার pending leave request টা দেখাও।",
        note="Show pending leave draft / request",
        intent=INTENT_LEAVE_REQUEST,
        body_contains=["ছুটি", "জুন"],
    ),
    StepExpect(
        15,
        "javascript কি?",
        should_work=False,
        out_of_scope=True,
        body_not_contains=["JavaScript", "programming"],
    ),
    StepExpect(
        16,
        "ট্রেনে কমলাপুর থেকে বিমানবন্দর ১৩০ টাকা খরচ হয়েছে।",
        note="Expense: train 130",
        intent=INTENT_EXPENSE_CLAIM,
        expense_amounts=[150.0, 60.0, 45.0, 130.0],
    ),
    StepExpect(
        17,
        "expense report submit করার আগে দেখাও।",
        note="Pre-submit expense review",
        body_contains=["150", "130"],
    ),
    StepExpect(
        18,
        "প্রথম expense entry টা delete করো।",
        note="Delete first expense line — confirmation gate",
        awaiting_delete_confirmation=True,
    ),
    StepExpect(
        19,
        "হ্যাঁ delete করো।",
        note="Confirm delete first expense (150)",
        expense_amounts=[60.0, 45.0, 130.0],
    ),
    StepExpect(
        20,
        "annual leave বছরে কতদিন পাওয়া যায়?",
        note="Policy: annual leave entitlement",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        21,
        "leave application submit করো।",
        note="Leave submit → confirmation gate (LLM message)",
        awaiting_confirmation=True,
        body_contains=["চেক", "ঠিক"],
    ),
    StepExpect(
        22,
        "expense application submit করো।",
        note="Expense submit → confirmation gate",
        body_contains=["চেক", "ঠিক"],
    ),
    StepExpect(
        23,
        "আগামীকাল ছুটি চাই।",
        note="Tomorrow leave — block duplicate or start new",
        body_contains=["আগামীকাল"],
    ),
    StepExpect(
        24,
        "আজকে নাস্তা ৫০ টাকা।",
        note="Expense: snack 50",
        intent=INTENT_EXPENSE_CLAIM,
    ),
    StepExpect(
        25,
        "cancel leave।",
        note="Cancel leave with verification",
        body_contains=["নিশ্চিত"],
    ),
    StepExpect(
        26,
        "expense summary দেখাও।",
        note="Expense summary",
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        outcome="INFORMATIONAL",
    ),
    StepExpect(
        27,
        "leave summary দেখাও।",
        note="Leave summary or none after cancel",
        body_contains=["সারাংশ"],
    ),
    StepExpect(
        28,
        "golang কি?",
        should_work=False,
        out_of_scope=True,
    ),
    StepExpect(
        29,
        "আবার ছুটি চাই।",
        note="Fresh leave request",
        intent=INTENT_LEAVE_REQUEST,
        leave_active=True,
    ),
    StepExpect(
        30,
        "কারণ অসুস্থতা।",
        note="Sick reason slot",
        leave_reason_contains="অসুস্থ",
    ),
    StepExpect(
        31,
        "আজকে lunch ১২০ টাকা।",
        note="Expense: lunch 120",
        intent=INTENT_EXPENSE_CLAIM,
    ),
    StepExpect(
        32,
        "sick leave policy কী?",
        note="Policy: sick leave",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        33,
        "lunch ১২০ না, ১৮০ টাকা হবে।",
        note="Expense correction lunch 120→180",
        expense_amounts=[180.0],
    ),
    StepExpect(
        34,
        "ছুটির কারণ অসুস্থতা না, ব্যক্তিগত কাজ হবে।",
        note="Leave reason sick→personal",
        leave_reason_contains="ব্যক্তিগত",
    ),
    StepExpect(
        35,
        "expense submit করো।",
        note="Expense submit with yes confirmation flow",
        body_contains=["চেক", "ঠিক"],
    ),
    StepExpect(
        36,
        "leave submit করো।",
        note="Leave submit — confirmation gate from expense context",
        body_contains=["চেক", "ঠিক"],
    ),
]


def _patch_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    class FixedDate(dt.date):
        @classmethod
        def today(cls):
            return FIXED_TODAY

    for mod in (
        "chat.services.entity_extractor.date",
        "chat.services.leave_workflow.date",
        "chat.services.leave_slot_extraction._today",
        "chat.services.leave_draft_utils.today",
        "chat.services.decision_engine.date",
        "chat.services.expense_incurred_date.date",
    ):
        try:
            monkeypatch.setattr(mod, FixedDate if mod.endswith("date") else lambda: FIXED_TODAY)
        except Exception:
            pass
    monkeypatch.setattr("chat.services.leave_slot_extraction._today", lambda: FIXED_TODAY)
    monkeypatch.setattr("chat.services.leave_draft_utils.today", lambda: FIXED_TODAY)


def _patch_no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "chat.services.intent_detector.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.entity_extractor.LLMClient.is_configured",
        lambda self: False,
    )
    monkeypatch.setattr(
        "chat.services.message_polish_llm.LLMClient.is_configured",
        lambda self: False,
    )


def _patch_policy_rag_miss(monkeypatch: pytest.MonkeyPatch) -> None:
    def _rag_miss(*_a, **_k):
        return {"hit": False, "text": "", "sources": [], "mode": "rag"}

    monkeypatch.setattr("chat.services.orchestrator.try_hr_policy_rag", _rag_miss)


def _patch_polish_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "chat.services.message_polish.polish_outbound_message",
        lambda body, **_k: body,
    )


def _expense_amounts(wf: dict[str, Any]) -> list[float]:
    block = read_expense_block(wf)
    items = block.get("items") or []
    return [float(i.get("amount") or 0) for i in items]


def _leave_draft(wf: dict[str, Any]) -> dict[str, Any]:
    if has_suspended_leave(wf):
        return dict((wf.get("suspended_leave") or {}).get("draft") or {})
    return dict(read_leave_state(wf).get("draft") or {})


def _assert_step(
    step: StepExpect,
    result: dict[str, Any],
    wf: dict[str, Any],
) -> list[str]:
    """Return list of failure messages (empty = pass)."""
    failures: list[str] = []
    body = (result.get("response") or {}).get("message") or ""
    intent = result.get("intent") or ""
    outcome = (result.get("decision") or {}).get("outcome") or ""

    if step.out_of_scope:
        if not is_off_topic_for_hr_assistant(step.message, wizard_active=True):
            failures.append("expected off-topic-for-HR detection")
        if intent != INTENT_UNKNOWN:
            failures.append(f"expected UNKNOWN intent, got {intent}")
        if outcome != "INFORMATIONAL":
            failures.append(f"expected INFORMATIONAL decline, got {outcome}")
        for bad in step.body_not_contains:
            if bad.lower() in body.lower():
                failures.append(f"body must not contain {bad!r}")

    if step.policy_query and step.policy_not_found:
        if "পলিসি" not in body and "policy" not in body.lower():
            if "মিলছিল না" not in body and "খুঁজে পাইনি" not in body:
                failures.append("expected policy-not-found style reply")

    if step.intent and not step.out_of_scope:
        if intent != step.intent:
            failures.append(f"intent: expected {step.intent}, got {intent}")

    if step.outcome:
        if outcome != step.outcome:
            failures.append(f"outcome: expected {step.outcome}, got {outcome}")

    if step.leave_active is not None:
        active = is_leave_in_progress(wf) or has_suspended_leave(wf)
        if active != step.leave_active:
            failures.append(f"leave_active: expected {step.leave_active}, got {active}")

    if step.expense_items_min is not None:
        n = len(read_expense_block(wf).get("items") or [])
        if n < step.expense_items_min:
            failures.append(f"expense items: expected >= {step.expense_items_min}, got {n}")

    if step.expense_amounts is not None:
        amounts = sorted(_expense_amounts(wf), reverse=True)
        expected = sorted(step.expense_amounts, reverse=True)
        if amounts != expected:
            failures.append(f"expense amounts: expected {expected}, got {amounts}")

    draft = _leave_draft(wf)
    if step.leave_start_date:
        got = str(draft.get("start_date") or "")
        if got != step.leave_start_date:
            failures.append(f"leave start_date: expected {step.leave_start_date}, got {got}")

    if step.leave_days is not None:
        from chat.services.leave_days import compute_requested_leave_days

        got_days = compute_requested_leave_days(draft)
        if abs(got_days - step.leave_days) > 0.01:
            failures.append(f"leave days: expected {step.leave_days}, got {got_days}")

    if step.leave_reason_contains:
        reason = str(draft.get("reason") or "")
        if step.leave_reason_contains not in reason:
            failures.append(
                f"leave reason should contain {step.leave_reason_contains!r}, got {reason!r}"
            )

    if step.awaiting_confirmation is not None:
        awaiting = is_awaiting_leave_confirmation(wf)
        if awaiting != step.awaiting_confirmation:
            failures.append(
                f"awaiting_leave_confirmation: expected {step.awaiting_confirmation}, got {awaiting}"
            )

    if step.awaiting_delete_confirmation is not None:
        has_confirm_prompt = any(
            w in body.lower() or w in body
            for w in ("নিশ্চিত", "confirm", "delete", "মুছ", "বাদ", "sure")
        )
        if step.awaiting_delete_confirmation and not has_confirm_prompt:
            failures.append("delete should ask confirmation before removing line")

    for needle in step.body_contains:
        if needle.lower() not in body.lower() and needle not in body:
            failures.append(f"body should contain {needle!r}")

    for needle in step.body_not_contains:
        if needle.lower() in body.lower():
            failures.append(f"body should not contain {needle!r}")

    if step.custom:
        try:
            step.custom(result, wf)
        except AssertionError as exc:
            failures.append(str(exc))

    return failures


@pytest.fixture
def scenario_env(monkeypatch: pytest.MonkeyPatch, settings):
    settings.KB_RAG_ENABLED = True
    _patch_dates(monkeypatch)
    _patch_no_llm(monkeypatch)
    _patch_policy_rag_miss(monkeypatch)
    _patch_polish_passthrough(monkeypatch)
    monkeypatch.setattr(
        "chat.services.orchestrator.conversational_reply",
        lambda **_k: "আমি শুধু HR বিষয়ে সাহায্য করি।",
    )
    return ChatOrchestrator()


@pytest.mark.django_db
def test_scenario_35_messages_e2e(scenario_env: ChatOrchestrator):
    """Run full 35-message chain; collect per-step failures for diagnosis."""
    orch = scenario_env
    sid: str | None = None
    all_failures: list[str] = []

    for step in SCENARIO_STEPS:
        result = orch.run_chat(
            company_id=COMPANY_ID,
            message=step.message,
            session_id=sid,
            employee_id=EMP,
            trace_id=f"s35-{step.id:02d}",
        )
        sid = result["_session_id"]
        session = orch.memory.get_or_create_session(
            company_id=COMPANY_ID, employee_id=EMP, session_id=sid
        )
        wf = dict(session.workflow_state or {})
        failures = _assert_step(step, result, wf)
        if failures:
            snippet = re.sub(r"\s+", " ", ((result.get("response") or {}).get("message") or ""))[:120]
            all_failures.append(
                f"[{step.id}] {step.note or step.message}\n"
                f"  message: {step.message[:80]}\n"
                f"  intent={result.get('intent')} outcome={(result.get('decision') or {}).get('outcome')}\n"
                f"  reply: {snippet}…\n"
                f"  failures: {'; '.join(failures)}"
            )

    if all_failures:
        report = "\n\n".join(all_failures)
        pytest.fail(f"{len(all_failures)} step(s) failed:\n\n{report}")


@pytest.mark.django_db
@pytest.mark.parametrize("step", SCENARIO_STEPS, ids=lambda s: f"msg{s.id:02d}")
def test_scenario_35_messages_individual(step: StepExpect, scenario_env: ChatOrchestrator):
    """
    Parametrized isolation — runs full chain up to each step then asserts.
    Easier to see which message breaks when running pytest -k msg03.
    """
    orch = scenario_env
    sid: str | None = None
    for prior in SCENARIO_STEPS:
        if prior.id > step.id:
            break
        result = orch.run_chat(
            company_id=COMPANY_ID,
            message=prior.message,
            session_id=sid,
            employee_id=f"{EMP}-iso-{step.id:02d}",
            trace_id=f"iso-{prior.id:02d}",
        )
        sid = result["_session_id"]
        if prior.id == step.id:
            session = orch.memory.get_or_create_session(
                company_id=COMPANY_ID,
                employee_id=f"{EMP}-iso-{step.id:02d}",
                session_id=sid,
            )
            wf = dict(session.workflow_state or {})
            failures = _assert_step(step, result, wf)
            assert not failures, f"Step {step.id}: {'; '.join(failures)}"
