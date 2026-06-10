# Session Turn Router — Specification (Phase 1)

> **Goal:** এক জায়গায় সব routing decision নেওয়া, যাতে একই message-কে অনেক layer আলাদা ভাবে interpret করে bug না হয়।  
> **Status:** Spec only — implement ধাপে ধাপে।  
> **Related:** `docs/WORKFLOW_STATE_MACHINES.md`, `WORKFLOW_TEST_MATRIX.md`, scenario tests 35/36/40.

---

## 1. Problem (কেন bug বারবার আসে)

এক user message আজ **৬+ independent layer** দিয়ে যায়:

| Layer | File | কী decide করে |
|-------|------|----------------|
| L0 | `intent_detector.py` | Global intent (LLM + rules) |
| L1 | `hr_query_classifier.py` | HR query kind override |
| L2 | `orchestrator._detect_intent_during_leave_workflow` | Leave-active intent lock |
| L3 | `orchestrator._detect_intent_during_expense_workflow` | Expense-active intent lock |
| L4 | `turn_classifier.classify_workflow_turn` | Turn type (CORRECTION, CONFIRM…) |
| L5 | `wizard_interrupt_classifier.py` | Wizard interrupt / workflow switch |
| L6 | `orchestrator` post-gates | leave_workflow_lock, expense_correction_priority, duplicate_leave_early, context_clarification |
| L7 | `expense/wizard_commands.py` | Done/submit/cancel commands |
| L8 | `expense/turn_parser.py` + `turn_router.py` | Expense wizard execution |
| L9 | `message_context_clarity.py` | Underspecified message guard |
| L10 | `suspended_leave_correction.py` | Suspended leave edit patterns |

**Conflict pattern:** Layer A বলে `EXPENSE_CORRECTION`, Layer B বলে `DONE_COLLECTING` / `LEAVE_CORRECTION` / `CONTEXT_CLARIFICATION` — কারণ **priority order আলাদা** এবং **context flags inconsistent** (`expense_active` vs `has_suspended_expense`).

**Scenario 40-এ যে bug গুলো এসেছিল — সব routing conflict:**

| Step | Message | Wrong route | Correct route |
|------|---------|-------------|---------------|
| 9 | `বাস ভাড়া ৮০→১২০` | data merge bug (separate) | `EXPENSE_CORRECTION` |
| 11 | `শেষ expense টা ৫০→৭০` | `DONE` / suspended leave correction | `EXPENSE_CORRECTION` (last ordinal) |
| 16 | `আবার ১০ জুলাই ছুটি চাই` | `CONTEXT_CLARIFICATION` | `DUPLICATE_LEAVE_CHECK` |
| 20 | `expense summary` (leave active, expense suspended) | active block empty | `EXPENSE_SUMMARY` (suspended draft) |
| 30 | `expense টা আরেকবার দেখাও` | no match | `EXPENSE_PRE_SUBMIT_REVIEW` |
| 39 | `reimbursement policy` | `EXPENSE_CLAIM` | `HR_POLICY` |

---

## 2. Target Architecture

```
User message + SessionSnapshot
        │
        ▼
┌───────────────────────────────┐
│  session_turn_router.py       │  ← NEW: single classifier
│  route_session_turn(...)      │
└───────────────────────────────┘
        │
        ▼
   SessionTurnDecision
        │
        ├── intent (optional override)
        ├── turn_kind (enum)
        ├── target_workflow (leave | expense | none)
        ├── handler_id (which module executes)
        ├── suspend/resume flags
        └── reason (debug string)
        │
        ▼
┌───────────────────────────────┐
│  orchestrator.py (thin)       │  ← dispatch only
│  expense/turn_router.py       │  ← expense execution (unchanged role)
│  leave_workflow.py            │  ← leave execution
└───────────────────────────────┘
```

**Rule:** Pattern matching + priority **শুধু** `session_turn_router.py`-তে। অন্য file শুধু **predicate** export করবে (`looks_like_*`, `wants_*`) — decide করবে না।

---

