# Checkpoint Epoch Compaction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent specialist context overruns by calibrating request admission from provider usage, externalizing cumulative working state at safe checkpoint boundaries, and compacting completed exploration epochs without breaking tool-call history.

**Architecture:** Keep `Conversation` responsible for wire rendering and structurally valid event compaction, while `SpecialistSession` owns prompt-usage calibration, checkpoint disposition, cumulative checkpoint state, and epoch boundaries. A normal compaction replaces old result bodies while retaining their original tool-call pairs; emergency reconstruction uses the latest controller-materialized cumulative checkpoint. The controller and CLI only project bounded lifecycle diagnostics.

**Tech Stack:** Python 3.11+, dataclasses, existing OpenAI-compatible chat-completions transport, pytest.

## Global Constraints

- Every checkpoint uses one cumulative schema and is a safe epoch boundary.
- Checkpoint creation never resets lifetime model-turn, tool-call, recovery, output-token, input-token, or deadline budgets.
- Checkpoint repair remains bounded to one strict no-reasoning attempt.
- Compaction never occurs before a valid model checkpoint or a previously retained valid checkpoint.
- `read_compacted_evidence` remains limited to controller-registered evidence IDs, four unique reads, and 4,000 characters per read.
- Working summaries and completed steps are continuation memory, not evidence and not publishable findings.
- No raw prompts, responses, evidence bodies, secrets, or reasoning are added to console diagnostics.
- No new action input is required; checkpoint response limits and safety margins derive from existing session limits.

---

### Task 1: Make checkpoints cumulative working-memory snapshots

**Files:**
- Modify: `pr_reviewer/specialist_runtime/types.py`
- Modify: `pr_reviewer/specialist_runtime/session.py`
- Test: `tests/test_specialist_runtime_session.py`

**Interfaces:**
- Produces: `SessionCheckpoint.working_summary: str` and `SessionCheckpoint.completed_steps: tuple[str, ...]`.
- Produces: `SpecialistSession._cumulative_checkpoint_payload() -> dict[str, object]`, a controller-owned self-contained snapshot used by reconstruction.
- Preserves: existing candidate delta protocol at the model boundary and cumulative active candidates in session state.

- [ ] **Step 1: Write failing tests for bounded working state parsing**

Add tests that submit a valid checkpoint with `working_summary`, `completed_steps`, `hypotheses`, `invariants_evaluated`, and `proposed_next_actions`, then assert every field survives in `SessionResult.checkpoint`. Include over-limit strings and arrays and assert the parser bounds them rather than allowing unbounded recovery state.

```python
def test_checkpoint_retains_bounded_cumulative_working_state():
    gateway = ScriptedGateway([checkpoint_response(
        inspected=["a.py"],
        unresolved=["OB-tests"],
        working_summary="The input reaches the controller through config validation.",
        completed_steps=["Compared action input and config fallback; values agree."],
        hypotheses=["Recovery authorization still needs a boundary test."],
        invariants_evaluated=["Lifetime budget is not reset by follow-up."],
        proposed_next_actions=["Inspect the recovery authorization test."],
    )])
    result = make_session(gateway).request_checkpoint("controller-request")
    assert result.checkpoint.working_summary.startswith("The input reaches")
    assert result.checkpoint.completed_steps == (
        "Compared action input and config fallback; values agree.",
    )
    assert result.checkpoint.proposed_next_actions == (
        "Inspect the recovery authorization test.",
    )
```

- [ ] **Step 2: Run the new checkpoint tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_session.py -k "cumulative_working_state"
```

Expected: FAIL because `SessionCheckpoint` has no working-summary/completed-step fields and the parser currently replaces model-proposed next actions with deterministic gaps.

- [ ] **Step 3: Extend the checkpoint schema and immutable type**

Add bounded schema properties:

```python
"working_summary": {"type": "string", "maxLength": 2_000},
"completed_steps": {
    "type": "array", "maxItems": 12,
    "items": {"type": "string", "maxLength": 500},
},
```

Add defaulted immutable fields to `SessionCheckpoint`, parse them through bounded helpers, and preserve actual `proposed_next_actions` when supplied. Deterministic gaps remain the fallback only when the field is absent or empty.

- [ ] **Step 4: Write the failing cumulative materialization test**

Create one initial checkpoint with a full candidate, then a second checkpoint containing only an unchanged-candidate omission and updated working state. Assert `_cumulative_checkpoint_payload()` contains the active candidate definition, latest working state, evidence IDs, obligation statuses, and candidate lifecycle state without requiring the model to repeat the candidate.

- [ ] **Step 5: Run the materialization test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_session.py -k "cumulative_checkpoint_payload"
```

