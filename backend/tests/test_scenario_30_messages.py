"""
End-to-end regression: 30-message Bengali leave + expense + policy chat scenario.

Maps to the user's numbered transcript (Jun 20–23 leave, expense draft lifecycle,
submit flows, duplicate leave, post-submit summary/cancel).
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
    INTENT_UNKNOWN,
)
from chat.services.expense.expense_fsm import read_expense_block
from chat.services.leave_fsm import (
    is_awaiting_leave_confirmation,
    is_leave_submission_locked,
    read_leave_state,
)
from chat.services.leave_workflow import is_leave_in_progress
from chat.services.orchestrator import ChatOrchestrator
from chat.services.policy_intent_helpers import is_off_topic_for_hr_assistant
from chat.services.workflow_suspend import has_suspended_leave

COMPANY_ID = "company-a"
EMP = "scenario-30-emp"
FIXED_TODAY = dt.date(2026, 6, 10)


@dataclass
class StepExpect:
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
    leave_submitted: bool | None = None
    expense_items_min: int | None = None
    expense_amounts: list[float] | None = None
    leave_start_date: str | None = None
    leave_end_date: str | None = None
    leave_days: float | None = None
    leave_reason_contains: str | None = None
    awaiting_leave_confirmation: bool | None = None
    awaiting_delete_confirmation: bool | None = None
    body_contains: list[str] = field(default_factory=list)
    body_not_contains: list[str] = field(default_factory=list)
    custom: Callable[[dict[str, Any], dict[str, Any]], None] | None = None


SCENARIO_STEPS: list[StepExpect] = [
    StepExpect(
        1,
        "আগামী ২০ জুন থেকে ২২ জুন পর্যন্ত ছুটি নিতে চাই।",
        note="Start leave Jun 20–22 (3 days)",
        intent=INTENT_LEAVE_REQUEST,
        leave_active=True,
        leave_start_date="2026-06-20",
        leave_end_date="2026-06-22",
        leave_days=3.0,
    ),
    StepExpect(
        2,
        "আজকে ধানমন্ডি থেকে বনানী গিয়েছি ১৮০ টাকা।",
        note="Expense: travel 180 BDT",
        intent=INTENT_EXPENSE_CLAIM,
        expense_items_min=1,
        expense_amounts=[180.0],
    ),
    StepExpect(
        3,
        "ছুটির শেষ তারিখ ২২ জুন না, ২৩ জুন হবে।",
        note="Leave end date correction 22→23 June",
        leave_end_date="2026-06-23",
        leave_days=4.0,
    ),
    StepExpect(
        4,
        "react কি?",
        should_work=False,
        note="Out-of-scope general knowledge",
        out_of_scope=True,
        body_not_contains=["React", "JavaScript", "library"],
    ),
    StepExpect(
        5,
        "maternity leave policy কী?",
        note="Policy query — KB miss → not-found",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        6,
        "ছুটির কারণ পারিবারিক অনুষ্ঠান।",
        note="Leave reason: family event",
        leave_reason_contains="পারিবারিক",
    ),
    StepExpect(
        7,
        "বনানী থেকে গুলশান ৪৫ টাকা।",
        note="Expense: travel 45 BDT",
        intent=INTENT_EXPENSE_CLAIM,
        expense_amounts=[180.0, 45.0],
    ),
    StepExpect(
        8,
        "আমার active leave draft দেখাও।",
        note="Show pending leave draft",
        body_contains=["ছুটি", "জুন"],
    ),
    StepExpect(
        9,
        "আমার active expense draft দেখাও।",
        note="Show pending expense draft",
        body_contains=["180", "45"],
    ),
    StepExpect(
        10,
        "প্রথম expense টা ১৮০ না, ২০০ টাকা হবে।",
        note="Expense correction first line 180→200",
        expense_amounts=[200.0, 45.0],
    ),
    StepExpect(
        11,
        "docker কি?",
        should_work=False,
        out_of_scope=True,
        body_not_contains=["Docker", "container"],
    ),
    StepExpect(
        12,
        "কোম্পানির overtime policy কী?",
        note="Policy query — overtime",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        13,
        "আজকে দুপুরে লাঞ্চ ১৬০ টাকা।",
        note="Expense: lunch 160",
        intent=INTENT_EXPENSE_CLAIM,
        expense_amounts=[200.0, 45.0, 160.0],
    ),
    StepExpect(
        14,
        "দ্বিতীয় expense entry delete করো।",
        note="Delete 2nd expense — must ask confirmation first",
        awaiting_delete_confirmation=True,
        expense_amounts=[200.0, 45.0, 160.0],
        body_contains=["মুছ", "হ্যাঁ", "নম্বর"],
    ),
    StepExpect(
        15,
        "হ্যাঁ delete করো।",
        note="Confirm delete 2nd expense (45)",
        expense_amounts=[200.0, 160.0],
    ),
    StepExpect(
        16,
        "leave report submit করার আগে দেখাও।",
        note="Pre-submit leave review",
        body_contains=["জুন", "ছুটি"],
    ),
    StepExpect(
        17,
        "expense report submit করার আগে দেখাও।",
        note="Pre-submit expense review",
        body_contains=["200", "160"],
    ),
    StepExpect(
        18,
        "leave submit করো।",
        note="Leave submit → confirmation gate (not direct submit)",
        awaiting_leave_confirmation=True,
        body_contains=["চেক", "ঠিক"],
        leave_submitted=False,
    ),
    StepExpect(
        19,
        "হ্যাঁ leave ঠিক আছে submit করো।",
        note="Confirm leave submit",
        leave_submitted=True,
        leave_active=False,
    ),
    StepExpect(
        20,
        "আবার ২০ জুনের জন্য ছুটি চাই।",
        note="Duplicate leave on Jun 20 — ask which session to continue",
    ),
    StepExpect(
        21,
        "আজকে কফি ৮০ টাকা।",
        note="Expense: coffee 80",
        intent=INTENT_EXPENSE_CLAIM,
        expense_amounts=[200.0, 160.0, 80.0],
    ),
    StepExpect(
        22,
        "expense summary দেখাও।",
        note="Updated expense summary",
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        outcome="INFORMATIONAL",
        body_contains=["200", "80"],
    ),
    StepExpect(
        23,
        "kubernetes কি?",
        should_work=False,
        out_of_scope=True,
        body_not_contains=["Kubernetes", "container orchestration"],
    ),
    StepExpect(
        24,
        "expense submit করো।",
        note="Expense submit → confirmation gate",
        body_contains=["চেক", "ঠিক"],
    ),
    StepExpect(
        25,
        "সব ঠিক আছে submit করো।",
        note="Confirm expense submit",
        custom=lambda _r, wf: _assert_expense_submitted(wf),
    ),
    StepExpect(
        26,
        "leave summary দেখাও।",
        note="After leave submit — submitted summary or no active leave",
        body_contains=["জমা", "সারাংশ"],
    ),
    StepExpect(
        27,
        "expense summary দেখাও।",
        note="After expense submit — submitted summary or no active expense",
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        outcome="INFORMATIONAL",
    ),
    StepExpect(
        28,
        "work from home policy কী?",
        note="Policy query — WFH",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        29,
        "cancel expense।",
        note="Post-submit cancel expense — should refuse or explain no active draft",
        body_contains=["নেই", "জমা", "expense"],
    ),
    StepExpect(
        30,
        "cancel leave।",
        note="Post-submit cancel leave — should refuse or explain already submitted",
        body_contains=["জমা", "cancel"],
    ),
]


def _assert_expense_submitted(wf: dict[str, Any]) -> None:
    block = read_expense_block(wf)
    items = block.get("items") or []
    history = list(wf.get("expense_submissions_history") or [])
    last = dict(wf.get("expense_last_submission") or {})
    submitted = bool(last.get("reference_id")) or bool(history)
    if items and block.get("active"):
        raise AssertionError("expense still has active draft after submit")
    if not submitted:
        raise AssertionError("expected expense submission record in workflow state")


def _assert_duplicate_leave_prompt(body: str) -> None:
    """Step 20: must mention prior leave on same date and offer continue choice."""
    low = body.lower()
    has_date = "২০" in body or "20" in low or "জুন" in body
    has_prior = any(
        w in low
        for w in (
            "আগে",
            "আগের",
            "ইতিমধ্যে",
            "already",
            "open",
            "session",
            "continue",
            "চালিয়ে",
            "নতুন",
        )
    )
    if not has_date:
        raise AssertionError("duplicate leave reply should mention the date (Jun 20)")
    if not has_prior:
        raise AssertionError(
            "duplicate leave reply should mention prior session / continue choice"
        )


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

    if step.leave_submitted is not None:
        locked = is_leave_submission_locked(wf)
        if locked != step.leave_submitted:
            failures.append(f"leave_submitted: expected {step.leave_submitted}, got {locked}")

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

    if step.leave_end_date:
        got = str(draft.get("end_date") or "")
        if got != step.leave_end_date:
            failures.append(f"leave end_date: expected {step.leave_end_date}, got {got}")

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

    if step.awaiting_leave_confirmation is not None:
        awaiting = is_awaiting_leave_confirmation(wf)
        if awaiting != step.awaiting_leave_confirmation:
            failures.append(
                f"awaiting_leave_confirmation: expected {step.awaiting_leave_confirmation}, got {awaiting}"
            )

    if step.awaiting_delete_confirmation is not None:
        has_confirm_prompt = any(
            w in body.lower() or w in body
            for w in ("নিশ্চিত", "confirm", "delete", "মুছ", "বাদ", "sure")
        )
        if step.awaiting_delete_confirmation and not has_confirm_prompt:
            failures.append("delete should ask confirmation before removing line")

    if step.id == 20:
        try:
            _assert_duplicate_leave_prompt(body)
        except AssertionError as exc:
            failures.append(str(exc))

    for needle in step.body_contains:
        if needle.lower() not in body.lower() and needle not in body:
            failures.append(f"body should contain {needle!r}")

    for needle in step.body_not_contains:
        if needle.lower() in body.lower():
            failures.append(f"body should not contain {needle!r}")

    if step.custom and step.id != 20:
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
def test_scenario_30_messages_e2e(scenario_env: ChatOrchestrator):
    """Run full 30-message chain; collect per-step failures for diagnosis."""
    orch = scenario_env
    sid: str | None = None
    all_failures: list[str] = []

    for step in SCENARIO_STEPS:
        result = orch.run_chat(
            company_id=COMPANY_ID,
            message=step.message,
            session_id=sid,
            employee_id=EMP,
            trace_id=f"s30-{step.id:02d}",
        )
        sid = result["_session_id"]
        session = orch.memory.get_or_create_session(
            company_id=COMPANY_ID, employee_id=EMP, session_id=sid
        )
        wf = dict(session.workflow_state or {})
        failures = _assert_step(step, result, wf)
        if failures:
            snippet = re.sub(r"\s+", " ", ((result.get("response") or {}).get("message") or ""))[:200]
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
def test_scenario_30_messages_individual(step: StepExpect, scenario_env: ChatOrchestrator):
    """Parametrized isolation — runs full chain up to each step then asserts."""
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
            trace_id=f"iso30-{prior.id:02d}",
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