## 3. SessionSnapshot (router input)

```python
@dataclass(frozen=True)
class SessionSnapshot:
    message: str
    # Active workflows
    leave_active: bool
    leave_stage: str | None          # collecting | review_pending | edit_* | submitted_locked
    leave_review_pending: bool
    pending_leave_step: str | None
    expense_active: bool
    expense_stage: str | None          # collecting | review | submit_confirm
    expense_review_pending: bool
    pending_expense_step: str | None   # category | from_to | clarify | delete_verify
    # Parked workflows
    has_suspended_leave: bool
    has_suspended_expense: bool
    has_expense_draft: bool            # items exist (active OR suspended)
    # Pending UI states
    duplicate_leave_choice_pending: bool
    expense_delete_verify_pending: bool
    leave_submit_confirm_pending: bool
    expense_submit_confirm_pending: bool
    # Probes (pre-computed once)
    balance_probe: bool
    policy_interrupt: bool
    # Context
    context_lines: list[str]
    is_greeting: bool
    is_cancel: bool
```

**Important flags (bug prevention):**

- `expense_domain_active = expense_active or has_suspended_expense or has_expense_draft`
- `leave_domain_active = leave_active or has_suspended_leave`
- Ordinal/correction rules **must** use `expense_domain_active`, not only `expense_active`

---

## 4. SessionTurnDecision (router output)

```python
class TurnKind(str, Enum):
    # Global
    CANCEL = "cancel"
    CHITCHAT = "chitchat"
    OUT_OF_SCOPE = "out_of_scope"
    POLICY_QUERY = "policy_query"
    BALANCE_QUERY = "balance_query"
    CONTEXT_CLARIFICATION = "context_clarification"
    # Workflow lifecycle
    NEW_LEAVE = "new_leave"
    NEW_EXPENSE = "new_expense"
    WORKFLOW_SWITCH = "workflow_switch"      # suspend current, start other
    RESUME_SUSPENDED = "resume_suspended"
    DEFER_SUBMIT = "defer_submit"            # "submit leave first" etc.
    # In-wizard
    SLOT_ANSWER = "slot_answer"
    CONFIRM_YES = "confirm_yes"
    CONFIRM_NO = "confirm_no"
    CORRECTION = "correction"
    SUMMARY = "summary"
    SUBMIT_COMMAND = "submit_command"
    DONE_COLLECTING = "done_collecting"
    DELETE_REQUEST = "delete_request"
    DELETE_CONFIRM = "delete_confirm"
    DUPLICATE_LEAVE = "duplicate_leave"
    META_QUESTION = "meta_question"          # post-submit edit, timing, etc.
    PRE_SUBMIT_REVIEW = "pre_submit_review"
    # Fallback
    CONTINUE_WIZARD = "continue_wizard"
    UNKNOWN = "unknown"

@dataclass
class SessionTurnDecision:
    turn_kind: TurnKind
    intent: str | None              # INTENT_* override; None = use global detector
    target_workflow: str | None     # "leave" | "expense" | None
    handler_id: str                 # see §7
    confidence: float
    reason: str                     # e.g. "P04_expense_correction_before_done"
    flags: dict = field(default_factory=dict)
    # flags examples: pause_leave, suspend_expense, force_leave_lock, skip_llm_intent
```

---

## 5. Master Priority Matrix

**Evaluation order:** উপর থেকে নিচ — প্রথম match জিতবে।  
**Prefix:** `P##` — golden test-এ reference করবে।

### Tier 0 — Hard guards (session-level)

| ID | Condition | TurnKind | Intent | Handler |
|----|-----------|----------|--------|---------|
| P00 | `is_cancel` | CANCEL | UNKNOWN | `workflow_cancel` |
| P01 | `duplicate_leave_choice_pending` | SLOT_ANSWER | LEAVE_REQUEST | `leave.duplicate_choice` |
| P02 | `expense_delete_verify_pending` + yes/no | DELETE_CONFIRM | EXPENSE_CLAIM | `expense.turn_router` |