Expected: FAIL because no cumulative payload projection exists.

- [ ] **Step 6: Implement cumulative checkpoint materialization**

Implement `_cumulative_checkpoint_payload()` using `latest_checkpoint`, `candidate_findings`, `_candidate_statuses`, coverage state, and retained evidence metadata. Candidate objects use the established `CandidateFinding` fields; evidence bodies are excluded.

- [ ] **Step 7: Run Task 1 tests and the session suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_session.py
```

Expected: PASS.

- [ ] **Step 8: Commit Task 1**

```powershell
git add pr_reviewer/specialist_runtime/types.py pr_reviewer/specialist_runtime/session.py tests/test_specialist_runtime_session.py
git commit -m "Retain cumulative specialist checkpoint state"
```

---

### Task 2: Estimate rendered requests and calibrate them from provider usage

**Files:**
- Modify: `pr_reviewer/specialist_runtime/model_gateway.py`
- Modify: `pr_reviewer/specialist_runtime/request_attempts.py`
- Modify: `pr_reviewer/specialist_runtime/session.py`
- Test: `tests/test_specialist_runtime_model_gateway.py`
- Test: `tests/test_specialist_runtime_session.py`
- Create: `tests/test_specialist_runtime_request_attempts.py`

**Interfaces:**
- Produces: `OpenAIModelGateway.render_request(request: ModelTurnRequest) -> dict[str, Any]`, shared by estimation and transport.
- Produces: `OpenAIModelGateway.rendered_request_bytes(request: ModelTurnRequest) -> int`.
- Produces: session-local calibration records keyed by `"tools"` and `"structured"` request mode.
- Extends: request-attempt terminal diagnostics with `actual_prompt_tokens`, `actual_completion_tokens`, `admission_tokens`, and `admission_source`.

- [ ] **Step 1: Write failing gateway rendering tests**

Assert `rendered_request_bytes()` includes tool schemas for exploration, excludes them for a checkpoint, includes response schema for strict JSON, and exactly matches `len(json.dumps(render_request(...), separators=(",", ":"), ensure_ascii=False).encode("utf-8"))`.

- [ ] **Step 2: Run gateway tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_model_gateway.py -k "rendered_request"
```

Expected: FAIL because rendering is currently private to `complete()`.

- [ ] **Step 3: Extract one shared request renderer**

Move the existing payload construction in `OpenAIModelGateway.complete()` into `render_request()`. `complete()` must call this method unchanged before transport. `rendered_request_bytes()` serializes that payload compactly and never includes API keys.

- [ ] **Step 4: Write failing usage-calibration admission tests**

Use a gateway that exposes rendered byte counts and returns two successful results with `usage.prompt_tokens`. Assert the next exploration request uses the largest observed conservative calibrated estimate rather than `Conversation.approx_tokens()`. Add a missing-usage case that uses rendered bytes plus safety, and keep tools/structured calibration independent.

```python
def test_provider_prompt_usage_calibrates_next_same_mode_admission():
    gateway = EstimatingGateway([
        checkpoint_response(usage={"prompt_tokens": 12_000, "completion_tokens": 100}),
    ], rendered_bytes=32_000)
    session = make_session(gateway, max_context_tokens=20_000)
    session.request_checkpoint("controller-request")
    estimate = session._estimate_admission(tools_enabled=False, max_tokens=2_048)
    assert estimate.source == "provider-calibrated"
    assert estimate.input_tokens >= 12_000
```

- [ ] **Step 5: Run calibration tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_session.py -k "calibrat or rendered_admission"
```

Expected: FAIL because session admission only uses the coarse conversation estimate.

- [ ] **Step 6: Implement conservative calibration**

For each request mode retain the last rendered byte count, actual prompt tokens, and maximum observed tokens-per-rendered-byte ratio/positive underestimate. The next estimate is the maximum of:

```python
ceil(rendered_bytes / 3)                       # conservative fallback
ceil(rendered_bytes * max_observed_ratio)      # calibrated ratio
coarse_conversation_tokens + observed_offset   # calibrated offset
```

Add the existing response reserve and a deterministic wire safety margin after input estimation. Ignore absent, zero, negative, or non-numeric usage. Record actual completion tokens for repair admission after the first attempt.

- [ ] **Step 7: Extend request-attempt accounting tests**

Add failing then passing assertions that completed attempts retain estimated admission and actual provider usage, while failed attempts retain the estimate and zero actual usage. Keep event payloads bounded scalars only.

- [ ] **Step 8: Run Task 2 suites**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_model_gateway.py tests/test_specialist_runtime_request_attempts.py tests/test_specialist_runtime_session.py
```

