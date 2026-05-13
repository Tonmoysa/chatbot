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
    pending_question,
    pending_step,
    process_leave_turn,
)
from chat.services.conversational import conversational_reply
from chat.services.memory_store import ConversationMemoryStore
from chat.services.observability import log_step
from chat.services.response_formatter import build_user_message
from chat.services.rules_handbook import answer_rules_query
from chat.services.translator import (
    detect_user_language,
    is_translation_request,
    translate_text,
)


_RULES_FOOTER_EN_SECTION = (
    "_(Ask \"show me all rules and regulations\" to see the full handbook.)_"
)
_RULES_FOOTER_EN_FULL = (
    "_(Ask a specific question — e.g. \"rules about leave\" or "
    "\"data confidentiality policy\" — to drill into one section.)_"
)
_RULES_FOOTER_BN_SECTION = (
    "_(সব নিয়ম ও নিয়মাবলী দেখতে চাইলে লিখুন \"show me all rules and regulations\"।)_"
)
_RULES_FOOTER_BN_FULL = (
    "_(কোনো নির্দিষ্ট বিষয়ে জানতে চাইলে প্রশ্ন করুন — যেমন \"ছুটির নিয়ম\" "
    "বা \"ডেটা গোপনীয়তা পলিসি\"।)_"
)


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
        employee_id: str,
        trace_id: str,
        document_text: str | None = None,
    ) -> dict[str, Any]:
        session = self.memory.get_or_create_session(session_id or "", employee_id)
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
        if is_leave_collecting(wf_state) and (is_greeting_now or is_cancel_now):
            session.workflow_state = deactivate_leave_session(wf_state)
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
                # Explicit chit-chat regex hit ("kemon acho?", "ki koro", "thanks",
                # "hi", jokes). Respond conversationally and keep the wizard alive.
                intent = INTENT_UNKNOWN
                intent_result = {
                    **intent_result,
                    "intent": INTENT_UNKNOWN,
                    "source": (intent_result.get("source") or "intent") + "+chitchat_in_wizard",
                }
            elif _strong_hr_policy(message):
                # User asked about rules/policy mid-wizard. Answer the rules question
                # and keep the wizard alive so they can resume afterwards.
                intent = INTENT_HR_POLICY
                intent_result = {
                    **intent_result,
                    "intent": INTENT_HR_POLICY,
                    "source": (intent_result.get("source") or "intent") + "+rules_in_wizard",
                }
            elif not _message_answers_wizard_step(
                message, pending_step(wf_state)
            ):
                # The wizard is mid-collection but the message doesn't look like an
                # answer for the current step (e.g. user typed "oi", "how are ypou?",
                # "tell me a joke"). Force UNKNOWN so the conversational fallback
                # fires instead of trapping the user in the wizard. The wizard state
                # is preserved so the user can resume with a proper answer.
                intent = INTENT_UNKNOWN
                intent_result = {
                    **intent_result,
                    "intent": INTENT_UNKNOWN,
                    "source": (intent_result.get("source") or "intent") + "+sidetalk_in_wizard",
                }
            else:
                intent = INTENT_LEAVE_REQUEST
                intent_result = {
                    **intent_result,
                    "intent": INTENT_LEAVE_REQUEST,
                    "source": (intent_result.get("source") or "intent") + "+leave_wizard",
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
        leave_collecting_blocked = False
        if intent == INTENT_LEAVE_REQUEST:
            lv_pack = process_leave_turn(
                workflow_state=getattr(session, "workflow_state", None) or {},
                message=message,
                entities=dict(entities),
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

        crm_context: dict[str, Any] = {}
        crm_payload: dict[str, Any] = {}
        status = "success"
        request_id = ""
        decision: dict[str, Any] = {}
        msg = ""
        rstatus = ""

        try:
            if intent in (
                INTENT_LEAVE_BALANCE,
                INTENT_LEAVE_REQUEST,
                INTENT_WFH_REQUEST,
            ):
                bal = self.crm.get_leave_balance(employee_id or session.employee_id)
                crm_context.update(bal)
                if intent == INTENT_LEAVE_BALANCE:
                    crm_payload.update(bal)

            if intent in (INTENT_EXPENSE_STATUS, INTENT_REQUEST_STATUS):
                rid = entities.get("request_id")
                if rid:
                    st = self.crm.get_request_status(str(rid))
                    crm_payload.update(st)
                else:
                    crm_payload["detail"] = "Missing request_id for status lookup."

            if intent == INTENT_EXPENSE_CLAIM:
                emp_ctx = employee_id or session.employee_id
                inc_iso = (entities.get("expense_incurred_date") or "").strip() or infer_expense_incurred_date_iso(
                    message=message, hints=entities, today=date.today()
                )
                day_tot = self.crm.get_expense_day_approved_total(emp_ctx, inc_iso)
                crm_context.update(day_tot)

            if intent == INTENT_HR_POLICY:
                rules_pack = answer_rules_query(message)
                crm_payload["rules_answer"] = rules_pack.get("text") or ""
                crm_payload["rules_mode"] = rules_pack.get("mode") or "full"
                crm_payload["rules_matched_sections"] = rules_pack.get("matched") or []
                log_step(
                    trace_id,
                    "rules_lookup",
                    {
                        "mode": rules_pack.get("mode"),
                        "matched": rules_pack.get("matched"),
                    },
                )

            if intent == INTENT_EXPENSE_DAY_SUMMARY:
                emp_ctx = employee_id or session.employee_id
                inc_iso = (entities.get("expense_incurred_date") or "").strip() or infer_expense_incurred_date_iso(
                    message=message, hints=entities, today=date.today()
                )
                breakdown = self.crm.get_expense_day_breakdown(emp_ctx, inc_iso)
                crm_payload.update(breakdown)
                crm_context.update(breakdown)

            if leave_collecting_blocked:
                decision = {
                    "outcome": "NEEDS_CLARIFICATION",
                    "reason": lv_pack.get("question")
                    or "আর একটু জানতে হবে — উপরের প্রশ্নের উত্তরটা নিচে লিখে পাঠান।",
                    "rules_applied": ["LEAVE_WORKFLOW_COLLECTING"],
                }
            else:
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
                exec_result = self.crm.create_request(
                    employee_id or session.employee_id,
                    intent,
                    entities,
                    decision,
                )
                request_id = str(exec_result.get("request_id") or "")
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
            #      (HR_POLICY with rules_mode == "no_match").
            # When the LLM is unavailable we keep the existing degraded text
            # so the user always gets *something*.
            used_conversational = False
            needs_conversational = (
                (
                    intent == INTENT_UNKNOWN
                    and decision.get("outcome") == "NEEDS_CLARIFICATION"
                )
                or (
                    intent == INTENT_HR_POLICY
                    and crm_payload.get("rules_mode") == "no_match"
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
                intent == INTENT_HR_POLICY
                and msg
                and not used_conversational
                and crm_payload.get("rules_mode") in ("full", "section")
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
                if rules_mode in ("full", "section"):
                    msg = msg.rstrip() + "\n\n" + _rules_footer(
                        mode=rules_mode, lang=user_lang
                    )

            # If the user asked a side-question (balance probe, chit-chat, rules
            # lookup) while the leave wizard is still mid-collection, remind them
            # of the open step so the form does not feel abandoned.
            if (
                intent in (
                    INTENT_LEAVE_BALANCE,
                    INTENT_UNKNOWN,
                    INTENT_HR_POLICY,
                )
                and is_leave_collecting(getattr(session, "workflow_state", None) or {})
            ):
                resume = pending_question(getattr(session, "workflow_state", None) or {})
                if resume:
                    msg = f"{msg}\n\n---\n_(ছুটি ফর্ম এখনও চালু আছে — পরের প্রশ্ন:)_\n\n{resume}"

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
            or "**Step " in last_assistant
            or "Step 3 of 5" in last_assistant
        ):
            if len(msg) <= 180 or is_dateish:
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
            "APPROVED",
            "REJECTED",
            "PENDING_APPROVAL",
        ):
            return True
        if intent == INTENT_WFH_REQUEST:
            return decision.get("outcome") == "PENDING_APPROVAL"
        if intent == INTENT_EXPENSE_CLAIM:
            return decision.get("outcome") in ("AUTO_APPROVED", "PENDING_APPROVAL")
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
                "APPROVED",
                "REJECTED",
                "PENDING_APPROVAL",
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


def new_trace_id() -> str:
    return str(uuid.uuid4())