### Tier 1 — Explicit commands (high confidence)

| ID | Predicate (existing fn) | Context | TurnKind | Intent | Handler |
|----|-------------------------|---------|----------|--------|---------|
| P03 | `wants_leave_submit_command` | leave_domain | SUBMIT_COMMAND | LEAVE_REQUEST | `leave_workflow` |
| P04 | `wants_expense_submit_command` | expense_domain | SUBMIT_COMMAND | EXPENSE_CLAIM | `expense.turn_router` |
| P05 | `wants_cancel_leave_command` | any | CANCEL | LEAVE_REQUEST | `leave_workflow` |
| P06 | `wants_cancel_expense_command` | expense_active | CANCEL | EXPENSE_CLAIM | `expense_workflow` |

### Tier 2 — Corrections (MUST beat done/navigate/suspended-leave)

| ID | Predicate | Context | TurnKind | Intent | Handler | Notes |
|----|-----------|---------|----------|--------|---------|-------|
| **P10** | `looks_like_expense_correction` | `expense_domain_active` | CORRECTION | EXPENSE_CLAIM | `expense.turn_router` | **শেষ expense**, ordinal, amount replace |
| P11 | `looks_like_suspended_leave_correction` AND NOT P10 | `has_suspended_leave` | CORRECTION | LEAVE_REQUEST | `leave_workflow` | expense+ordinal excluded |
| P12 | `looks_like_leave_review_update` AND NOT P10 | `leave_review_pending` | CORRECTION | LEAVE_REQUEST | `leave_workflow` | |
| P13 | `parse_edit_slot` / `_looks_like_slot_correction` | leave_domain | CORRECTION | LEAVE_REQUEST | `leave_workflow` | |

> **Invariant I1:** P10 always runs before `wants_expense_done_command_rules` and before P11.

### Tier 3 — Duplicate / clarification (before generic intent)

| ID | Predicate | Context | TurnKind | Intent | Handler |
|----|-----------|---------|----------|--------|---------|
| P20 | `আবার/again` + leave application + overlap | not leave_active collecting | DUPLICATE_LEAVE | LEAVE_REQUEST | `leave_meta_queries` |
| P21 | `should_ask_context_clarification` | no wizard continuation, not P20 | CONTEXT_CLARIFICATION | UNKNOWN | `message_context_clarity` |

> **Invariant I2:** P20 always before P21 (step 16 bug).

### Tier 4 — Confirm / deny (wizard stage aware)

| ID | Predicate | Context | TurnKind | Intent | Handler |
|----|-----------|---------|----------|--------|---------|
| P30 | `is_confirmation_yes` (leave or expense) | review/submit_confirm pending | CONFIRM_YES | *workflow* | stage handler |
| P31 | `is_confirmation_no` / `is_confirmation_cancel` | review/submit_confirm | CONFIRM_NO | *workflow* | stage handler |
| P32 | `wants_defer_expense_for_leave_submit` | leave confirm + suspended expense | DEFER_SUBMIT | LEAVE_REQUEST | `leave_confirm` |
| P33 | `wants_defer_leave_for_expense_submit` | expense confirm + suspended leave | DEFER_SUBMIT | EXPENSE_CLAIM | `leave_confirm` |

### Tier 5 — Summary / review / meta (informational)

| ID | Predicate | Context | TurnKind | Intent | Handler |
|----|-----------|---------|----------|--------|---------|
| P40 | `wants_expense_pre_submit_review` | expense submit_confirm | PRE_SUBMIT_REVIEW | EXPENSE_CLAIM | `expense.session_action_memory` |
| P41 | `wants_expense_summary` | `expense_domain_active` | SUMMARY | EXPENSE_DAY_SUMMARY | `expense_workflow` |
| P42 | `wants_leave_session_summary` | leave_domain | SUMMARY | LEAVE_REQUEST | `leave_meta_queries` |
| P43 | `wants_expense_meta_question` / post-submit edit | any | META_QUESTION | varies | `expense.session_action_memory` |

