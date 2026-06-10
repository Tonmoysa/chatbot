"""
End-to-end regression: 40-message Bengali leave + expense + policy chat scenario.

Maps to the user's numbered transcript (July 10 leave → expense lifecycle with
category/route prompts, leave submit defer, duplicate leave + new draft Jul 11,
expense delete/submit, post-submit guards, blocked edits).
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
EMP = "scenario-40-emp"
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
    expense_pending_step: str | None = None
    body_contains: list[str] = field(default_factory=list)
    body_not_contains: list[str] = field(default_factory=list)
    custom: Callable[[dict[str, Any], dict[str, Any]], None] | None = None


SCENARIO_STEPS: list[StepExpect] = [
    StepExpect(
        1,
        "আগামী ১০ জুলাই একদিনের ছুটি নিতে চাই।",
        note="Start leave draft — July 10, 1 day",
        intent=INTENT_LEAVE_REQUEST,
        leave_active=True,
        leave_start_date="2026-07-10",
        leave_days=1.0,
    ),
    StepExpect(
        2,
        "annual leave। কারণ ব্যক্তিগত কাজ।",
        note="Leave reason: personal work + annual leave type",
        leave_reason_contains="ব্যক্তিগত",
    ),
    StepExpect(
        3,
        "আজকে মিরপুর থেকে ফার্মগেট বাসে ৮০ টাকা খরচ হয়েছে।",
        note="Expense: bus Mirpur→Farmgate 80 BDT (category prompt if unclear)",
        intent=INTENT_EXPENSE_CLAIM,
        expense_items_min=1,
        custom=lambda _r, wf: _assert_expense_has_amount(wf, 80.0),
    ),
    StepExpect(
        4,
        "ফার্মগেট থেকে কারওয়ান বাজার ৩০ টাকা।",
        note="Expense: Farmgate→Karwan Bazar 30 BDT",
        intent=INTENT_EXPENSE_CLAIM,
        expense_items_min=2,
        custom=lambda _r, wf: _assert_expense_amounts_include(wf, [80.0, 30.0]),
    ),
    StepExpect(
        5,
        "আমার leave summary দেখাও।",
        note="Active leave draft summary",
        body_contains=["ছুটি", "জুলাই"],
    ),
    StepExpect(
        6,
        "আমার expense summary দেখাও।",
        note="Active expense draft summary",
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        outcome="INFORMATIONAL",
        body_contains=["80", "30"],
    ),
    StepExpect(
        7,
        "company bonus policy কী?",
        note="Policy query — company bonus",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        8,
        "python কি?",
        should_work=False,
        note="Out-of-scope general knowledge",
        out_of_scope=True,
        body_not_contains=["Python is", "programming language"],
    ),
    StepExpect(
        9,
        "বাস ভাড়া ৮০ টাকা না, ১২০ টাকা হবে।",
        note="Expense correction first line 80→120",
        expense_amounts=[120.0, 30.0],
    ),
    StepExpect(
        10,
        "আরেকটা নাস্তা ৫০ টাকা।",
        note="Expense: snack 50 BDT",
        intent=INTENT_EXPENSE_CLAIM,
        expense_amounts=[120.0, 30.0, 50.0],
    ),
    StepExpect(
        11,
        "শেষ expense টা ৫০ না, ৭০ টাকা হবে।",
        note="Expense correction last line 50→70",
        expense_amounts=[120.0, 30.0, 70.0],
    ),
    StepExpect(
        12,
        "leave submit করো।",
        note="Leave submit → confirmation gate",
        awaiting_leave_confirmation=True,
        body_contains=["চেক", "ঠিক"],
        leave_submitted=False,
    ),
    StepExpect(
        13,
        "না, আগে leave summary দেখাও।",
        note="Decline submit — show leave summary instead",
        awaiting_leave_confirmation=True,
        leave_submitted=False,
        body_contains=["ছুটি", "জুলাই"],
    ),
    StepExpect(
        14,
        "leave summary দেখাও।",
        note="Updated leave summary",
        body_contains=["জুলাই", "ব্যক্তিগত"],
    ),
    StepExpect(
        15,
        "সব ঠিক আছে, leave submit করো।",
        note="Confirm leave submit — July 10",
        leave_submitted=True,
        leave_active=False,
        leave_start_date="2026-07-10",
    ),
    StepExpect(
        16,
        "আবার ১০ জুলাই ছুটি চাই।",
        note="Duplicate leave Jul 10 — detect prior submission",
        custom=lambda r, _wf: _assert_duplicate_leave_prompt(
            (r.get("response") or {}).get("message") or ""
        ),
    ),
    StepExpect(
        17,
        "নতুন leave request খুলে দাও।",
        note="New leave draft — do not touch submitted Jul 10 leave",
        leave_active=True,
        custom=lambda _r, wf: _assert_july10_still_submitted(wf),
    ),
    StepExpect(
        18,
        "১১ জুলাই।",
        note="New leave draft date Jul 11",
        leave_start_date="2026-07-11",
        leave_days=1.0,
    ),
    StepExpect(
        19,
        "কারণ চিকিৎসা।",
        note="New leave reason: medical",
        leave_reason_contains="চিকিৎসা",
    ),
    StepExpect(
        20,
        "expense summary দেখাও।",
        note="Active expense draft still present",
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        outcome="INFORMATIONAL",
        body_contains=["120", "30", "70"],
        custom=lambda _r, wf: _assert_expense_draft_has_amounts(wf, [120.0, 30.0, 70.0]),
    ),
    StepExpect(
        21,
        "প্রথম expense delete করো।",
        note="Delete 1st expense — must ask confirmation",
        awaiting_delete_confirmation=True,
        expense_amounts=[120.0, 30.0, 70.0],
        body_contains=["মুছ", "হ্যাঁ"],
    ),
    StepExpect(
        22,
        "হ্যাঁ delete করো।",
        note="Confirm delete first expense (120)",
        expense_amounts=[30.0, 70.0],
    ),
    StepExpect(
        23,
        "expense summary দেখাও।",
        note="Updated expense summary after delete",
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        outcome="INFORMATIONAL",
        expense_items_exact=2,
        body_contains=["30", "70"],
    ),
    StepExpect(
        24,
        "work from home policy কী?",
        note="Policy query — WFH",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        25,
        "আজকে লাঞ্চ ১৮০ টাকা।",
        note="Expense: lunch 180 BDT",
        intent=INTENT_EXPENSE_CLAIM,
        expense_amounts=[30.0, 70.0, 180.0],
    ),
    StepExpect(
        26,
        "দ্বিতীয় expense টা delete করো।",
        note="Delete 2nd expense — must ask confirmation",
        awaiting_delete_confirmation=True,
        expense_amounts=[30.0, 70.0, 180.0],
        body_contains=["মুছ", "হ্যাঁ"],
    ),
    StepExpect(
        27,
        "না, delete করো না।",
        note="Cancel delete — all expenses remain",
        expense_amounts=[30.0, 70.0, 180.0],
    ),
    StepExpect(
        28,
        "expense summary দেখাও।",
        note="All current expenses still present",
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        outcome="INFORMATIONAL",
        expense_items_exact=3,
        body_contains=["30", "70", "180"],
    ),
    StepExpect(
        29,
        "expense submit করো।",
        note="Expense submit → confirmation gate",
        body_contains=["চেক", "ঠিক"],
        custom=lambda _r, wf: _assert_expense_not_submitted(wf),
    ),
    StepExpect(
        30,
        "expense টা আরেকবার দেখাও।",
        note="Review expense without submitting",
        body_contains=["30", "180"],
        custom=lambda _r, wf: _assert_expense_not_submitted(wf),
    ),
    StepExpect(
        31,
        "সব ঠিক আছে submit করো।",
        note="Confirm expense submit",
        custom=lambda _r, wf: _assert_expense_submitted(wf),
    ),
    StepExpect(
        32,
        "expense summary দেখাও।",
        note="After expense submit — submitted summary or no active expense",
        intent=INTENT_EXPENSE_DAY_SUMMARY,
        outcome="INFORMATIONAL",
        body_contains=["জমা", "সারাংশ"],
    ),
    StepExpect(
        33,
        "leave summary দেখাও।",
        note="Active Jul 11 leave draft summary",
        body_contains=["জুলাই", "চিকিৎসা"],
        leave_active=True,
    ),
    StepExpect(
        34,
        "javascript কি?",
        should_work=False,
        note="Out-of-scope general knowledge",
        out_of_scope=True,
        body_not_contains=["JavaScript", "programming"],
    ),
    StepExpect(
        35,
        "leave submit করো।",
        note="Leave submit Jul 11 → confirmation gate",
        awaiting_leave_confirmation=True,
        body_contains=["চেক", "ঠিক"],
    ),
    StepExpect(
        36,
        "হ্যাঁ submit করো।",
        note="Confirm second leave submit",
        leave_submitted=True,
        leave_active=False,
        leave_start_date="2026-07-11",
    ),
    StepExpect(
        37,
        "cancel leave।",
        note="Post-submit cancel leave — should refuse",
        body_contains=["জমা", "cancel"],
    ),
    StepExpect(
        38,
        "cancel expense।",
        note="Post-submit cancel expense — should refuse",
        body_contains=["নেই", "জমা"],
    ),
    StepExpect(
        39,
        "reimbursement policy কী?",
        note="Policy query — reimbursement",
        policy_query=True,
        policy_not_found=True,
        intent=INTENT_HR_POLICY,
    ),
    StepExpect(
        40,
        "প্রথম expense ১২০ টাকা ছিল, ২০০ করে দাও।",
        note="Modify submitted expense — should block (no active draft)",
        body_contains=["submit", "যায় না", "edit"],
    ),
]


def _expense_amounts(wf: dict[str, Any]) -> list[float]:
    block = read_expense_block(wf)
    items = block.get("items") or []
    return [float(i.get("amount") or 0) for i in items]


def _assert_expense_has_amount(wf: dict[str, Any], amount: float) -> None:
    amounts = _expense_amounts(wf)
    if amount not in amounts:
        raise AssertionError(f"expected expense with amount {amount}, got {amounts}")


def _assert_expense_amounts_include(wf: dict[str, Any], expected: list[float]) -> None:
    amounts = sorted(_expense_amounts(wf), reverse=True)
    for a in expected:
        if a not in amounts:
            raise AssertionError(f"expected amounts to include {a}, got {amounts}")


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
        raise AssertionError("expected active expense draft (submit was declined or pending)")
    last = dict(wf.get("expense_last_submission") or {})
    if last.get("reference_id") and not items:
        pass  # ok if prior batch submitted but new draft exists


def _expense_draft_amounts(wf: dict[str, Any]) -> list[float]:
    block = read_expense_block(wf)
    items = block.get("items") or []
    if items:
        return [float(i.get("amount") or 0) for i in items]
    suspended = (wf.get("suspended_expense") or {}).get("expense_request") or wf.get(
        "suspended_expense"
    ) or {}
    if isinstance(suspended, dict):
        items = suspended.get("items") or []
        return [float(i.get("amount") or 0) for i in items]
    return []


def _assert_expense_draft_has_amounts(
    wf: dict[str, Any], expected: list[float]
) -> None:
    amounts = sorted(_expense_draft_amounts(wf), reverse=True)
    want = sorted(expected, reverse=True)
    if amounts != want:
        raise AssertionError(f"expense draft amounts: expected {want}, got {amounts}")


def _assert_july10_still_submitted(wf: dict[str, Any]) -> None:
    from chat.services.leave_fsm import read_leave_last_submission

    last = read_leave_last_submission(wf)
    draft = dict(last.get("draft") or {})
    start = str(draft.get("start_date") or "")
    if start != "2026-07-10":
        raise AssertionError(f"submitted Jul 10 leave should remain, got start_date={start!r}")
    if not last.get("submission_id"):
        raise AssertionError("expected Jul 10 leave submission record")


def _assert_duplicate_leave_prompt(body: str) -> None:
    low = body.lower()
    has_date = "১০" in body or "10" in low or "জুলাই" in body
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
            "জমা",
        )
    )
    if not has_date:
        raise AssertionError("duplicate leave reply should mention the date (Jul 10)")
    if not has_prior:
        raise AssertionError(
            "duplicate leave reply should mention prior session / continue choice"
        )


def _leave_draft(wf: dict[str, Any]) -> dict[str, Any]:
    if has_suspended_leave(wf):
        return dict((wf.get("suspended_leave") or {}).get("draft") or {})
    return dict(read_leave_state(wf).get("draft") or {})


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

    if step.expense_pending_step is not None:
        block = read_expense_block(wf)
        got = str(block.get("pending_step") or "")
        if got != step.expense_pending_step:
            failures.append(f"expense pending_step: expected {step.expense_pending_step!r}, got {got!r}")

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
def test_scenario_40_messages_e2e(scenario_env: ChatOrchestrator):
    """Run full 40-message chain; collect per-step failures for diagnosis."""
    orch = scenario_env
    sid: str | None = None
    all_failures: list[str] = []

    for step in SCENARIO_STEPS:
        result = orch.run_chat(
            company_id=COMPANY_ID,
            message=step.message,
            session_id=sid,
            employee_id=EMP,
            trace_id=f"s40-{step.id:02d}",
        )
        sid = result["_session_id"]
        session = orch.memory.get_or_create_session(
            company_id=COMPANY_ID, employee_id=EMP, session_id=sid
        )
        wf = dict(session.workflow_state or {})
        failures = _assert_step(step, result, wf)
        if failures:
            snippet = re.sub(r"\s+", " ", ((result.get("response") or {}).get("message") or ""))[:220]
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
def test_scenario_40_messages_individual(step: StepExpect, scenario_env: ChatOrchestrator):
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
            trace_id=f"iso40-{prior.id:02d}",
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
