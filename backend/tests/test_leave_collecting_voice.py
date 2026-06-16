"""Leave collecting: Bengali voice scope + reason correction."""

import pytest

from chat.services.leave.normalization import parse_day_scope_answer
from chat.services.leave.reason_value import extract_reason_replacement
from chat.services.leave_slots import SLOT_REASON, SLOT_SCOPE, SLOT_DATES, get_missing_slots
from chat.services.leave_fsm import read_leave_state
from chat.services.leave_workflow import process_leave_turn

REASON_SWAP_BN = "শরীর খারাপ হবে না ফ্যামিলি প্রবলেম হবে"


@pytest.mark.parametrize(
    "message",
    [
        "ফুল ডে",
        "ফুল দিন",
        "পুরো দিন",
        "full day",
        "ful day",
    ],
)
def test_parse_day_scope_bengali_voice(message: str) -> None:
    assert parse_day_scope_answer(message) == "full"


@pytest.mark.parametrize(
    "message",
    [
        "হাফ দিন",
        "হাফ ডে",
        "half day",
    ],
)
def test_parse_day_scope_half_voice(message: str) -> None:
    assert parse_day_scope_answer(message) == "half"


def test_extract_reason_replacement_negation_bn() -> None:
    val = extract_reason_replacement(REASON_SWAP_BN)
    assert val
    assert "ফ্যামিলি" in val or "family" in val.lower()


def test_process_leave_turn_scope_bn_voice() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": SLOT_SCOPE,
        "draft": {
            "start_date": "2026-06-09",
            "end_date": "2026-06-09",
            "leave_payment_category": "paid",
            "leave_type": "sick",
            "reason": "শরীর খারাপ",
        },
    }
    pack = process_leave_turn(
        workflow_state=wf,
        message="ফুল ডে",
        entities={},
        company_id="company-a",
    )
    draft = read_leave_state(pack["workflow_state"]).get("draft") or {}
    assert draft.get("day_scope") == "full"
    assert "day_scope" not in get_missing_slots(draft)


def test_process_leave_turn_reason_swap_while_scope_pending() -> None:
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": SLOT_SCOPE,
        "draft": {
            "start_date": "2026-06-09",
            "end_date": "2026-06-09",
            "leave_payment_category": "paid",
            "leave_type": "sick",
            "reason": "শরীর খারাপ",
        },
    }
    pack = process_leave_turn(
        workflow_state=wf,
        message=REASON_SWAP_BN,
        entities={},
        company_id="company-a",
    )
    draft = read_leave_state(pack["workflow_state"]).get("draft") or {}
    reason = str(draft.get("reason") or "")
    assert "ফ্যামিলি" in reason or "family" in reason.lower()
    assert "শরীর খারাপ" not in reason or "ফ্যামিলি" in reason


def test_collecting_switch_field_to_date_edit():
    """While on reason step, user can jump to date edit."""
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": SLOT_REASON,
        "draft": {
            "start_date": "2026-06-09",
            "end_date": "2026-06-09",
            "leave_payment_category": "paid",
            "leave_type": "sick",
            "day_scope": "full",
        },
    }
    pack = process_leave_turn(
        workflow_state=wf,
        message="date change koro 2026-06-12",
        entities={},
        company_id="company-a",
    )
    st = read_leave_state(pack["workflow_state"])
    draft = st.get("draft") or {}
    assert draft.get("start_date") == "2026-06-12"


def test_collecting_edit_menu_opens_on_edit_koro():
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": SLOT_REASON,
        "draft": {
            "start_date": "2026-06-09",
            "end_date": "2026-06-09",
            "leave_payment_category": "paid",
            "leave_type": "sick",
            "day_scope": "full",
        },
    }
    pack = process_leave_turn(
        workflow_state=wf,
        message="edit koro",
        entities={},
        company_id="company-a",
    )
    st = read_leave_state(pack["workflow_state"])
    assert st.get("step") == "edit_menu"
    q = pack.get("question") or ""
    assert "date" in q.lower() or "তারিখ" in q


def test_compound_leave_message_sets_full_scope_from_bn_voice() -> None:
    """Initial compound utterance with ফুল ডে must not leave scope missing."""
    wf = {
        "active_flow": "leave",
        "status": "active",
        "step": None,
        "draft": {},
    }
    msg = "আমার শরীর খারাপ তাই কালকে লিভ লাগবে ফুল্লিপেড এন্ড ফোল্ডে ফুল ডে"
    pack = process_leave_turn(
        workflow_state=wf,
        message=msg,
        entities={},
        company_id="company-a",
    )
    draft = read_leave_state(pack["workflow_state"]).get("draft") or {}
    assert draft.get("day_scope") == "full"