> **Invariant I3:** P41 uses suspended draft when `expense_active=False` but `has_expense_draft` (step 20).

### Tier 6 — Workflow switch / resume

| ID | Predicate | Context | TurnKind | Intent | Handler |
|----|-----------|---------|----------|--------|---------|
| P50 | strong leave application | expense_active | WORKFLOW_SWITCH | LEAVE_REQUEST | `workflow_suspend` |
| P51 | `_strong_expense_claim` AND NOT policy | leave_active | WORKFLOW_SWITCH | EXPENSE_CLAIM | `workflow_suspend` |
| P52 | `wants_resume_suspended_leave` | has_suspended_leave | RESUME_SUSPENDED | LEAVE_REQUEST | `workflow_suspend` |
| P53 | `wants_resume_or_show_expense` | expense_domain | RESUME_SUSPENDED | EXPENSE_CLAIM | `expense_workflow` |

### Pre-router navigation rows (N50–N55)

These run **before** P00–P99 via `plan_pre_router_navigation` so the snapshot
the classifier sees already reflects resume/restore/switch. The orchestrator
only persists each planned step and logs `rule` + legacy `log_step` name.

| ID | Condition | Mutation |
|----|-----------|----------|
| N50 | `is_leave_paused` + not cancel + (resume leave OR not policy interrupt) | `resume_leave_session` |
| N51 | suspended leave + resume leave + expense active + leave not active | `switch_active_expense_to_suspended_leave` |
| N52a | expense paused + resume leave + has suspended leave | `switch_active_expense_to_suspended_leave` |
| N52b | expense paused + `wants_resume_or_show_expense` | `resume_expense_session` |
| N53 | suspended leave + nothing active + resume/apply/answer step | `restore_suspended_leave` |
| N54 | suspended expense + nothing active + resume/show expense | `restore_suspended_expense` |
| N55a | leave in progress + expense query | `suspend_leave_for_workflow_switch` |
| N55b | leave in progress + misrouted leave clear | `clear_suspended_leave` + `deactivate_leave_session` |

### Tier 7 — Done collecting (AFTER correction tier)

| ID | Predicate | Context | TurnKind | Intent | Handler |
|----|-----------|---------|----------|--------|---------|
| P60 | `wants_expense_done_command_rules` AND NOT `looks_like_expense_correction` | expense collecting | DONE_COLLECTING | EXPENSE_CLAIM | `expense.turn_router` |

> **Invariant I4:** `শেষ` in done-rules must not match `শেষ expense` (step 11 bug).

### Tier 8 — Policy / balance / chitchat

| ID | Predicate | Context | TurnKind | Intent | Handler |
|----|-----------|---------|----------|--------|---------|
| P70 | `_is_policy_query` / `HR_POLICY` hr_query | any | POLICY_QUERY | HR_POLICY | `policy_kb` |
| P71 | `balance_probe` | any | BALANCE_QUERY | LEAVE_BALANCE | `leave_balance` |
| P72 | `_looks_like_chitchat` / out-of-scope | wizard active | CHITCHAT | UNKNOWN | side-question handler |
| P73 | `is_general_knowledge_out_of_scope` | any | OUT_OF_SCOPE | UNKNOWN | fallback |

> **Invariant I5:** Policy hr_query overrides `_strong_expense_claim` (step 39: `reimbursement policy`).

### Tier 9 — Default slot continuation

| ID | Condition | TurnKind | Intent | Handler |
|----|-----------|----------|--------|---------|
| P80 | `leave_active` + answers pending step | SLOT_ANSWER | LEAVE_REQUEST | `leave_workflow` |
| P81 | `expense_active` + wizard continuation / clarify reply | SLOT_ANSWER | EXPENSE_CLAIM | `expense.turn_router` |
| P82 | `leave_active` (fallback) | CONTINUE_WIZARD | LEAVE_REQUEST | `leave_workflow` |
| P83 | `expense_active` (fallback) | CONTINUE_WIZARD | EXPENSE_CLAIM | `expense_workflow` |
| P99 | none matched | UNKNOWN | None | global intent_detector |

