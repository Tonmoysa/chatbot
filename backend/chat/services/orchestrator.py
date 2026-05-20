import uuid
import re
from datetime import date
from typing import Any

from chat.constants import (
    INTENT_APPROVAL_ESCALATION,
    INTENT_ATTENDANCE_CORRECTION,
    INTENT_EXPENSE_CLAIM,
    INTENT_EXPENSE_DAY_SUMMARY,
    INTENT_EXPENSE_STATUS,
    INTENT_HR_POLICY,
    INTENT_LEAVE_BALANCE,
    INTENT_LEAVE_REQUEST,
    INTENT_REQUEST_STATUS,
    INTENT_UNKNOWN,
    INTENT_WFH_REQUEST,
)
from chat.services.crm.base import CRMError
from chat.services.crm.factory import get_crm_adapter
from chat.services.decision_engine import DecisionEngine
from chat.services.entity_extractor import EntityExtractor
from chat.services.expense_incurred_date import infer_expense_incurred_date_iso
from chat.services.intent_detector import (
    IntentDetector,
    _is_cancel_form_request,
    _is_fresh_start_greeting,
    _looks_like_chitchat,
    _message_answers_wizard_step,
    _strong_hr_policy,
)
from chat.services.leave_days import compute_requested_leave_days
from chat.services.leave_workflow import (
    deactivate_leave_session,
    is_leave_collecting,
    is_leave_paused,
    pause_leave_session,
    pending_question,
    pending_step,
    process_leave_turn,
    resume_leave_session,
)
from chat.services.expense_workflow import (
    deactivate_expense_session,
    is_expense_collecting,
    process_expense_turn,
)
from chat.services.conversational import conversational_reply
from chat.services.memory_store import ConversationMemoryStore
from chat.services.observability import log_step
from chat.services.response_formatter import build_user_message
# from chat.services.rules_handbook import (
#     answer_rules_query,
#     is_rules_query,
#     wants_full_handbook,
# )  # disabled — policy text comes only from knowledge-base RAG
from chat.services.policy_intent_helpers import is_rules_query
from chat.services.translator import (
    detect_user_language,
    is_translation_request,
    translate_text,
)
from knowledge_base.services.rag_pipeline import (
    hr_policy_not_found_message,
    try_hr_policy_rag,
)


_RULES_FOOTER_EN_SECTION = (
    "_(Answers come from your uploaded policies; ask using the policy title or topic.)_"
)
_RULES_FOOTER_EN_FULL = (
    "_(Ask about a specific policy by name or section — e.g. \"attendance policy\" or "
    "\"leave policy\" — so retrieval can match your knowledge base.)_"
)
_RULES_FOOTER_BN_SECTION = (
    "_(উত্তর আপনার আপলোড করা পলিসি থেকে আসে; পলিসির নাম বা বিষয় লিখে জিজ্ঞাসা করুন।)_"
)
_RULES_FOOTER_BN_FULL = (
    "_(নির্দিষ্ট পলিসির নাম বা বিষয় লিখে জিজ্ঞাসা করুন — যেমন \"উপস্থিতি পলিসি\" বা "
    "\"ছুটির পলিসি\" — যাতে নলেজ বেসে মিলে।)_"
)


def _canonical_leave_wizard_token(message: str) -> bool:
    """
    Short replies that should stay in the leave wizard even when the step
    guard or LLM intent mis-fires (e.g. "paid" while dates are pending).
    """
    t = (message or "").strip().lower()
    if not t or len(t) > 48:
        return False
    if re.match(
        r"^(paid|unpaid|lwop|full|half|sick|casual|annual|emergency|maternity|paternity)(\s+day)?s?$",
        t,
    ):
        return True
    return bool(re.search(r"(বেতনসহ|বেতন\s*ছাড়া)", message or ""))


def _rules_footer(*, mode: str, lang: str) -> str:
    if lang == "bn":
        return _RULES_FOOTER_BN_SECTION if mode == "section" else _RULES_FOOTER_BN_FULL
    return _RULES_FOOTER_EN_SECTION if mode == "section" else _RULES_FOOTER_EN_FULL