Expected: PASS.

- [ ] **Step 9: Commit Task 2**

```powershell
git add pr_reviewer/specialist_runtime/model_gateway.py pr_reviewer/specialist_runtime/request_attempts.py pr_reviewer/specialist_runtime/session.py tests/test_specialist_runtime_model_gateway.py tests/test_specialist_runtime_request_attempts.py tests/test_specialist_runtime_session.py
git commit -m "Calibrate specialist context admission"
```

---

### Task 3: Add explicit checkpoint dispositions and pressure-triggered synthesis

**Files:**
- Modify: `pr_reviewer/specialist_runtime/session.py`
- Test: `tests/test_specialist_runtime_session.py`

**Interfaces:**
- Produces: internal `CheckpointDisposition` values `compact_resume`, `pause`, and `finalize`.
- Extends: `SpecialistSession.request_checkpoint(reason, *, disposition=...)`.
- Produces: `_checkpoint_pressure_due() -> bool`, which reserves the initial checkpoint and one repair before normal exploration admission.

- [ ] **Step 1: Write failing prompt-disposition tests**

Assert checkpoint user content states the exact reason, whether compaction is immediate, and whether the session resumes, pauses, or finalizes. Assert every disposition says the checkpoint must be cumulative because it may become a future epoch boundary. Assert no transient remaining-budget message is present.

- [ ] **Step 2: Run prompt tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_session.py -k "checkpoint_disposition"
```

Expected: FAIL because checkpoint prompts currently include only the reason and candidate-retention contract.

- [ ] **Step 3: Implement checkpoint disposition prompts**

Add a small immutable disposition type or validated literal mapping. Keep checkpoint JSON instructions separate from lifecycle prose so repair requests can reuse the schema contract without duplicating the full lifecycle explanation.

- [ ] **Step 4: Write the failing proactive-pressure test**

Construct a session whose next exploration request fits under the old coarse estimate but whose rendered/calibrated projection plus two checkpoint response allowances crosses the context limit. Assert the next gateway request is a no-tools checkpoint with purpose `checkpoint` rather than a rejected exploration request.

- [ ] **Step 5: Run the pressure test and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_session.py -k "pressure_requests_checkpoint_before_exploration"
```

Expected: FAIL because pressure is checked only against one ordinary request.

- [ ] **Step 6: Implement checkpoint repair reservation**

Derive `checkpoint_max_tokens` from existing limits, bounded to `min(max_tokens, max(2_048, recovery_max_tokens * 2))`. Before exploration, project a no-tools checkpoint input and reserve two checkpoint outputs plus the repair-instruction estimate and safety margin. Do not use a fixed tool-count cadence.

- [ ] **Step 7: Preserve native first response and strict repair semantics**

Keep the first checkpoint response's ordinary content and reasoning in native assistant fields. The repair uses tools disabled, reasoning disabled, and only the still-needed output allowance. No compaction runs between attempts.

- [ ] **Step 8: Run the session suite**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_session.py
```

Expected: PASS.

- [ ] **Step 9: Commit Task 3**

```powershell
git add pr_reviewer/specialist_runtime/session.py tests/test_specialist_runtime_session.py
git commit -m "Checkpoint before specialist context pressure"
```

---

### Task 4: Compact validated exploration epochs without breaking tool history

**Files:**
- Modify: `pr_reviewer/conversation.py`
- Modify: `pr_reviewer/specialist_runtime/session.py`
- Test: `tests/test_conversation.py`
- Test: `tests/test_specialist_runtime_session.py`

**Interfaces:**
- Produces: `Conversation.compact_tool_epoch(end_index, replacements, *, keep_newest_results=2) -> EpochCompactionStats`.
- Produces: session checkpoint-span tracking and `_compact_validated_epoch()`.
- Preserves: original assistant tool-call IDs, names, arguments, and matching tool-result call IDs.

- [ ] **Step 1: Write failing conversation wire-validity tests**

Create several assistant reasoning/tool-call/result turns. Compact through a supplied event boundary and assert:

- old reasoning events are removed;
- original assistant tool calls remain;
- old result contents become deterministic compacted JSON;
- every call ID still has exactly one result;
- newest two exchanges retain complete result content;
- events after the boundary are unchanged;
- OpenAI rendering contains no orphan call or result.

- [ ] **Step 2: Run conversation tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_conversation.py -k "compact_tool_epoch"
```