---

## 6. Intent Resolution (after TurnKind)

Router `intent` field set হলে orchestrator **LLM intent skip** করবে (`skip_llm_intent=True`).

| TurnKind | Default intent override |
|----------|-------------------------|
| CORRECTION + expense | `EXPENSE_CLAIM` |
| CORRECTION + leave | `LEAVE_REQUEST` |
| SUMMARY expense | `EXPENSE_DAY_SUMMARY` |
| POLICY_QUERY | `HR_POLICY` |
| DUPLICATE_LEAVE | `LEAVE_REQUEST` |
| WORKFLOW_SWITCH | target workflow intent |
| CHITCHAT / OUT_OF_SCOPE | `UNKNOWN` |

**hr_query_classifier** শুধু তখন override করবে যখন router `intent=None` (P99 path) অথবা explicit `allow_hr_query_override=True` (policy beats expense claim).

---

## 7. Handler Registry

| handler_id | Module | Executes |
|------------|--------|----------|
| `expense.turn_router` | `expense/turn_router.py` | `route_expense_wizard_turn` |
| `expense_workflow` | `expense_workflow.py` | summary, suspend resume, ingest |
| `leave_workflow` | `leave_workflow.py` | slot fill, review, submit |
| `leave_meta_queries` | `leave_meta_queries.py` | duplicate check, session summary |
| `leave.duplicate_choice` | `leave/duplicate_choice.py` | duplicate choice UI |
| `leave_confirm` | `leave_confirm.py` | defer submit, confirm gates |
| `workflow_suspend` | `workflow_suspend.py` | park/restore snapshots |
| `workflow_cancel` | orchestrator helpers | deactivate + clear suspended |
| `message_context_clarity` | `message_context_clarity.py` | clarification prompt |
| `expense.session_action_memory` | `expense/session_action_memory.py` | meta / pre-submit review |
| `policy_kb` | policy retrieval path | KB answer |
| `global_intent` | `intent_detector` + entity pipeline | cold-start messages |

---

## 8. Migration Map (বর্তমান → নতুন)

### Phase 2 — Create router (no orchestrator change yet)

| Step | Action |
|------|--------|
| 2.1 | Create `chat/services/session_turn_router.py` |
| 2.2 | Create `chat/services/session_snapshot.py` (build from workflow_state) |
| 2.3 | Implement `route_session_turn(snapshot) -> SessionTurnDecision` using §5 matrix |
| 2.4 | Add `tests/test_session_turn_router.py` — golden rows (§9) |

### Phase 3 — Wire orchestrator (incremental)

| Current location | Lines / area | Move to router rule | Remove from orchestrator when |
|------------------|--------------|---------------------|------------------------------|
| `_detect_intent_during_leave_workflow` | ~306–550 | P03–P13, P30–P33, P40–P42 | golden tests pass |
| `_detect_intent_during_expense_workflow` | ~601–800 | P04–P13, P40–P43 | golden tests pass |
| `classify_workflow_turn` call + post-processing | ~1854–2150 | Entire §5 matrix supersedes | router parity verified |
| `expense_correction_priority` block | ~2088–2106 | P10 | redundant |
| `leave_workflow_lock` block | ~2107–2114 | P80–P82 with expense exception | redundant |
| `duplicate_leave_early` | ~2281–2301 | P20 | redundant |
| `should_ask_context_clarification` | ~2302–2324 | P21 (after P20) | redundant |
| `wizard_interrupt_classifier` inside turn_classifier | turn_classifier L176–195 | P50–P53, P70–P72 | interrupt tests pass |

**Keep in orchestrator (execution only):**
- CRM submit, persistence, tracing, entity extraction
- `route_expense_wizard_turn` dispatch
- `leave_workflow` dispatch
- Response formatting

### Phase 4 — Thin wrappers / deprecate

