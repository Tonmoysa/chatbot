import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from unittest.mock import patch
from django.conf import settings

from chat.services.leave.duplicate_choice import is_duplicate_leave_choice_pending
from chat.services.leave_fsm import is_leave_in_progress
from chat.services.orchestrator import ChatOrchestrator
from tests.test_scenario_40_messages import (
    COMPANY_ID,
    EMP,
    SCENARIO_STEPS,
    _patch_dates,
    _patch_no_llm,
    _patch_policy_rag_miss,
    _patch_polish_passthrough,
)


class MP:
    def setattr(self, target, val):
        patch(target, val).start()


mp = MP()
_patch_dates(mp)
_patch_no_llm(mp)
_patch_policy_rag_miss(mp)
_patch_polish_passthrough(mp)
patch("chat.services.orchestrator.conversational_reply", lambda **_k: "x").start()
settings.KB_RAG_ENABLED = True

orch = ChatOrchestrator()
sid = None
emp = f"{EMP}-d17"
for step in SCENARIO_STEPS:
    if step.id > 17:
        break
    result = orch.run_chat(
        company_id=COMPANY_ID,
        message=step.message,
        session_id=sid,
        employee_id=emp,
        trace_id=f"d-{step.id:02d}",
    )
    sid = result["_session_id"]
    session = orch.memory.get_or_create_session(
        company_id=COMPANY_ID, employee_id=emp, session_id=sid
    )
    wf = dict(session.workflow_state or {})
    if step.id >= 16:
        print(
            f"step {step.id}: dup_pending={is_duplicate_leave_choice_pending(wf)} "
            f"leave_active={is_leave_in_progress(wf)} intent={result.get('intent')}"
        )
