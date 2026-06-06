"""Leave draft normalization — synonyms and sick inference."""

from chat.services.leave.normalization import (
    infer_leave_type_from_text,
    normalize_leave_draft,
    normalize_payment_category,
    text_has_sick_signal,
)
from chat.services.leave_workflow import _apply_slots_from_message
from chat.services.leave_slots import get_missing_slots
from chat.services.leave_slot_extraction import extract_reason_from_message


PET_BETHA_MSG = "mar pet betha tai kalke amar leave lagbe full day paid"


def test_text_has_sick_signal():
    assert text_has_sick_signal("mar pet betha")
    assert text_has_sick_signal("পেট ব্যথা")
    assert not text_has_sick_signal("family wedding")


def test_normalize_payment_synonyms():
    assert normalize_payment_category("without pay") == "lwop"
    assert normalize_payment_category("salary cut leave") == "lwop"
    assert normalize_payment_category("paid") == "paid"


def test_infer_sick_from_reason():
    assert infer_leave_type_from_text("pet betha") == "sick"
    assert infer_leave_type_from_text("family program") is None


def test_extract_reason_pet_betha_tai():
    reason = extract_reason_from_message(PET_BETHA_MSG)
    assert reason
    assert "pet betha" in reason.lower()


def test_compound_pet_betha_prefills_reason_and_skips_reason_slot():
    draft: dict = {}
    _apply_slots_from_message(draft, PET_BETHA_MSG, {})
    assert draft.get("start_date")
    assert draft.get("leave_payment_category") == "paid"
    assert draft.get("day_scope") == "full"
    assert draft.get("reason")
    assert "pet betha" in str(draft.get("reason")).lower()
    assert draft.get("leave_type") == "sick"
    missing = get_missing_slots(draft)
    assert "reason" not in missing


def test_normalize_leave_draft_idempotent():
    draft = {
        "leave_payment_category": "unpaid",
        "day_scope": "half",
        "reason": "pet betha",
        "start_date": "2026-06-10",
    }
    normalize_leave_draft(draft)
    assert draft["leave_payment_category"] == "lwop"
    assert draft["day_scope"] == "half"
    assert draft["leave_type"] == "sick"
