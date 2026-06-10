"""Leave workflow v2 — sick/annual/unpaid, multi-day auto full, half period."""

from chat.services.leave.normalization import (
    parse_half_day_period_answer,
    parse_wizard_leave_type_answer,
)
from chat.services.leave.workflow_schema import get_leave_workflow_schema
from chat.services.leave_draft_utils import apply_multi_day_scope_default
from chat.services.leave_slots import (
    SLOT_HALF_PERIOD,
    SLOT_LEAVE_TYPE,
    SLOT_SCOPE,
    get_missing_slots,
)


def test_parse_wizard_leave_types():
    assert parse_wizard_leave_type_answer("sick leave") == "sick"
    assert parse_wizard_leave_type_answer("annual") == "annual"
    assert parse_wizard_leave_type_answer("leave without pay") == "unpaid"


def test_parse_half_day_period():
    assert parse_half_day_period_answer("first half") == "first"
    assert parse_half_day_period_answer("দ্বিতীয় অর্ধ") == "second"


def test_multi_day_auto_full_skips_scope():
    draft = {
        "leave_type": "sick",
        "days": 3,
        "reason": "onek osusto",
    }
    apply_multi_day_scope_default(draft)
    missing = get_missing_slots(draft)
    assert SLOT_SCOPE not in missing
    assert draft.get("day_scope") == "full"


def test_half_day_needs_period():
    draft = {
        "leave_type": "annual",
        "day_scope": "half",
        "start_date": "2026-06-10",
        "end_date": "2026-06-10",
    }
    missing = get_missing_slots(draft)
    assert SLOT_HALF_PERIOD in missing


def test_missing_leave_type_asked():
    draft = {"day_scope": "full", "start_date": "2026-06-10"}
    missing = get_missing_slots(draft)
    assert missing[0] == SLOT_LEAVE_TYPE


def test_reason_optional_skip():
    schema = get_leave_workflow_schema()
    draft = {
        "leave_type": "annual",
        "day_scope": "full",
        "start_date": "2026-06-10",
        "end_date": "2026-06-10",
        "_reason_skipped": True,
    }
    assert schema.reason_satisfied(draft)
    missing = schema.missing_fields(draft)
    assert "reason" not in missing
