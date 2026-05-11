"""
Multi-step paid / LWOP leave collection. LLM extracts hints; readiness and questions are deterministic.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any


LEAVE_PAYMENT_PAID = "paid"
LEAVE_PAYMENT_LWOP = "lwop"

DAY_SCOPE_FULL = "full"
DAY_SCOPE_HALF = "half"

# Sick/medical leaves over this inclusive calendar span generally need proof.
_SICK_DOCUMENT_MIN_SPAN_DAYS = 3

# Same footer on every prompt so the orchestrator can spot leave follow-ups.
_WIZ_FOOTER = "\n\n_(ছুটি ফর্ম — নিচে একটা করে উত্তর দিন)_"

_MESSAGES: dict[str, str] = {
    "leave_payment_category": (
        "প্রথম প্রশ্ন (১/৫) — এই ছুটিটা **বেতনসহ** চান, নাকি **বেতন ছাড়া**?\n\n"
        "• বেতনসহ = আপনার ছুটির ব্যালান্স থেকে কাটবে (লিখতে পারেন: বেতনসহ / paid)\n"
        "• বেতন ছাড়া = বেতন কাটবে না; সাধারণত আগে ম্যানেজার বা HR-এর অনুমোদন লাগে (লিখতে পারেন: বেতন ছাড়া / unpaid)"
        + _WIZ_FOOTER
    ),
    "day_scope": (
        "দ্বিতীয় প্রশ্ন (২/৫) — প্রতিদিন **পুরো দিন** নাকি **হাফ দিন** ছুটি?\n\n"
        "উত্তর: পুরো দিন / হাফ দিন (ইংরেজিতে full বা half লিখলেও চলবে)"
        + _WIZ_FOOTER
    ),
    "leave_dates": (
        "তৃতীয় প্রশ্ন (৩/৫) — **কোন তারিখ(গুলো)** ছুটি চান?\n\n"
        "• এক দিন হলে: কাল / আগামীকাল / বা তারিখ (যেমন 2026-05-12)\n"
        "• একাধিক দিন হলে: 2026-05-12 থেকে 2026-05-14 — এভাবে লিখুন"
        + _WIZ_FOOTER
    ),
    "reason": (
        "চতুর্থ প্রশ্ন (৪/৫) — **কেন** ছুটি লাগছে? এক লাইনেই হবে।\n\n"
        "উদাহরণ: পরিবারের কাজ, অসুস্থ, ভ্রমণ…"
        + _WIZ_FOOTER
    ),
    "supporting_document": (
        "পঞ্চম প্রশ্ন (৫/৫) — এই ধরনের ছুটিতে সাধারণত **ডাক্তারের চিট বা কাগজপত্র** লাগে।\n\n"
        "ফাইল আপলোড করুন, অথবা লেখাটা এখানে পেস্ট করুন। এখন দিতে পারছেন না? শুধু **skip** লিখুন — তখন ম্যানেজার/HR দেখে নেবে।"
        + _WIZ_FOOTER
    ),
}

_DATE_VALIDATION_MESSAGES: dict[str, str] = {
    "BAD_RANGE": (
        "তারিখটা একটু গুলিয়ে গেছে — **শেষ তারিখ** যেন **প্রথম তারিখের আগে** না হয়। আবার ঠিক করে লিখুন।"
        + _WIZ_FOOTER
    ),
    "IN_PAST": (
        "আজকের **আগের** তারিখে ছুটি দেওয়া যাবে না। আজ বা পরের দিন দিন, অথবা পুরনো তারিখ লাগলে HR-এর সাথে কথা বলুন।"
        + _WIZ_FOOTER
    ),
}


def clone_workflow_state(state: dict[str, Any] | None) -> dict[str, Any]:
    raw = dict(state or {})
    return raw


def is_leave_collecting(workflow_state: dict[str, Any] | None) -> bool:
    lr = (workflow_state or {}).get("leave_request") or {}
    return bool(lr.get("active"))


def deactivate_leave_session(workflow_state: dict[str, Any]) -> dict[str, Any]:
    wf = clone_workflow_state(workflow_state)
    if "leave_request" in wf:
        wf.pop("leave_request", None)
    return wf


def _today() -> date:
    return date.today()


def _parse_iso(d: Any) -> date | None:
    if not d:
        return None
    s = str(d).strip().split("T")[0]
    try:
        return datetime.fromisoformat(s).date()
    except Exception:
        return None


def _calendar_span_days(draft: dict[str, Any]) -> int:
    s = _parse_iso(draft.get("start_date"))
    e = _parse_iso(draft.get("end_date") or draft.get("start_date"))
    if not s or not e:
        return 0
    return max(0, (e - s).days) + 1


def _effective_leave_bucket(draft: dict[str, Any]) -> str:
    """Broad bucket for sick vs other for document policy."""
    lt = str(draft.get("leave_type") or "").strip().lower()
    reason_l = str(draft.get("reason") or "").lower()
    if lt in {"sick", "medical", "health"}:
        return "sick"
    sickish = ("sick", "ill", "fever", "medical", "doctor", "hospital")
    if any(w in reason_l for w in sickish):
        return "sick"
    return "other"


def supporting_document_needed(draft: dict[str, Any]) -> bool:
    span = _calendar_span_days(draft)
    return _effective_leave_bucket(draft) == "sick" and span >= _SICK_DOCUMENT_MIN_SPAN_DAYS


def _normalize_end_equals_start_if_missing(draft: dict[str, Any]) -> None:
    """If user gave only one day copy to end_date when missing."""
    s = draft.get("start_date")
    if draft.get("end_date"):
        return
    if not s:
        return
    draft["end_date"] = str(s).strip().split("T")[0]


def _infer_payment_category(message: str, draft: dict[str, Any]) -> None:
    low = message.lower().strip()
    if draft.get("leave_payment_category"):
        return

    if re.search(
        r"\b(lwop|leave without pay|without pay|unpaid)\b|\bbezeton\b|\bbezaton\b|\bbina\s+beton\b|\bbez\s+beton\b",
        low,
    ):
        draft["leave_payment_category"] = LEAVE_PAYMENT_LWOP
        return
    if re.search(
        r"\bpaid\b|\bpto\b|\bannual\b|\bcasual\b|\b(?:from|using)\s+(?:my\s+)?balance\b",
        low,
    ):
        if re.search(r"\b(lwop|unpaid)\b", low):
            draft["leave_payment_category"] = LEAVE_PAYMENT_LWOP
        else:
            draft["leave_payment_category"] = LEAVE_PAYMENT_PAID


def _infer_day_scope(message: str, draft: dict[str, Any]) -> None:
    if draft.get("day_scope"):
        return
    low = message.lower()
    if re.search(r"\bhalf\b|half[- ]day|semi|অর্ধ", low):
        draft["day_scope"] = DAY_SCOPE_HALF
        return
    if re.search(r"\bfull\b|whole\s*day|\bwhole\b|সম্পূর্ণ\b", low):
        draft["day_scope"] = DAY_SCOPE_FULL


def _reason_from_message(message: str) -> str | None:
    m = message.strip()
    if len(m) < 4:
        return None
    low = m.lower().strip()

    refusal = ("skip doc", "skip document", "^skip$")
    if re.match(r"^skip$", low.strip()):
        return None

    if any(m.lower().startswith(x) for x in ("paid", "lwop", "unpaid", "full ", "half ")):
        if len(m.split()) <= 6 and not any(c.isdigit() for c in m):
            maybe_structural = False
            for kw in ("family", "sick", "travel", "wedding", "medical"):
                if kw in low:
                    maybe_structural = True
            if not maybe_structural:
                return None
    stripped = low
    stripped = re.sub(
        r"^(paid|lwop|unpaid|annual|pto|whole day|full day|half day|half|full)\b[\s,:-]*",
        "",
        stripped,
        flags=re.I,
    ).strip(" ,.:;-")
    if len(stripped) < 10:
        if not re.search(r"(family|travel|sick|wedding|planned|annual|pto|urgent)", stripped):
            return None
        if len(stripped) < 3:
            return None
        return stripped
    # Longer textual reply — treat whole message minus leading structural fluff
    remainder = stripped
    remainder = remainder.strip(",.:;-")
    return remainder.strip()[:2000]


def merge_extractor_entities(draft: dict[str, Any], entities: dict[str, Any]) -> None:
    """Apply LLM / rule extractor fields into mutable draft."""

    dt = entities.get("document_text")
    if dt and str(dt).strip():
        draft["document_text"] = str(dt).strip()

    for key in (
        "start_date",
        "end_date",
        "date",
        "days",
        "leave_type",
        "reason",
        "leave_payment_category",
        "day_scope",
    ):
        v = entities.get(key)
        if v is None or v == "":
            continue

        if key == "leave_payment_category":
            s = str(v).strip().lower()
            if s in {"paid", "pto", "annual", "casual", "balance"}:
                draft["leave_payment_category"] = LEAVE_PAYMENT_PAID
            elif s in {"lwop", "unpaid", "without_pay", "leave_without_pay"}:
                draft["leave_payment_category"] = LEAVE_PAYMENT_LWOP
            continue

        if key == "day_scope":
            s = str(v).strip().lower()
            if s in {"half", "half_day", "half-day"}:
                draft["day_scope"] = DAY_SCOPE_HALF
            elif s in {"full", "full_day", "full-day"}:
                draft["day_scope"] = DAY_SCOPE_FULL
            continue

        if key == "date" and not draft.get("start_date"):
            draft["start_date"] = str(v).split("T")[0]
            continue

        draft[key] = v

    rs = entities.get("description")
    if rs and isinstance(rs, str) and rs.strip() and not draft.get("reason"):
        draft["reason"] = rs.strip()[:2000]


def _validate_dates(draft: dict[str, Any]) -> tuple[bool, str | None]:
    s = _parse_iso(draft.get("start_date"))
    e = _parse_iso(draft.get("end_date") or draft.get("start_date"))
    if not s:
        return True, None
    if not e:
        e = s
        draft.setdefault("end_date", s.isoformat())
    today = _today()
    if s < today:
        return False, "IN_PAST"
    if e < s:
        return False, "BAD_RANGE"
    return True, None


def _first_missing_step(draft: dict[str, Any]) -> str | None:
    if not draft.get("leave_payment_category"):
        return "leave_payment_category"
    if not draft.get("day_scope"):
        return "day_scope"
    if not draft.get("start_date"):
        return "leave_dates"
    _normalize_end_equals_start_if_missing(draft)
    if not draft.get("start_date"):
        return "leave_dates"
    ok, err = _validate_dates(draft)
    if not ok and err:
        return "leave_dates"
    if not str(draft.get("reason") or "").strip():
        return "reason"
    if supporting_document_needed(draft):
        if draft.get("supporting_document_waived"):
            return None
        doc = str(draft.get("document_text") or "").strip()
        if not doc:
            return "supporting_document"
    return None


def _question_for_step(step: str, *, date_error: str | None) -> str:
    if step == "leave_dates" and date_error:
        return _DATE_VALIDATION_MESSAGES.get(date_error, _MESSAGES["leave_dates"])
    return _MESSAGES.get(step, "Please provide the missing information to continue your leave request.")


def build_merged_entities_for_engine(draft: dict[str, Any]) -> dict[str, Any]:
    """Flat entity dict for DecisionEngine + CRM."""

    out: dict[str, Any] = {
        "start_date": draft.get("start_date"),
        "end_date": draft.get("end_date") or draft.get("start_date"),
        "date": draft.get("start_date"),
        "days": draft.get("days"),
        "leave_type": draft.get("leave_type"),
        "reason": draft.get("reason"),
        "leave_payment_category": draft.get("leave_payment_category"),
        "day_scope": draft.get("day_scope") or DAY_SCOPE_FULL,
        "document_text": draft.get("document_text"),
    }
    if draft.get("supporting_document_waived"):
        out["supporting_document_waived"] = True
    return {k: v for k, v in out.items() if v is not None and v != ""}


def process_leave_turn(
    *,
    workflow_state: dict[str, Any],
    message: str,
    entities: dict[str, Any],
) -> dict[str, Any]:
    """
    Returns:
      workflow_state: updated session JSON
      merged_entities: snapshot for logging + decision path
      complete: whether decision engine should run final leave rules
      question: assistant prompt if not complete
    """
    wf = clone_workflow_state(workflow_state)
    block = wf.setdefault("leave_request", {})
    block["active"] = True
    draft = dict(block.get("draft") or {})
    draft["_last_user_message"] = message

    # Wizard steps must not trust LLM-invented payment / scope flags on the first utterance —
    # they are parsed only from the actual user wording below.
    safe_entities = dict(entities)
    safe_entities.pop("leave_payment_category", None)
    safe_entities.pop("day_scope", None)
    safe_entities.pop("reason", None)
    safe_entities.pop("description", None)
    merge_extractor_entities(draft, safe_entities)
    _infer_payment_category(message, draft)
    _infer_day_scope(message, draft)

    _normalize_end_equals_start_if_missing(draft)

    if _first_missing_step(draft) == "reason":
        inferred = _reason_from_message(message)
        if inferred:
            draft["reason"] = str(inferred).strip()[:2000]

    date_err: str | None = None
    if draft.get("start_date"):
        ok, code = _validate_dates(draft)
        if not ok:
            date_err = code
            if code == "BAD_RANGE":
                draft.pop("end_date", None)
            if code == "IN_PAST":
                draft.pop("start_date", None)
                draft.pop("end_date", None)
                draft.pop("date", None)

    miss0 = _first_missing_step(draft)
    if miss0 == "supporting_document" and message.strip().lower() == "skip":
        draft["supporting_document_waived"] = True

    missing = _first_missing_step(draft)
    if missing == "leave_dates" and date_err:
        block["draft"] = draft
        wf["leave_request"] = block
        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(draft),
            "complete": False,
            "question": _question_for_step("leave_dates", date_error=date_err),
        }

    if missing:
        block["draft"] = draft
        wf["leave_request"] = block
        return {
            "workflow_state": wf,
            "merged_entities": build_merged_entities_for_engine(draft),
            "complete": False,
            "question": _question_for_step(missing, date_error=None),
        }

    merged = build_merged_entities_for_engine(draft)
    wf = deactivate_leave_session(wf)
    return {
        "workflow_state": wf,
        "merged_entities": merged,
        "complete": True,
        "question": None,
    }