| File | After migration |
|------|-----------------|
| `turn_classifier.py` | Deprecated → thin wrapper calling `session_turn_router` OR delete |
| `wizard_interrupt_classifier.py` | Predicates only; `classify_*` removed |
| `orchestrator` intent gates | Deleted; single `decision = route_session_turn(snapshot)` |
| `wizard_commands.wants_expense_done_command_rules` | Predicate only; priority in router P60 |

### Phase 5 — Do NOT merge (execution stays separate)

| Module | Why keep separate |
|--------|-------------------|
| `expense/turn_parser.py` | Parses correction plans (execution detail) |
| `expense/turn_router.py` | Stage-specific handlers (already good pattern) |
| `expense/command_executor.py` | Applies correction plan to items |
| `leave/turn_apply.py` | Leave slot application |

---

## 9. Golden Test Rows (অবশ্যই `test_session_turn_router.py`-তে)

প্রতিটি row: `(message, snapshot_fixture, expected_turn_kind, expected_intent, expected_handler)`

### From scenario 40 (routing-critical)

| # | Message | Snapshot highlights | Expected |
|---|---------|---------------------|----------|
| G01 | `বাস ভাড়া ৮০ টাকা না, ১২০ টাকা হবে` | expense_active, 2+ items | P10 CORRECTION, EXPENSE_CLAIM |
| G02 | `শেষ expense টা ৫০ না, ৭০` | expense_active, 3 items | P10 CORRECTION (NOT P60 DONE) |
| G03 | `আবার ১০ জুলাই ছুটি চাই` | leave_submitted Jul10, not collecting | P20 DUPLICATE_LEAVE (NOT P21) |
| G04 | `expense summary দেখাও` | leave_active, has_suspended_expense | P41 SUMMARY, EXPENSE_DAY_SUMMARY |
| G05 | `expense টা আরেকবার দেখাও` | expense submit_confirm | P40 PRE_SUBMIT_REVIEW |
| G06 | `reimbursement policy কী` | no active wizard | P70 POLICY_QUERY, HR_POLICY |
| G07 | `python কি?` | any | P73 OUT_OF_SCOPE |
| G08 | `leave submit করো` | leave review_pending | P03 SUBMIT_COMMAND |
| G09 | `নতুন leave request খুলে দাও` | after duplicate prompt | P50 or NEW_LEAVE (workflow switch) |
| G10 | `হ্যাঁ` | expense delete_verify pending | P02 DELETE_CONFIRM |

### Cross-scenario invariants

| # | Message | Must NOT route to |
|---|---------|-------------------|
| G11 | `শেষ expense টা ৫০ না, ৭০` | DONE_COLLECTING, suspended_leave_correction |
| G12 | `আবার ১০ জুলাই ছুটি চাই` | CONTEXT_CLARIFICATION |
| G13 | `reimbursement policy` | EXPENSE_CLAIM |
| G14 | `শেষ` (alone, collecting, no expense noun) | CORRECTION |
| G15 | `বাস ভাড়া ৮০→১২০` during leave collecting + suspended expense | EXPENSE_CLAIM not LEAVE_REQUEST |

---

## 10. Predicate Ownership (নতুন pattern যোগ করার নিয়ম)

