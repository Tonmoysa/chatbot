"""
End-to-end regression: 36-message Bengali leave + expense + policy chat scenario.

Maps to the user's numbered transcript (next-week leave → Jun 25–26, expense lifecycle,
delete cancel, submit flows, duplicate leave + continue, post-submit summary/cancel,
new sick leave, blocked expense modify after submit).
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
EMP = "scenario-36-emp"
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
    expense_items_exact: int | None = None
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
        "আগামী সপ্তাহে দুই দিনের ছুটি নিতে চাই।",
        note="Start leave draft — dates missing → clarification",
        intent=INTENT_LEAVE_REQUEST,
        leave_active=True,
        body_contains=["তারিখ", "কখন"],
    ),
    StepExpect(
        2,
        "২৫ জুন থেকে ২৬ জুন।",
        note="Fill leave dates Jun 25–26 (2 days)",
        leave_start_date="2026-06-25",
        leave_end_date="2026-06-26",
        leave_days=2.0,
    ),
    StepExpect(
        3,
        "আজকে উত্তরা থেকে আগারগাঁও ৭০ টাকা।",
        note="Expense: travel 70 BDT",
        intent=INTENT_EXPENSE_CLAIM,
        expense_items_min=1,
        expense_amounts=[70.0],
    ),
    StepExpect(
        4,
        "spring boot কি?",
        should_work=False,
        note="Out-of-scope general knowledge",
        out_of_scope=True,
        body_not_contains=["Spring Boot", "Java", "framework"],
    ),
    StepExpect(
        5,
        "annual leave। ছুটির কারণ পারিবারিক কাজ।",
        note="Leave reason: family work + annual leave type",
        leave_reason_contains="পারিবারিক",
    ),
    StepExpect(
        6,
        "আগারগাঁও থেকে ফার্মগেট ৩০ টাকা।",
        note="Expense: travel 30 BDT",
        intent=INTENT_EXPENSE_CLAIM,
        expense_amounts=[70.0, 30.0],
    ),
    StepExpect(
        7,
        "leave summary দেখাও।",
        note="Current leave draft summary",
        body_contains=["ছুটি", "জুন"],
    ),
    StepExpect(
        8,
        "expense summary দেখাও।",
        note="Current expense draft summary",
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        outcome="INFORMATIONAL",
        body_contains=["70", "30"],
    ),
    StepExpect(
        9,
        "transportation allowance policy কী?",
        note="Policy query — transportation allowance",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        10,
        "প্রথম expense টা ৭০ না, ৯০ টাকা হবে।",
        note="Expense correction first line 70→90",
        expense_amounts=[90.0, 30.0],
    ),
    StepExpect(
        11,
        "দ্বিতীয় expense delete করো।",
        note="Delete 2nd expense — must ask confirmation",
        awaiting_delete_confirmation=True,
        expense_amounts=[90.0, 30.0],
        body_contains=["মুছ", "হ্যাঁ"],
    ),
    StepExpect(
        12,
        "না delete করো না।",
        note="Cancel delete — both expenses remain",
        expense_amounts=[90.0, 30.0],
    ),
    StepExpect(
        13,
        "expense summary দেখাও।",
        note="Both expenses still present",
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        outcome="INFORMATIONAL",
        expense_items_exact=2,
        body_contains=["90", "30"],
    ),
    StepExpect(
        14,
        "leave submit করো।",
        note="Leave submit → confirmation gate",
        awaiting_leave_confirmation=True,
        body_contains=["চেক", "ঠিক"],
        leave_submitted=False,
    ),
    StepExpect(
        15,
        "হ্যাঁ সব ঠিক আছে।",
        note="Confirm leave submit",
        leave_submitted=True,
        leave_active=False,
    ),
    StepExpect(
        16,
        "আবার ২৫ জুন থেকে ২৬ জুন ছুটি চাই।",
        note="Duplicate leave Jun 25–26 — detect prior session",
        custom=lambda _r, _wf: None,  # checked via _assert_duplicate_leave_prompt in _assert_step
    ),
    StepExpect(
        17,
        "আগের leave continue করব।",
        note="Continue prior submitted leave — no new draft",
        leave_active=False,
        body_contains=["জমা", "25"],
    ),
    StepExpect(
        18,
        "lunch ১২০ টাকা।",
        note="Expense: lunch 120",
        intent=INTENT_EXPENSE_CLAIM,
        expense_amounts=[90.0, 30.0, 120.0],
    ),
    StepExpect(
        19,
        "expense report দেখাও।",
        note="Updated expense review",
        body_contains=["90", "120"],
    ),
    StepExpect(
        20,
        "annual leave carry forward policy কী?",
        note="Policy query — carry forward",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        21,
        "nodejs কি?",
        should_work=False,
        note="Out-of-scope general knowledge",
        out_of_scope=True,
        body_not_contains=["Node.js", "JavaScript", "runtime"],
    ),
    StepExpect(
        22,
        "expense submit করো।",
        note="Expense submit → confirmation gate",
        body_contains=["চেক", "ঠিক"],
    ),
    StepExpect(
        23,
        "না, আগে summary দেখাও।",
        note="Decline submit — show summary instead",
        body_contains=["90", "120"],
        custom=lambda _r, wf: _assert_expense_not_submitted(wf),
    ),
    StepExpect(
        24,
        "expense summary দেখাও।",
        note="Latest draft summary (still active)",
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        outcome="INFORMATIONAL",
        expense_items_exact=3,
    ),
    StepExpect(
        25,
        "সব ঠিক আছে, expense submit করো।",
        note="Confirm expense submit",
        custom=lambda _r, wf: _assert_expense_submitted(wf),
    ),
    StepExpect(
        26,
        "expense summary দেখাও।",
        note="After expense submit — submitted summary or no active expense",
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        outcome="INFORMATIONAL",
        body_contains=["জমা", "সারাংশ"],
    ),
    StepExpect(
        27,
        "leave summary দেখাও।",
        note="After leave submit — submitted summary or no active leave",
        body_contains=["জমা", "সারাংশ"],
    ),
    StepExpect(
        28,
        "cancel leave।",
        note="Post-submit cancel leave — should refuse",
        body_contains=["জমা", "cancel"],
    ),
    StepExpect(
        29,
        "cancel expense।",
        note="Post-submit cancel expense — should refuse",
        body_contains=["নেই", "জমা"],
    ),
    StepExpect(
        30,
        "sick leave policy কী?",
        note="Policy query — sick leave",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        31,
        "আগামীকাল ছুটি চাই।",
        note="New leave draft for tomorrow (Jun 11) — no conflict",
        intent=INTENT_LEAVE_REQUEST,
        leave_active=True,
        leave_start_date="2026-06-11",
    ),
    StepExpect(
        32,
        "কারণ অসুস্থতা।",
        note="New leave reason: sick",
        leave_reason_contains="অসুস্থ",
    ),
    StepExpect(
        33,
        "leave summary দেখাও।",
        note="New active leave draft summary",
        body_contains=["অসুস্থ", "ছুটি"],
    ),
    StepExpect(
        34,
        "প্রথম expense ৯০ টাকা ছিল, ১৫০ টাকা করে দাও।",
        note="Modify submitted expense — should block (no active expense)",
        body_contains=["submit", "যায় না", "edit"],
    ),
    StepExpect(
        35,
        "leave submit করো।",
        note="Leave submit → confirmation gate",
        awaiting_leave_confirmation=True,
        body_contains=["চেক", "ঠিক"],
    ),
    StepExpect(
        36,
        "হ্যাঁ submit করো।",
        note="Confirm second leave submit",
        leave_submitted=True,
        leave_active=False,
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


def _assert_expense_not_submitted(wf: dict[str, Any]) -> None:
    block = read_expense_block(wf)
    items = block.get("items") or []
    if not items or not block.get("active"):
        raise AssertionError("expected active expense draft (submit was declined)")
    last = dict(wf.get("expense_last_submission") or {})
    if last.get("reference_id"):
        raise AssertionError("expense should not be submitted yet")


def _assert_duplicate_leave_prompt(body: str) -> None:
    low = body.lower()
    has_date = "২৫" in body or "25" in low or "জুন" in body
    has_prior = any(
        w in low or w in body
        for w in (
            "আগে",
            "আগের",
            "ইতিমধ্যে",
            "already",
            "session",
            "continue",
            "চালিয়ে",
            "নতুন",
            "duplicate",
        )
    )
    if not has_date:
        raise AssertionError("duplicate leave reply should mention the date (Jun 25–26)")
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

    if step.expense_items_exact is not None:
        n = len(read_expense_block(wf).get("items") or [])
        if n != step.expense_items_exact:
            failures.append(f"expense items: expected exactly {step.expense_items_exact}, got {n}")

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

    if step.id == 16:
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

    if step.custom and step.id != 16:
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
def test_scenario_36_messages_e2e(scenario_env: ChatOrchestrator):
    """Run full 36-message chain; collect per-step failures for diagnosis."""
    orch = scenario_env
    sid: str | None = None
    all_failures: list[str] = []

    for step in SCENARIO_STEPS:
        result = orch.run_chat(
            company_id=COMPANY_ID,
            message=step.message,
            session_id=sid,
            employee_id=EMP,
            trace_id=f"s36-{step.id:02d}",
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
def test_scenario_36_messages_individual(step: StepExpect, scenario_env: ChatOrchestrator):
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
            trace_id=f"iso36-{prior.id:02d}",
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