Expected: FAIL because existing compaction removes completed call/result pairs into a notebook.

- [ ] **Step 3: Implement structured result replacement**

Add immutable `EpochCompactionStats` counts for removed reasoning, replaced results, retained full results, and removed empty assistant text. Replacement bodies contain only:

```json
{
  "status": "compacted",
  "evidence_id": "evidence:<hash>",
  "source_path": "path/or/identity",
  "original_bytes": 8192
}
```

The session supplies replacements only for successful evidence records and registers them in `_compacted_evidence`.

- [ ] **Step 4: Write failing session epoch-policy tests**

Assert:

- a normal-completion `pause` checkpoint does not compact;
- a context-pressure `compact_resume` checkpoint compacts only after validation;
- an invalid checkpoint and failed repair leave the conversation unmodified;
- a resumed safe-size paused session keeps its full prior epoch;
- a resumed pressure-size paused session compacts at its existing checkpoint without requesting a redundant checkpoint.

- [ ] **Step 5: Run epoch-policy tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_session.py -k "epoch_compaction or pause_checkpoint"
```

Expected: FAIL because the session has no validated epoch boundaries or disposition-aware compaction.

- [ ] **Step 6: Track checkpoint spans and compact safely**

Record the checkpoint request start and validated assistant response end. For normal compaction, compact only events older than the latest validated checkpoint response while protecting checkpoint request/response spans. Add one continuation user message containing the cumulative checkpoint, compacted evidence catalogue, exact removal summary, and `proposed_next_actions`.

- [ ] **Step 7: Write failing older-epoch and emergency reconstruction tests**

Create two checkpoints. Assert the second permits removal of non-checkpoint messages older than checkpoint 1 while retaining both valid checkpoint pairs. Then force a post-compaction admission failure and assert emergency reconstruction contains system prompt, immutable assignment, latest cumulative checkpoint with active candidate definitions, bounded evidence ledger, newest fitting exchanges, and one continuation instruction.

- [ ] **Step 8: Implement older-epoch pruning and emergency reconstruction**

Use session-owned checkpoint spans; do not infer boundaries from model text. Reuse the cumulative payload from Task 1. Emergency reconstruction is allowed only when a valid checkpoint exists.

- [ ] **Step 9: Run Task 4 suites**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_conversation.py tests/test_specialist_runtime_session.py
```

Expected: PASS.

- [ ] **Step 10: Commit Task 4**

```powershell
git add pr_reviewer/conversation.py pr_reviewer/specialist_runtime/session.py tests/test_conversation.py tests/test_specialist_runtime_session.py
git commit -m "Compact validated specialist epochs"
```

---

### Task 5: Recover once from provider context-limit errors

**Files:**
- Modify: `pr_reviewer/specialist_runtime/session.py`
- Modify: `pr_reviewer/transport.py` only if a bounded reusable classifier belongs beside `ModelRequestError`
- Test: `tests/test_specialist_runtime_session.py`
- Test: `tests/test_api_key_argv.py` only if `transport.py` changes

**Interfaces:**
- Produces: `_is_context_limit_error(exc: BaseException) -> bool`.
- Produces: one session-lifetime emergency-checkpoint guard.
- Preserves: original masked provider error in request diagnostics.

- [ ] **Step 1: Write failing context-error classification tests**

Parametrize masked errors containing `context_length_exceeded`, `context size`, `maximum context`, `prompt too long`, and `too many tokens`. Include an unrelated HTTP 500 and assert it is not classified as context pressure.

- [ ] **Step 2: Run classifier tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_session.py -k "context_limit_error"
```

Expected: FAIL because provider context errors currently propagate as generic specialist failures.

- [ ] **Step 3: Implement the bounded classifier**

Inspect `str(exc)` and `ModelRequestError.body` after existing secret masking. Match only the approved case-insensitive phrases. Do not classify timeouts, cancellations, authentication errors, or arbitrary HTTP 500 responses.

- [ ] **Step 4: Write the failing emergency-checkpoint success test**

Script a tool-enabled exploration request that raises a context error, followed by a valid no-tools checkpoint. Assert exactly one emergency checkpoint runs, it sees the last accepted conversation state, compaction occurs after validation, and the session returns a resumable checkpoint rather than generic degradation.

- [ ] **Step 5: Write the failing emergency-checkpoint failure test**

Make both exploration and emergency checkpoint raise context errors. Assert no third provider call occurs. With a prior valid checkpoint, assert emergency reconstruction uses it; without one, assert the session stops degraded with retention uncertainty.

- [ ] **Step 6: Run emergency tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_session.py -k "emergency_checkpoint"
```