| Pattern type | Single owner file | Register in router |
|--------------|-------------------|-------------------|
| Expense amount/ordinal correction | `expense/expense_confirm.py` → `looks_like_expense_correction` | P10 |
| Expense done/submit/cancel | `expense/wizard_commands.py` | P04, P06, P60 |
| Expense summary/review/meta | `expense/session_action_memory.py`, `expense_workflow.py` | P40–P43 |
| Leave confirm/defer | `leave_confirm.py` | P30–P33 |
| Leave duplicate/overlap | `leave_meta_queries.py` | P20 |
| Parallel leave block (active wizard) | `leave_meta_queries.py` → `should_block_parallel_leave_application` | R04 (workflow gate; not session router) |
| Leave reason sick→other reselect | `leave/reason_bucket_classifier.py` → `_apply_bucket_to_draft` | R03 (entity/workflow reconcile; not session router) |
| Non-sick Select Leave (annual vs LWOP) | `leave_draft_utils.py` → `is_non_sick_wizard_leave`, `should_auto_infer_wizard_leave_type` | R05 (workflow schema SLOT_LEAVE_TYPE; not session router) |
| Expense route amount strip (BN) | `expense_extraction.py` → `_trim_route_location_tail`, `_BN_PLACE_ROMAN` | E01 (entity pipeline; not session router) |
| Leave session summary vs balance | `leave_meta_queries.py` → `wants_leave_session_summary`, `session_has_leave_summary_context` | P42 (+ `hr_query_classifier` guard before balance) |
| Suspended leave edit | `suspended_leave_correction.py` | P11 (with P10 guard) |
| Leave reason / health signal | `leave/reason_value.py` → `looks_like_health_leave_reason`, `extract_reason_value` | R01–R02 (entity pipeline; not session router) |
| Policy / balance | `intent_detector.py`, `policy_intent_helpers.py` | P70–P71 |
| Context clarity | `message_context_clarity.py` | P21 |
| Chitchat / OOS | `intent_detector.py`, `policy_intent_helpers.py` | P72–P73 |

**নিয়ম:** নতুন Bengali phrase → **শুধু owner predicate-এ regex যোগ** + **golden test row**। orchestrator-এ copy করা **নিষিদ্ধ**।

---

## 11. Orchestrator Target Shape (post-migration)

```python
# orchestrator.py (simplified)
snapshot = build_session_snapshot(
    message, workflow_state=wf_state, context_lines=context_lines, ...
)
decision = route_session_turn(snapshot)

if decision.intent is not None:
    intent = decision.intent
    intent_result = {"intent": intent, "source": f"session_turn_router+{decision.reason}"}

if decision.turn_kind == TurnKind.CONTEXT_CLARIFICATION:
    return clarification_response(...)

if decision.handler_id == "expense.turn_router":
    return route_expense_wizard_turn(...)

if decision.handler_id == "leave_workflow":
    return handle_leave_turn(...)

# ... handler dispatch table ...
```

---

## 12. Implementation Checklist

### Phase 2 (classify only)
- [x] `session_snapshot.py` + `session_turn_router.py`
- [x] `tests/test_session_turn_router.py` with G01–G15
- [x] `pytest tests/test_session_turn_router.py` — all green (17 tests)
- [x] Existing scenario 35/36/40 still pass (no orchestrator change)

### Phase 3 (orchestrator wire) — **complete**
- [x] `session_turn_bridge.py` — map router → intent_result / workflow_turn / side effects
- [x] Orchestrator calls `run_session_turn_router` on every turn (logged as `session_turn_routed`)
- [x] Duplicate leave + context clarification from router (cold-start); legacy clarification kept during active wizard
- [x] `router_locked_intent` — workflow locks skip when router owns intent
- [x] Scenario 35/36/40 pass

### Phase 4 (cleanup) — **complete**
- [x] `turn_classifier.classify_workflow_turn` — router-first wrapper + `_classify_workflow_turn_legacy` fallback
- [x] Full wizard override — router owns intent/workflow_turn unless `P99_*` (legacy `_detect_intent_during_*` fallback via `legacy_wizard_intent_fallback`)
- [x] `wizard_interrupt_classifier` integrated at P84/P85 before continue-wizard fallback
- [x] Tier-7 parity rules: pending leave show, total check, restore, wizard commands, slot tokens, duplicate tomorrow, leave_nav_no_session
- [x] `route_session_turn(snapshot, workflow_state=..., trace_id=...)`
- [x] P11/P44/P42 priority fixes; leave draft correction short-circuit for router P12*; duplicate choice pending on P20
- [x] Legacy `_detect_intent_during_*` extracted to `legacy_wizard_intent.py` (orchestrator re-exports for tests)
- [x] `test_workflow_context_switch.py` — copy/assertion fixes for leave review + P11 guard
- [x] `test_expense_wizard_interrupt.py` — expense review yes path (P30b + review stage setup)
- [x] Update `WORKFLOW_TEST_MATRIX.md` (Mandatory CI Gate section)
- [x] Tier-9 guard: pending `reason` step → plain reason answer is SLOT_ANSWER, not P12a/P13 correction (`_explicit_correction_marker`)