class ChatOrchestrator:
    """
    Central pipeline controller.
    User Input → Intent → Entities → Context merge → Decision → CRM → Formatter.
    """

    def __init__(self) -> None:
        self.memory = ConversationMemoryStore()
        self.intents = IntentDetector()
        self.entities = EntityExtractor()
        self.engine = DecisionEngine()
        self.crm = get_crm_adapter()

    def run_chat(
        self,
        *,
        message: str,
        session_id: str | None,
        company_id: str,
        employee_id: str,
        trace_id: str,
        document_text: str | None = None,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        session = self.memory.get_or_create_session(
            company_id=company_id,
            employee_id=employee_id,
            session_id=session_id or "",
        )
        context_lines = self.memory.recent_context_lines(session)

        # Translation follow-up — if the user is asking to translate the previous
        # assistant turn, do that directly so the message never falls into the
        # generic "I didn't understand" greeting. Workflow state is preserved
        # so any in-progress wizard simply resumes on the next turn.
        translate_to = is_translation_request(message)
        if translate_to:
            prev_assistant = self._last_assistant_text(context_lines)
            if prev_assistant:
                log_step(
                    trace_id,
                    "translation_request",
                    {"target_lang": translate_to, "source_chars": len(prev_assistant)},
                )
                translated, ok = translate_text(
                    prev_assistant,
                    target_lang=translate_to,
                    trace_id=trace_id,
                )
                if ok:
                    msg = translated
                    status_str = "success"
                else:
                    msg = (
                        "এই মুহূর্তে অনুবাদ চালু হচ্ছে না — একটু পরে আবার চেষ্টা করুন। "
                        "নিচে আগের উত্তরটি আবার পাঠানো হলো:\n\n" + prev_assistant
                        if translate_to == "bn"
                        else (
                            "Translation is briefly unavailable — please try again. "
                            "Re-posting the previous answer below:\n\n" + prev_assistant
                        )
                    )
                    status_str = "degraded"
                self.memory.append(session, "user", message)
                self.memory.append(session, "assistant", msg)
                return {
                    "trace_id": trace_id,
                    "intent": "HR_POLICY",
                    "entities": {"translation_target_lang": translate_to},
                    "decision": {
                        "outcome": "INFORMATIONAL",
                        "reason": "Translated the previous assistant turn.",
                        "rules_applied": ["TRANSLATION_FOLLOWUP"],
                    },
                    "response": {
                        "message": msg,
                        "status": status_str,
                        "request_id": "",
                    },
                    "status": "success",
                    "_session_id": session.session_id,
                }

        log_step(
            trace_id,
            "intent_detection_start",
            {"user_message": message, "session_id": session.session_id},
        )

        intent_result = self.intents.detect(message, trace_id)
        intent = intent_result["intent"]

        wf_state = getattr(session, "workflow_state", None) or {}
        if is_leave_paused(wf_state) and _wants_resume_leave(message):
            session.workflow_state = resume_leave_session(wf_state)
            session.save(update_fields=["workflow_state", "updated_at"])
            wf_state = session.workflow_state or {}
            log_step(trace_id, "leave_wizard_resumed", {})

        low_msg = (message or "").lower()
        # Balance probe must catch Banglish phrasing too (kotodin / koydin / kondin / baki etc.)
        # so mid-wizard balance questions are not fed back into the wizard as date answers.
        balance_probe = bool(
            re.search(
                r"\b(balance|remaining|left|pto|how\s+many\s+days|vacation\s+left|baki|baaki)\b",
                low_msg,
            )
            or re.search(r"(ছুটি\s*কত|কত\s*দিন|কয়\s*দিন|কতদিন|কয়দিন)", message or "")
            or re.search(r"\b(koto|koy|kon)\s*din\b", low_msg)
            or re.search(r"\b(kotodin|koydin|kondin)\b", low_msg)
        )
        # A stand-alone greeting ("hi", "hello", "oi", "salam", "নমস্কার") OR
        # an explicit cancel signal ("cancel", "lagbe na", "বাদ দাও") means the
        # user is starting fresh / dropping the in-progress form. Close any
        # leave wizard BEFORE the wizard-routing block so the user doesn't get
        # a reminder appended to their greeting. We ALSO force the intent to
        # UNKNOWN here — otherwise an LLM that mis-classified "hi" as
        # LEAVE_REQUEST would re-open the wizard inside process_leave_turn().
        wizard_dismissed_reason: str | None = None
        is_greeting_now = _is_fresh_start_greeting(message)
        is_cancel_now = _is_cancel_form_request(message)
        if (is_leave_collecting(wf_state) or is_expense_collecting(wf_state)) and (
            is_greeting_now or is_cancel_now
        ):
            if is_leave_collecting(wf_state):
                wf_state = deactivate_leave_session(wf_state)
            if is_expense_collecting(wf_state):
                wf_state = deactivate_expense_session(wf_state)
            session.workflow_state = wf_state
            session.save(update_fields=["workflow_state", "updated_at"])
            wf_state = session.workflow_state or {}
            wizard_dismissed_reason = "greeting" if is_greeting_now else "cancel"
            intent = INTENT_UNKNOWN
            intent_result = {
                **intent_result,
                "intent": INTENT_UNKNOWN,
                "source": (intent_result.get("source") or "intent")
                + f"+wizard_dismissed_{wizard_dismissed_reason}",
            }
            log_step(
                trace_id,
                "leave_wizard_dismissed",
                {"reason": wizard_dismissed_reason},
            )

        if is_leave_collecting(wf_state):
            # Do not cancel on EXPENSE_STATUS / REQUEST_STATUS alone — short replies like
            # "paid" are often misclassified and would wipe the leave draft.
            hard_switch = intent in (
                INTENT_EXPENSE_CLAIM,
                INTENT_EXPENSE_DAY_SUMMARY,
                INTENT_WFH_REQUEST,
                INTENT_ATTENDANCE_CORRECTION,
                INTENT_APPROVAL_ESCALATION,
            )
            if hard_switch:
                session.workflow_state = deactivate_leave_session(wf_state)
                session.save(update_fields=["workflow_state", "updated_at"])
            elif balance_probe:
                # Heuristic is deterministic; trust it even if the LLM mis-labeled the
                # short Banglish phrasing as LEAVE_REQUEST (the wizard remains active in
                # workflow_state so the user resumes after the balance answer).
                intent = INTENT_LEAVE_BALANCE
                intent_result = {
                    **intent_result,
                    "intent": INTENT_LEAVE_BALANCE,
                    "source": (intent_result.get("source") or "intent") + "+balance_probe",
                }
            elif _looks_like_chitchat(message, strict=True):
                # LEGACY: previously UNKNOWN + conversational; that broke the leave
                # state machine. Keep the user in the workflow — process_leave_turn
                # will re-prompt only missing slots (no generic LLM).
                intent = INTENT_LEAVE_REQUEST
                intent_result = {
                    **intent_result,
                    "intent": INTENT_LEAVE_REQUEST,
                    "source": (intent_result.get("source") or "intent") + "+chitchat_in_wizard",
                }
            elif _strong_hr_policy(message):
                # Explicit policy question — pause wizard (keep draft) so the policy
                # answer is not followed by the leave form reminder.
                session.workflow_state = pause_leave_session(wf_state)
                session.save(update_fields=["workflow_state", "updated_at"])
                wf_state = session.workflow_state or {}
                intent = INTENT_HR_POLICY
                intent_result = {
                    **intent_result,
                    "intent": INTENT_HR_POLICY,
                    "source": (intent_result.get("source") or "intent") + "+rules_pause_wizard",
                }
                log_step(trace_id, "leave_wizard_paused_for_policy", {})
            elif (
                not _message_answers_wizard_step(message, pending_step(wf_state))
                and not _canonical_leave_wizard_token(message)
            ):
                # Do not drop to UNKNOWN / generic LLM while leave is active — that
                # desynced session.draft from the user's follow-ups ("date lost").
                intent = INTENT_LEAVE_REQUEST
                intent_result = {
                    **intent_result,
                    "intent": INTENT_LEAVE_REQUEST,
                    "source": (intent_result.get("source") or "intent") + "+leave_wizard",
                }
            else:
                intent = INTENT_LEAVE_REQUEST
                intent_result = {
                    **intent_result,
                    "intent": INTENT_LEAVE_REQUEST,
                    "source": (intent_result.get("source") or "intent") + "+leave_wizard",
                }
        elif is_expense_collecting(wf_state):
            if intent in (
                INTENT_LEAVE_REQUEST,
                INTENT_WFH_REQUEST,
                INTENT_ATTENDANCE_CORRECTION,
                INTENT_APPROVAL_ESCALATION,
            ):
                session.workflow_state = deactivate_expense_session(wf_state)
                session.save(update_fields=["workflow_state", "updated_at"])
                wf_state = session.workflow_state or {}
            elif intent in (INTENT_EXPENSE_DAY_SUMMARY, INTENT_EXPENSE_STATUS):
                pass
            elif _looks_like_chitchat(message, strict=True):
                intent = INTENT_EXPENSE_CLAIM
                intent_result = {
                    **intent_result,
                    "intent": INTENT_EXPENSE_CLAIM,
                    "source": (intent_result.get("source") or "intent") + "+chitchat_in_expense_wizard",
                }
            elif _strong_hr_policy(message):
                session.workflow_state = deactivate_expense_session(wf_state)
                session.save(update_fields=["workflow_state", "updated_at"])
                wf_state = session.workflow_state or {}
                intent = INTENT_HR_POLICY
                intent_result = {
                    **intent_result,
                    "intent": INTENT_HR_POLICY,
                    "source": (intent_result.get("source") or "intent") + "+rules_exit_expense_wizard",
                }
            else:
                intent = INTENT_EXPENSE_CLAIM
                intent_result = {
                    **intent_result,
                    "intent": INTENT_EXPENSE_CLAIM,
                    "source": (intent_result.get("source") or "intent") + "+expense_wizard",
                }
        else:
            # Skip follow-up inference when the user just dismissed the wizard
            # (greeting / cancel) — otherwise the recent assistant turn (which
            # contained "ছুটি ফর্ম" or "Step …") would drag the intent back to
            # LEAVE_REQUEST and re-open the form.
            if wizard_dismissed_reason is None:
                forced_intent = self._infer_followup_intent(context_lines, message)
                if forced_intent:
                    intent = forced_intent
                    intent_result = {"intent": forced_intent, "confidence": 1.0, "source": "followup"}

        # Belt-and-suspenders: never finish intent detection with UNKNOWN while a
        # leave collection is active (LLM glitches, edge regex gaps).
        wf_gate = getattr(session, "workflow_state", None) or {}
        if (
            wizard_dismissed_reason is None
            and is_leave_collecting(wf_gate)
            and intent == INTENT_UNKNOWN
        ):
            intent = INTENT_LEAVE_REQUEST
            intent_result = {
                **intent_result,
                "intent": INTENT_LEAVE_REQUEST,
                "source": (intent_result.get("source") or "intent") + "+leave_workflow_lock",
            }
        if (
            wizard_dismissed_reason is None
            and is_expense_collecting(wf_gate)
            and intent == INTENT_UNKNOWN
        ):
            intent = INTENT_EXPENSE_CLAIM
            intent_result = {
                **intent_result,
                "intent": INTENT_EXPENSE_CLAIM,
                "source": (intent_result.get("source") or "intent") + "+expense_workflow_lock",
            }

        log_step(trace_id, "intent_detection_done", {"intent": intent})

        log_step(trace_id, "entity_extraction_start", {})
        entity_result = self.entities.extract(
            message, intent, context_lines, trace_id
        )
        entities = entity_result.get("entities") or {}
        if document_text:
            # Carry document text into the rule engine (LLM must not decide outcomes).
            entities["document_text"] = document_text
        log_step(trace_id, "entity_extraction_done", {"keys": list(entities.keys())})

        lv_pack: dict[str, Any] = {}
        exp_pack: dict[str, Any] = {}
        leave_collecting_blocked = False
        expense_collecting_blocked = False
        if intent == INTENT_LEAVE_REQUEST:
            lv_pack = process_leave_turn(
                workflow_state=getattr(session, "workflow_state", None) or {},
                message=message,
                entities=dict(entities),
                company_id=company_id,
            )
            session.workflow_state = lv_pack["workflow_state"]
            session.save(update_fields=["workflow_state", "updated_at"])
            merged = lv_pack["merged_entities"] or {}
            entities.clear()
            entities.update(merged)
            leave_collecting_blocked = not bool(lv_pack.get("complete"))
        log_step(
            trace_id,
            "leave_workflow_gate",
            {"blocked": leave_collecting_blocked, "intent": intent},
        )

        if intent == INTENT_EXPENSE_CLAIM or is_expense_collecting(
            getattr(session, "workflow_state", None) or {}
        ):
            wf_exp = getattr(session, "workflow_state", None) or {}
            day_logged = 0.0
            inc_hint = infer_expense_incurred_date_iso(
                message=message, hints=entities, today=date.today()
            )
            try:
                br = self.crm.get_expense_day_breakdown(
                    company_id=company_id,
                    employee_id=employee_id,
                    session_id=session.session_id,
                    incurred_date_iso=inc_hint,
                )
                day_logged = float(br.get("expense_day_logged_total") or 0)
            except Exception:
                day_logged = 0.0
            from chat.constants import EXPENSE_DAY_CAP_BDT

            exp_pack = process_expense_turn(
                workflow_state=wf_exp,
                message=message,
                company_id=company_id,
                employee_id=employee_id,
                session_id=session.session_id,
                day_logged_total=day_logged,
                daily_cap=float(EXPENSE_DAY_CAP_BDT),
            )
            session.workflow_state = exp_pack["workflow_state"]
            session.save(update_fields=["workflow_state", "updated_at"])
            expense_collecting_blocked = not bool(exp_pack.get("complete"))
            if exp_pack.get("items"):
                entities["expense_items"] = list(exp_pack["items"])
            if exp_pack.get("incurred_date_iso"):
                entities["expense_incurred_date"] = exp_pack["incurred_date_iso"]
            if exp_pack.get("warnings"):
                entities["expense_warnings"] = list(exp_pack["warnings"])
        log_step(
            trace_id,
            "expense_workflow_gate",
            {"blocked": expense_collecting_blocked, "intent": intent},
        )

        crm_context: dict[str, Any] = {}
        crm_payload: dict[str, Any] = {}
        status = "success"
        request_id = ""
        decision: dict[str, Any] = {}
        msg = ""
        rstatus = ""
        sources_out: list[dict[str, Any]] = []
        rag_unknown_hit = False

        try:
            if intent in (
                INTENT_LEAVE_BALANCE,
                INTENT_LEAVE_REQUEST,
                INTENT_WFH_REQUEST,
            ):
                bal = self.crm.get_leave_balance(
                    company_id=company_id,
                    employee_id=employee_id,
                    session_id=session.session_id,
                )
                crm_context.update(bal)
                crm_context["company_id"] = company_id
                crm_context["employee_id"] = employee_id
                if intent == INTENT_LEAVE_REQUEST and not leave_collecting_blocked:
                    lr_pack = self.crm.list_employee_leave_requests(
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                    )
                    crm_context["existing_leave_requests"] = list(
                        lr_pack.get("leave_requests") or []
                    )
                if intent == INTENT_LEAVE_BALANCE:
                    crm_payload.update(bal)

            if intent in (INTENT_EXPENSE_STATUS, INTENT_REQUEST_STATUS):
                rid = entities.get("request_id")
                if rid:
                    st = self.crm.get_request_status(
                        str(rid),
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                    )
                    crm_payload.update(st)
                else:
                    crm_payload["detail"] = "Missing request_id for status lookup."

            # LEGACY / OBSOLETE: per-turn single-amount day cap for auto-approve path.
            # Enterprise workflow uses expense_validation warnings + SUBMITTED outcome.
            if intent == INTENT_EXPENSE_CLAIM and not expense_collecting_blocked:
                if not entities.get("expense_workflow_submit"):
                    inc_iso = (entities.get("expense_incurred_date") or "").strip() or infer_expense_incurred_date_iso(
                        message=message, hints=entities, today=date.today()
                    )
                    day_tot = self.crm.get_expense_day_approved_total(
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                        incurred_date_iso=inc_iso,
                    )
                    crm_context.update(day_tot)

            if intent == INTENT_HR_POLICY:
                dept = entities.get("department")
                rag = try_hr_policy_rag(
                    message,
                    trace_id,
                    company_id=company_id,
                    department=str(dept).strip() if dept else None,
                )
                if rag and rag.get("hit"):
                    crm_payload["rules_answer"] = rag.get("text") or ""
                    crm_payload["rules_mode"] = rag.get("mode") or "rag"
                    crm_payload["rules_matched_sections"] = []
                    crm_payload["rag_sources"] = rag.get("sources") or []
                    sources_out = list(crm_payload["rag_sources"])
                    log_step(
                        trace_id,
                        "rag_hr_policy_hit",
                        {"sources": len(sources_out)},
                    )
                else:
                    # `rules_handbook.py` is not used for runtime answers (RAG / KB only).
                    # if wants_full_handbook(message):
                    #     rules_pack = answer_rules_query(message)
                    #     ...
                    crm_payload["rules_answer"] = hr_policy_not_found_message()
                    crm_payload["rules_mode"] = "rag_no_hit"
                    crm_payload["rules_matched_sections"] = []
                    log_step(trace_id, "rag_no_hit_kb_only_no_static_handbook", {})

            if intent == INTENT_EXPENSE_DAY_SUMMARY:
                inc_iso = (entities.get("expense_incurred_date") or "").strip() or infer_expense_incurred_date_iso(
                    message=message, hints=entities, today=date.today()
                )
                breakdown = self.crm.get_expense_day_breakdown(
                    company_id=company_id,
                    employee_id=employee_id,
                    session_id=session.session_id,
                    incurred_date_iso=inc_iso,
                )
                crm_payload.update(breakdown)
                crm_context.update(breakdown)

            if leave_collecting_blocked:
                decision = {
                    "outcome": "NEEDS_CLARIFICATION",
                    "reason": lv_pack.get("question")
                    or "আর একটু জানতে হবে — উপরের প্রশ্নের উত্তরটা নিচে লিখে পাঠান।",
                    "rules_applied": ["LEAVE_WORKFLOW_COLLECTING"],
                }
            elif expense_collecting_blocked:
                decision = {
                    "outcome": "NEEDS_CLARIFICATION",
                    "reason": exp_pack.get("question")
                    or "আজকের খরচের বিস্তারিত লিখুন (যেমন: lunch 100, bus 50)।",
                    "rules_applied": ["EXPENSE_WORKFLOW_COLLECTING"],
                }
            else:
                if intent == INTENT_EXPENSE_CLAIM and exp_pack.get("submitted"):
                    entities["expense_workflow_submit"] = True
                decision = self.engine.evaluate(
                    intent=intent, entities=entities, crm_context=crm_context
                )
            log_step(trace_id, "decision", {"outcome": decision.get("outcome")})

            dedup_request_id = self._recent_duplicate_request_id(
                session=session,
                context_lines=context_lines,
                intent=intent,
                entities=entities,
                decision=decision,
                user_message=message,
            )

            if dedup_request_id:
                request_id = dedup_request_id
                crm_payload.update({"request_id": dedup_request_id, "_deduped": True})
            elif self._should_mutate_crm(intent, decision):
                if (
                    intent == INTENT_LEAVE_REQUEST
                    and decision.get("outcome") == "SUBMITTED"
                    and not leave_collecting_blocked
                ):
                    from chat.services.leave_payload import build_leave_submission_payload
                    from chat.services.leave_submission_service import submit_leave_request

                    pl = build_leave_submission_payload(
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                        entities=dict(entities),
                        decision=dict(decision),
                        trace_id=trace_id,
                    )
                    sub = submit_leave_request(pl)
                    crm_payload["leave_submission"] = sub
                    entities["leave_submission_payload"] = pl
                if (
                    intent == INTENT_EXPENSE_CLAIM
                    and decision.get("outcome") == "SUBMITTED"
                    and not expense_collecting_blocked
                ):
                    from chat.services.expense_payload import build_expense_submission_payload
                    from chat.services.expense_submission_service import submit_expense_request

                    pl_exp = build_expense_submission_payload(
                        company_id=company_id,
                        employee_id=employee_id,
                        session_id=session.session_id,
                        items=list(entities.get("expense_items") or []),
                        incurred_date_iso=str(entities.get("expense_incurred_date") or ""),
                        trace_id=trace_id,
                        warnings=list(entities.get("expense_warnings") or []),
                    )
                    sub_exp = submit_expense_request(pl_exp)
                    crm_payload["expense_submission"] = sub_exp
                    entities["expense_submission_payload"] = pl_exp
                    request_id = str(sub_exp.get("reference_id") or "")
                crm_entities = dict(entities)
                if (
                    intent == INTENT_EXPENSE_CLAIM
                    and decision.get("outcome") == "SUBMITTED"
                    and crm_entities.get("expense_items")
                ):
                    total = sum(
                        float(x.get("amount") or 0)
                        for x in crm_entities.get("expense_items") or []
                    )
                    crm_entities["amount"] = total
                    crm_entities["expense_line_count"] = len(
                        crm_entities.get("expense_items") or []
                    )
                exec_result = self.crm.create_request(
                    employee_id=employee_id,
                    intent=intent,
                    entities=crm_entities,
                    decision=decision,
                    company_id=company_id,
                    session_id=session.session_id,
                    idempotency_key=idempotency_key,
                )
                crm_rid = str(exec_result.get("request_id") or "")
                if intent == INTENT_EXPENSE_CLAIM and decision.get("outcome") == "SUBMITTED":
                    sub_ref = str(
                        (crm_payload.get("expense_submission") or {}).get("reference_id")
                        or ""
                    )
                    request_id = sub_ref or crm_rid
                else:
                    request_id = crm_rid
                crm_payload.update(exec_result)

            if (
                intent == INTENT_LEAVE_REQUEST
                and request_id
                and not crm_payload.get("_deduped")
                and status == "success"
            ):
                wf_post = getattr(session, "workflow_state", None) or {}
                wf_post["last_leave_fingerprint"] = {
                    "sig": list(self._leave_booking_signature(entities)),
                    "request_id": request_id,
                }
                session.workflow_state = wf_post
                session.save(update_fields=["workflow_state", "updated_at"])

            msg, rstatus = build_user_message(
                intent=intent,
                entities=entities,
                decision=decision,
                crm_payload=crm_payload,
            )

            # Friendly, human-toned LLM fallback for cases where the rules /
            # intent pipeline could not produce a specific answer:
            #   1. The user message did not match any HR intent (UNKNOWN).
            #   2. The user asked about a rule we don't have a section for
            #      (HR_POLICY with rules_mode == "no_match"), but not rag_no_hit
            #      (explicit KB miss copy — do not overwrite with chit-chat).
            # When the LLM is unavailable we keep the existing degraded text
            # so the user always gets *something*.
            used_conversational = False
            if (
                intent == INTENT_UNKNOWN
                and decision.get("outcome") == "NEEDS_CLARIFICATION"
                and (is_rules_query(message) or _strong_hr_policy(message))
            ):
                dept = entities.get("department")
                rag_u = try_hr_policy_rag(
                    message,
                    trace_id,
                    company_id=company_id,
                    department=str(dept).strip() if dept else None,
                )
                if rag_u and rag_u.get("hit"):
                    msg = rag_u.get("text") or msg
                    rstatus = "success"
                    sources_out = list(rag_u.get("sources") or [])
                    rag_unknown_hit = True
                    crm_payload["rules_mode"] = "rag"
                    crm_payload["rules_answer"] = msg
                    log_step(
                        trace_id,
                        "rag_unknown_policy_hit",
                        {"sources": len(sources_out)},
                    )

            wf_for_conv = getattr(session, "workflow_state", None) or {}
            needs_conversational = (
                not rag_unknown_hit
                and not is_leave_collecting(wf_for_conv)
                and not is_expense_collecting(wf_for_conv)
                and (
                    (
                        intent == INTENT_UNKNOWN
                        and decision.get("outcome") == "NEEDS_CLARIFICATION"
                    )
                    or (
                        intent == INTENT_HR_POLICY
                        and crm_payload.get("rules_mode") == "no_match"
                    )
                )
            )
            if needs_conversational:
                log_step(
                    trace_id,
                    "conversational_fallback_start",
                    {"intent": intent, "reason": "no_match_or_unknown"},
                )
                reply = conversational_reply(
                    message=message,
                    context_lines=context_lines,
                    trace_id=trace_id,
                )
                if reply:
                    msg = reply
                    rstatus = "success"
                    used_conversational = True
                    log_step(
                        trace_id,
                        "conversational_fallback_done",
                        {"chars": len(reply)},
                    )

            # Explicit "cancel the form" requests get a clear, deterministic
            # confirmation so the user knows the wizard is gone. We do this
            # AFTER the conversational fallback so an LLM reply never
            # contradicts the cancellation.
            if wizard_dismissed_reason == "cancel":
                user_lang = detect_user_language(message)
                msg = (
                    "ঠিক আছে, ছুটি ফর্মটি বাদ দিলাম। অন্য কিছু লাগলে জানাবেন।"
                    if user_lang == "bn"
                    else "Got it — I've cancelled the leave form. Let me know if you need anything else."
                )
                rstatus = "success"
                used_conversational = True

            # Language-aware reply for matched rules: rules content is authored
            # in English, so if the user wrote in Bangla / Banglish we translate
            # the answer to Bangla. Markdown structure is preserved. The
            # localized footer/hint is appended after translation so it never
            # gets dropped or mistranslated by the LLM.
            if (
                msg
                and not used_conversational
                and (
                    (
                        intent == INTENT_HR_POLICY
                        and crm_payload.get("rules_mode") in ("full", "section", "rag", "rag_no_hit")
                    )
                    or rag_unknown_hit
                )
            ):
                user_lang = detect_user_language(message)
                if user_lang == "bn":
                    log_step(
                        trace_id,
                        "rules_translate",
                        {"target_lang": "bn", "chars": len(msg)},
                    )
                    translated, ok = translate_text(
                        msg, target_lang="bn", trace_id=trace_id
                    )
                    if ok:
                        msg = translated
                rules_mode = str(crm_payload.get("rules_mode") or "")
                if rules_mode in ("full", "section", "rag", "rag_no_hit"):
                    footer_mode = "section" if rules_mode in ("rag", "rag_no_hit") else rules_mode
                    msg = msg.rstrip() + "\n\n" + _rules_footer(
                        mode=footer_mode, lang=user_lang
                    )

            # Side-questions during an active wizard: nudge resume (not for explicit
            # policy lookups — those pause the wizard above).
            if (
                (
                    intent in (INTENT_LEAVE_BALANCE, INTENT_UNKNOWN)
                    or rag_unknown_hit
                )
                and is_leave_collecting(getattr(session, "workflow_state", None) or {})
            ):
                resume = pending_question(getattr(session, "workflow_state", None) or {})
                if resume:
                    msg = f"{msg}\n\n---\n_(ছুটি ফর্ম এখনও চালু আছে — পরের প্রশ্ন:)_\n\n{resume}"

            # Paused leave draft: short hint only (no full wizard step dump).
            wf_after = getattr(session, "workflow_state", None) or {}
            if is_leave_paused(wf_after) and intent == INTENT_HR_POLICY and msg:
                user_lang = detect_user_language(message)
                hint = (
                    "\n\n_(ছুটির খসড়া সংরক্ষিত আছে — চালিয়ে যেতে লিখুন: continue leave)_"
                    if user_lang == "bn"
                    else "\n\n_(Your leave draft is saved — type **continue leave** to resume the form.)_"
                )
                if "continue leave" not in msg.lower() and "ছুটির খসড়া" not in msg:
                    msg = msg.rstrip() + hint

        except CRMError:
            log_step(trace_id, "crm_error", {"error": "CRMError"})
            status = "failed"
            decision = {
                "outcome": "ERROR",
                "reason": "CRM integration error.",
                "rules_applied": ["CRM_FAILURE"],
            }
            msg = "The HR system is temporarily unavailable. Please try again shortly."
            rstatus = "error"
        except Exception as exc:
            log_step(trace_id, "unexpected_error", {"error": type(exc).__name__})
            status = "failed"
            decision = {
                "outcome": "ERROR",
                "reason": "Unexpected processing error.",
                "rules_applied": ["UNHANDLED_EXCEPTION"],
            }
            msg = "Something went wrong processing your request."
            rstatus = "error"

        self.memory.append(session, "user", message)
        self.memory.append(session, "assistant", msg)

        return {
            "trace_id": trace_id,
            "intent": intent,
            "entities": entities,
            "decision": decision,
            "response": {
                "message": msg,
                "status": rstatus if status == "success" else "error",
                "request_id": request_id or str(crm_payload.get("request_id", "") or ""),
            },
            "sources": sources_out,
            "status": status,
            "_session_id": session.session_id,
        }

    @staticmethod
    def _last_assistant_text(context_lines: list[str]) -> str | None:
        """Most recent assistant turn from the rolling conversation context."""
        for line in reversed(context_lines or []):
            if line.startswith("Assistant:"):
                content = line[len("Assistant:"):].strip()
                if content:
                    return content
        return None

    def _infer_followup_intent(self, context_lines: list[str], message: str) -> str | None:
        """
        Heuristic: if the assistant just asked for missing fields, treat short user replies
        (dates/days/etc) as a follow-up for the same workflow instead of re-classifying intent.
        """
        if not context_lines:
            return None
        last_assistant = ""
        for line in reversed(context_lines):
            if line.startswith("Assistant:"):
                last_assistant = line[len("Assistant:") :].strip()
                break
        if not last_assistant:
            return None

        msg = (message or "").strip()
        # if user replies with just a date-like token, it's very likely a continuation
        is_dateish = bool(
            re.search(r"\b\d{4}-\d{1,2}-\d{1,2}\b", msg)
            or re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", msg)
            or re.search(r"\b\d{1,2}-\d{1,2}-\d{2,4}\b", msg)
        )

        # Leave flow follow-up (legacy copy + wizard copy)
        if (
            "Leave dates or duration required" in last_assistant
            or "ছুটি ফর্ম" in last_assistant
            or "ছুটি আবেদন" in last_assistant
            or "**Step " in last_assistant
            or "Step 3 of 5" in last_assistant
        ):
            if len(msg) <= 180 or is_dateish:
                # Do not hijack policy / handbook questions (e.g. after a leave prompt
                # in the same session, or when the wizard is paused and the last turn
                # still mentioned leave). Those must stay HR_POLICY for RAG.
                if _strong_hr_policy(msg) or is_rules_query(msg):
                    return None
                return INTENT_LEAVE_REQUEST
        return None

    def _should_mutate_crm(self, intent: str, decision: dict[str, Any]) -> bool:
        if decision.get("outcome") == "NEEDS_CLARIFICATION":
            return False
        if intent == INTENT_UNKNOWN:
            return False
        if intent in (INTENT_LEAVE_BALANCE, INTENT_HR_POLICY):
            return False
        if intent in (INTENT_EXPENSE_STATUS, INTENT_REQUEST_STATUS):
            return False
        if intent == INTENT_LEAVE_REQUEST and decision.get("outcome") in (
            "SUBMITTED",
            "REJECTED",
        ):
            return True
        if intent == INTENT_WFH_REQUEST:
            return decision.get("outcome") == "PENDING_APPROVAL"
        if intent == INTENT_EXPENSE_CLAIM:
            return decision.get("outcome") in (
                "SUBMITTED",
                "AUTO_APPROVED",
                "PENDING_APPROVAL",
            )
        if intent == INTENT_ATTENDANCE_CORRECTION:
            return decision.get("outcome") == "PENDING_REVIEW"
        if intent == INTENT_APPROVAL_ESCALATION:
            return decision.get("outcome") == "PENDING_APPROVAL"
        return False

    @staticmethod
    def _norm_user_message(s: str) -> str:
        return " ".join((s or "").strip().lower().split())

    def _recent_duplicate_request_id(
        self,
        *,
        session: Any,
        context_lines: list[str],
        intent: str,
        entities: dict[str, Any],
        decision: dict[str, Any],
        user_message: str,
    ) -> str | None:
        """
        Lightweight duplicate-submission guard.
        If the user repeats the same request in the same session (common with chat UIs),
        do not create a new CRM record; return the previously created request id.
        """
        if intent == INTENT_LEAVE_REQUEST:
            if decision.get("outcome") not in (
                "SUBMITTED",
                "REJECTED",
            ):
                return None
            cur = self._leave_booking_signature(entities)
            fp = (getattr(session, "workflow_state", None) or {}).get(
                "last_leave_fingerprint"
            ) or {}
            stored = fp.get("sig")
            if stored and len(stored) == 5:
                prev = (
                    str(stored[0]),
                    str(stored[1]),
                    float(stored[2]),
                    str(stored[3]),
                    str(stored[4]),
                )
                rid = str(fp.get("request_id") or "")
                if (
                    rid
                    and cur[0] == prev[0]
                    and cur[1] == prev[1]
                    and abs(cur[2] - prev[2]) < 1e-6
                    and cur[3] == prev[3]
                    and cur[4] == prev[4]
                ):
                    return rid
            return None

        last_ref, prior_user = self._last_reference_and_prior_user(context_lines)
        if not last_ref or not prior_user:
            return None

        if intent == INTENT_EXPENSE_CLAIM:
            if decision.get("outcome") not in ("AUTO_APPROVED", "PENDING_APPROVAL"):
                return None
            # Same calendar day + same amount can be separate line items; only dedupe
            # accidental repeat of the *same* user message (double-send / tap).
            if self._norm_user_message(user_message) != self._norm_user_message(prior_user):
                return None
            try:
                amount_val = float(entities.get("amount"))
            except Exception:
                return None
            cur_date = str(entities.get("expense_incurred_date") or "").strip()
            prev_e = self.entities.extract_rules_only(
                prior_user, intent=INTENT_EXPENSE_CLAIM
            )
            prev_date = str(prev_e.get("expense_incurred_date") or "").strip()
            if not cur_date or not prev_date or cur_date != prev_date:
                return None
            m2 = re.search(r"(?<!\d)(\d{1,6})(?:[.,](\d{1,2}))?(?!\d)", prior_user)
            if not m2:
                return None
            whole, frac = m2.group(1), m2.group(2) or ""
            try:
                prev_amount = float(f"{whole}.{frac}" if frac else whole)
            except Exception:
                return None
            if abs(float(prev_amount) - float(amount_val)) <= 0.01:
                return last_ref
            return None

        return None

    @staticmethod
    def _last_reference_and_prior_user(
        context_lines: list[str],
    ) -> tuple[str | None, str | None]:
        """
        Most recent Assistant message that included a Reference id, paired with the
        User message that immediately preceded that Assistant turn in time order.
        """
        lines = context_lines or []
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            if not line.startswith("Assistant:"):
                continue
            content = line[len("Assistant:") :].strip()
            m = re.search(
                r"\b(?:ref|reference)\b\s*[:#-]?\s*([A-Za-z0-9-]+)\b", content, re.I
            )
            if not m:
                continue
            ref = m.group(1)
            for j in range(i - 1, -1, -1):
                if lines[j].startswith("User:"):
                    return ref, lines[j][len("User:") :].strip()
            # If ordering is skewed (same-timestamp turns), fall back to nearest user after.
            for j in range(i + 1, len(lines)):
                if lines[j].startswith("User:"):
                    return ref, lines[j][len("User:") :].strip()
            return ref, None
        return None, None

    @staticmethod
    def _leave_booking_signature(entities: dict[str, Any]) -> tuple[str, str, float, str, str]:
        """Paid/LWOP leave duplicate comparison using ledger totals + anchors."""
        from datetime import datetime

        start_raw = entities.get("start_date") or entities.get("date")
        end_raw = entities.get("end_date")
        start_s = ""
        end_s = ""
        if start_raw:
            try:
                start_s = str(datetime.fromisoformat(str(start_raw).split("T")[0]).date())
            except Exception:
                start_s = str(start_raw).split("T")[0]
        if end_raw:
            try:
                end_s = str(datetime.fromisoformat(str(end_raw).split("T")[0]).date())
            except Exception:
                end_s = str(end_raw).split("T")[0]
        ledger = compute_requested_leave_days(entities or {})
        pay = str((entities or {}).get("leave_payment_category") or "")
        scope = str((entities or {}).get("day_scope") or "")
        return (start_s, end_s, float(ledger), pay, scope)


def _wants_resume_leave(message: str) -> bool:
    low = (message or "").lower()
    raw = message or ""
    if re.search(r"\b(continue|resume|finish)\b.*\bleave\b", low):
        return True
    if re.search(r"\bleave\b.*\b(form|application|request)\b", low) and re.search(
        r"\b(continue|resume|back)\b", low
    ):
        return True
    if re.search(r"(ছুটি\s*(ফর্ম|আবেদন).*(চালু|শেষ|আবার)|continue\s*ছুটি)", raw, re.I):
        return True
    return False


def new_trace_id() -> str:
    return str(uuid.uuid4())