Expected: FAIL because context errors are not recoverable and no one-attempt guard exists.

- [ ] **Step 7: Implement one bounded emergency path**

Catch classified context errors only around tool-enabled exploration requests. Charge the failed reserved model turn as today. Attempt one no-tools checkpoint using the bounded checkpoint output limit. On success, invoke validated epoch compaction. On a second context error, select the previous valid checkpoint path without another provider request.

- [ ] **Step 8: Run Task 5 suites**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_session.py tests/test_api_key_argv.py
```

Expected: PASS.

- [ ] **Step 9: Commit Task 5**

```powershell
git add pr_reviewer/specialist_runtime/session.py pr_reviewer/transport.py tests/test_specialist_runtime_session.py tests/test_api_key_argv.py
git commit -m "Recover specialist context-limit failures"
```

---

### Task 6: Project bounded diagnostics and dogfood the new runtime

**Files:**
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Modify: `.github/workflows/ai-pr-review.yaml`
- Test: `tests/test_specialist_runtime_controller.py`
- Test: `tests/test_specialist_runtime_cli.py`
- Test: `tests/test_ai_pr_review_workflow.py`

**Interfaces:**
- Extends: `specialist_checkpoint_diagnostics`, `llm_request_*`, and compaction lifecycle events with bounded scalar fields.
- Produces: console lines identifying checkpoint reason/disposition, admission source, actual usage, compaction level/counts, and emergency outcome.

- [ ] **Step 1: Write failing event-projection tests**

Assert artifacts and console rendering include:

- checkpoint reason and disposition;
- estimated input, provider-calibrated input, response reserves, and selected admission source;
- actual prompt/completion usage for successful requests;
- regular/emergency compaction with before/after estimates and bounded counts;
- emergency checkpoint success/failure outcome.

Assert prompts, raw responses, evidence bodies, and reasoning fragments are absent.

- [ ] **Step 2: Run diagnostic tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_specialist_runtime_cli.py tests/test_specialist_runtime_controller.py -k "admission or compaction or checkpoint_diagnostic"
```

Expected: FAIL because the new diagnostic fields/events are not projected or rendered.

- [ ] **Step 3: Implement bounded event projection and rendering**

Keep event values scalar or bounded tuples. Console output uses one lifecycle line per checkpoint/compaction decision and continues suppressing delayed duplicate `specialist_request_*` messages.

- [ ] **Step 4: Run focused runtime verification**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_conversation.py tests/test_specialist_runtime_model_gateway.py tests/test_specialist_runtime_request_attempts.py tests/test_specialist_runtime_session.py tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_cli.py tests/test_ai_pr_review_workflow.py
git diff --check
```

Expected: all tests PASS and `git diff --check` emits no output.

- [ ] **Step 5: Commit runtime diagnostics**

```powershell
git add pr_reviewer/specialist_runtime/controller.py pr_reviewer/specialist_runtime/cli.py tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_cli.py
git commit -m "Explain specialist epoch compaction"
```

- [ ] **Step 6: Pin the dogfood workflow to the runtime commit**

Replace the existing immutable SHA in `.github/workflows/ai-pr-review.yaml` with the full SHA from Step 5. Do not point the workflow at the subsequent pin-only commit.

- [ ] **Step 7: Verify and commit the workflow pin**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_ai_pr_review_workflow.py
git diff --check
git add .github/workflows/ai-pr-review.yaml
git commit -m "Pin dogfood epoch compaction"
```

Expected: workflow tests PASS and the pin is a 40-character immutable commit SHA.

---

## Final Verification

- [ ] Run the complete focused regression set:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_conversation.py tests/test_specialist_runtime_model_gateway.py tests/test_specialist_runtime_request_attempts.py tests/test_specialist_runtime_session.py tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_cli.py tests/test_ai_pr_review_workflow.py
git diff --check HEAD~6..HEAD
git status --short
```

- [ ] Confirm that only known pre-existing/unrelated untracked files remain unstaged.
- [ ] Report commits, exact test counts, and the fact that pushing requires user authentication.