### Phase 5 (CI gate)
- [x] `legacy_wizard_intent.py` — P99 fallback module (no longer ~580 LOC in orchestrator)
- [x] PR checklist: new phrase = predicate + golden row (see `WORKFLOW_TEST_MATRIX.md` → "Mandatory CI Gate")
- [x] Mandatory: `test_scenario_35/36/40` + `test_session_turn_router` documented as merge gate
- [x] Orchestrator post-gates respect `router_locked_intent` (no re-override when router decisively locks a wizard-active intent)
- [x] `apply_hr_query_to_intent` respects `router_locked` — only the HR_POLICY upgrade (invariant I5) may override a locked intent; meta/summary/status overrides skipped
- [x] Pre-router navigation mutations consolidated into one auditable phase: `orchestrator._apply_pre_router_navigation` (resume/switch/restore/suspend in a single seam before the router)
- [x] Full inversion: navigation driven by router-owned rows **N50–N55** in `plan_pre_router_navigation` — orchestrator only persists + logs each step (`rule` in log payload); behaviour identical to the legacy pre-router phase
- [x] LLM resilience: per-trace 429 fast-circuit in `llm_client` (rate-limited turn falls back to rules instantly; reset each turn)

### Execution-layer fixes (found via routing tests)

- [x] `expense/turn_router._route_review_confirm` — used real `datetime.date.today()` instead of the patchable module clock; submit-date policy now resolves via `expense_incurred_date` only

### Post-gate lock (Phase 5 detail)

`orchestrator.run_chat` computes `router_locked = router_locked_intent(intent_result)`
right after intent resolution. When true (source `session_turn_router+P<nn>` and
**not** `P99`), the following legacy post-gates are **skipped** so they cannot
re-introduce parallel-layer conflicts:

| Post-gate | Replaced by router row |
|-----------|------------------------|
| `expense_query_should_suspend_leave` → forced intent | P41 / P43 / P51 |
| `is_expense_entitlement_query` → `HR_POLICY` | P70 |
| `wants_expense_meta_question` → `EXPENSE_STATUS` | P43 |
| `wants_expense_pre_submit_review` → `EXPENSE_STATUS` | PRE_SUBMIT_REVIEW |

Cold-start (`self.intents.detect`) and the P99 legacy fallback path are **not**
locked, so their post-gates still run.

---

## 13. Debug / Observability

Router প্রতিটি decision-এ log করবে:

```json
{
  "step": "session_turn_routed",
  "turn_kind": "CORRECTION",
  "intent": "EXPENSE_CLAIM",
  "handler_id": "expense.turn_router",
  "reason": "P10_expense_correction",
  "matched_predicate": "looks_like_expense_correction"
}
```

Scenario test fail হলে `reason` দিয়ে **কোন priority row ভুল** তা তৎক্ষণাৎ দেখা যাবে — whack-a-mole কমবে।

---

## 14. FAQ

**Q: `expense/turn_router.py` আর `session_turn_router.py` পার্থক্য?**  
A: `session_turn_router` = **কোন workflow + কোন turn kind** (cross-workflow)। `expense/turn_router` = expense wizard-এর **execution** (edit/add/confirm handlers)।

**Q: এখনই পুরো refactor করব?**  
A: না। Phase 2 শুধু router + tests। Orchestrator untouched থাকলে risk কম।

**Q: LLM কোথায় থাকবে?**  
A: Entity extraction + cold-start intent (P99)। Wizard-active turn routing **rules-only** (বর্তমান `turn_classifier` এর মতো)।

---

*Last updated: 2026-06-10 — Phase 5 complete including router-driven navigation inversion (N50–N55); scenario 35/36/40 + session router (27 tests) + regression suites green (LLM-off deterministic).*
