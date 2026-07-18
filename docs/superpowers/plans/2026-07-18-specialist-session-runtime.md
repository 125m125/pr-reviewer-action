# Specialist Session Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current report-oriented specialist runner with a continuous, evidence-backed session runtime for large multilingual GitHub repositories, and ship an agent-facing migration handoff for downstream projects.

**Architecture:** Add a focused `pr_reviewer.specialist_runtime` package that owns deterministic coverage, assignment validation, durable specialist sessions, lifetime budgets, evidence, scheduling, reconciliation, negotiation, adjudication, and artifacts. Reuse the existing OpenAI-compatible transport, neutral `Conversation`, SSE reassembler, read-only executors, redaction, enforcement, and GitHub publishing helpers; reduce `scripts/run_specialist_reviews.py` to a CLI adapter and replace the current publish dispatcher with structured handoff/note publication.

**Tech Stack:** Python 3 standard library and dataclasses, pytest, Bash composite GitHub Action, GitHub REST/GraphQL through `gh`, OpenAI-compatible `/chat/completions`, SearXNG-compatible search JSON.

## Global Constraints

- Primary supported platform: GitHub.
- Primary model protocol: OpenAI-compatible chat completions; LM Studio is the reference runtime.
- Default review deadline: `7200` seconds, configurable.
- Default phase shares: planning `10`, initial investigation `60`, follow-up `20`, finalization/publishing `10`; shares must total `100`.
- Default specialist concurrency: `1`, configurable.
- Review quality takes priority over throughput inside the hard run deadline.
- One logical specialist retains one compactable transcript and one lifetime budget ledger.
- Coverage feedback resumes the same conversation; reconstruction requires a recorded recovery reason.
- Models may group mandatory obligations but cannot remove, satisfy, weaken, or grant budget to them.
- Current-head repository policy is authoritative after schema and network-safety validation.
- Web search is discovery; only content fetched under the current policy can become evidence.
- The sticky comment is a sparse human-review handoff, never the detailed finding database.
- `review_comment` is the default specialist publication mode; detailed notes use resolvable line/file review threads when possible.
- No new third-party Python dependency is introduced.
- Existing user changes and unrelated untracked files must remain untouched.

---

## File and responsibility map

### New runtime package

- `pr_reviewer/specialist_runtime/types.py` — enums and immutable cross-component dataclasses.
- `pr_reviewer/specialist_runtime/budget.py` — direct lifetime counters, phase deadlines, leases, and reserve enforcement.
- `pr_reviewer/specialist_runtime/events.py` — append-only run events and deterministic artifact projection.
- `pr_reviewer/specialist_runtime/policy.py` — runtime configuration, version-2 repository policy, version-1 migration, and source rules.
- `pr_reviewer/specialist_runtime/coverage.py` — analyzer-derived obligations, recipe obligations, satisfaction, and recipe status projection.
- `pr_reviewer/specialist_runtime/assignments.py` — planner payload/schema, validation, overlap rules, repair, and deterministic fallback.
- `pr_reviewer/specialist_runtime/evidence.py` — content-addressed evidence store and immutable wave snapshots.
- `pr_reviewer/specialist_runtime/web_evidence.py` — search-provider filtering, source-access requests, secure fetch, redirect/DNS guards.
- `pr_reviewer/specialist_runtime/model_gateway.py` — role-specific OpenAI-compatible calls over existing transport.
- `pr_reviewer/specialist_runtime/session.py` — checkpoint/resume/recovery/finalization state machine.
- `pr_reviewer/specialist_runtime/scheduler.py` — deterministic sequential/concurrent wave execution and cancellation.
- `pr_reviewer/specialist_runtime/negotiation.py` — bounded next-action proposals and validation.
- `pr_reviewer/specialist_runtime/adjudication.py` — candidate reconciliation, finalizer input, verdict policy, handoff, and review-note production.
- `pr_reviewer/specialist_runtime/controller.py` — end-to-end `ReviewRun` orchestration and terminal degradation.
- `pr_reviewer/specialist_runtime/cli.py` — environment/file adapter used by the action script.

### New publication and migration files

- `pr_reviewer/github_review_notes.py` — pure anchor selection, note body, fingerprint, and GraphQL variable construction.
- `scripts/publish_specialist_review.py` — GitHub sticky handoff, pending review threads, submit event, replies, and resolution.
- `docs/migrations/specialist-session-runtime.md` — downstream-project agent handoff and changelog.
- `tests/fixtures/specialist_runtime/` — recorded provider turns and representative replay inputs.

### Existing files retained or adapted

- `pr_reviewer/conversation.py`, `pr_reviewer/transport.py`, `pr_reviewer/sse_reassembler.py`, `pr_reviewer/stream_watchdog.py`, and `pr_reviewer/tool_executors.py` remain hardened adapters.
- `pr_reviewer/specialists.py` temporarily supplies topology/file-role helpers, then becomes a compatibility facade.
- `scripts/run_specialist_reviews.py` becomes a thin call to `specialist_runtime.cli.main`.
- `scripts/build_review_comments.py` and `scripts/resolve_finding_threads.py` become compatibility wrappers around `github_review_notes.py` and the new publisher before removal.
- `scripts/sections/config.sh`, `scripts/sections/corpus.sh`, `scripts/sections/review.sh`, `action.yml`, `README.md`, and example workflows receive the new configuration/output contract.

---

### Task 1: Runtime domain types, direct budgets, and event artifact

**Files:**
- Create: `pr_reviewer/specialist_runtime/__init__.py`
- Create: `pr_reviewer/specialist_runtime/types.py`
- Create: `pr_reviewer/specialist_runtime/budget.py`
- Create: `pr_reviewer/specialist_runtime/events.py`
- Test: `tests/test_specialist_runtime_state.py`

**Interfaces:**
- Produces: `RunPhase`, `SessionState`, `ObligationStatus`, `RecipeStatus`, `ReviewNoteKind`, `CoverageObligation`, `SpecialistAssignment`, `SessionCheckpoint`, `CandidateFinding`, `ReviewHandoff`, `ReviewNote`, `BudgetLimits`, `BudgetUsage`, `BudgetLedger`, `PhaseShares`, `RunDeadline`, `RunEvent`, and `RunArtifactProjector`.
- Consumes: Python standard library only.

- [ ] **Step 1: Write failing state and budget tests**

```python
from dataclasses import replace

import pytest

from pr_reviewer.specialist_runtime.budget import BudgetExceeded, BudgetLedger, RunDeadline
from pr_reviewer.specialist_runtime.events import RunArtifactProjector, RunEvent
from pr_reviewer.specialist_runtime.types import BudgetLimits, PhaseShares, RunPhase


def test_lifetime_budget_never_resets_on_recovery():
    ledger = BudgetLedger(BudgetLimits(model_turns=4, tool_calls=3, recoveries=1))
    ledger.record_model_turn(input_tokens=10, output_tokens=5)
    ledger.reserve_tool_calls(2)
    ledger.record_recovery("repetitive-transcript")
    snapshot = ledger.snapshot()
    assert snapshot.model_turns == 1
    assert snapshot.tool_calls == 2
    assert snapshot.recoveries == 1
    with pytest.raises(BudgetExceeded):
        ledger.record_recovery("context-pressure")


def test_phase_shares_must_total_one_hundred():
    with pytest.raises(ValueError, match="total 100"):
        PhaseShares(planning=10, initial=60, followup=20, finalization=9)


def test_artifact_projection_is_event_order_deterministic():
    events = [
        RunEvent(sequence=1, kind="run_started", payload={"head_sha": "abc"}),
        RunEvent(sequence=2, kind="phase_changed", payload={"phase": RunPhase.PLANNING.value}),
    ]
    assert RunArtifactProjector().project(events)["phase"] == "planning"
```

- [ ] **Step 2: Run the focused test and confirm import failure**

Run: `pytest tests/test_specialist_runtime_state.py -v`

Expected: FAIL during collection because `pr_reviewer.specialist_runtime` does not exist.

- [ ] **Step 3: Implement the exact domain and budget API**

Use string enums for serialized state and frozen dataclasses for values. `BudgetLedger` is the only mutable budget owner. Implement these public methods with the stated behavior:

- `__init__(limits: BudgetLimits)`: initialize zero lifetime usage and validate positive limits.
- `record_model_turn(*, input_tokens: int = 0, output_tokens: int = 0)`: atomically consume one model turn and the supplied non-negative token counts, raising `BudgetExhausted` before mutation when a limit would be exceeded.
- `reserve_tool_calls(count: int)`: atomically consume a positive number of tool-call slots before execution, raising `BudgetExhausted` before mutation when unavailable.
- `record_tool_rejection(reason: str)`: increment the rejection counter and retain the normalized reason in the next snapshot.
- `record_no_progress()`: increment and return the consecutive no-progress streak.
- `reset_no_progress_streak(reason: str)`: clear only the consecutive streak and record the reset reason; never change lifetime counters.
- `record_recovery(reason: str)`: consume one recovery and retain its reason, raising before mutation if the lifetime recovery limit is exhausted.
- `snapshot() -> BudgetUsage`: return an immutable copy of all lifetime counters and diagnostic reasons.
- `remaining_model_turns() -> int` and `remaining_tool_calls() -> int`: return non-negative lifetime remainders without modifying state.

`RunDeadline` accepts `started_at`, `deadline_sec`, and `PhaseShares`, computes absolute monotonic cutoffs, lets unused time flow forward, and refuses exploration work at the finalization cutoff. `RunArtifactProjector.project(events)` sorts by sequence and raises on duplicate or missing sequence numbers.

- [ ] **Step 4: Run state tests**

Run: `pytest tests/test_specialist_runtime_state.py -v`

Expected: PASS.

- [ ] **Step 5: Run existing conversation/tool-loop regressions**

Run: `pytest tests/test_conversation.py tests/test_native_tool_loop.py -q`

Expected: PASS; the new package has not changed existing wire behavior.

- [ ] **Step 6: Commit**

```bash
git add pr_reviewer/specialist_runtime tests/test_specialist_runtime_state.py
git commit -m "feat(runtime): add specialist state and lifetime budgets"
```

---

### Task 2: Versioned runtime configuration and current-branch review policy

**Files:**
- Create: `pr_reviewer/specialist_runtime/policy.py`
- Test: `tests/test_specialist_runtime_policy.py`
- Modify: `pr_reviewer/specialists.py`

**Interfaces:**
- Consumes: types from Task 1 and existing `classify_file_roles`, `_posix`, `_slug`, and safe repository-path conventions.
- Produces: `RuntimeConfig.from_env(env)`, `ReviewPolicy`, `SourceRule`, `RecipePolicy`, `load_review_policy(path, legacy_path=None)`, and `migrate_v1_policy(data)`.

- [ ] **Step 1: Write failing policy tests**

```python
import json

import pytest

from pr_reviewer.specialist_runtime.policy import load_review_policy


def test_v1_recipe_defaults_to_coverage_and_remains_named(tmp_path):
    path = tmp_path / "specialists.json"
    path.write_text(json.dumps({
        "version": 1,
        "components": [{"id": "worker", "paths": ["worker/**"]}],
        "recipes": [{
            "id": "delivery", "match": {"file_roles_any": ["messaging"]},
            "title": "Delivery", "objective": "Trace retries",
        }],
        "exclude": {"paths": [], "components": [], "lenses": [], "recipes": []},
    }))
    policy = load_review_policy(path)
    assert policy.version == 2
    assert policy.recipes[0].id == "delivery"
    assert policy.recipes[0].execution == "coverage"


def test_source_rules_reject_global_wildcard_and_http(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 2,
        "sources": [{"host": "*", "schemes": ["http"]}],
    }))
    with pytest.raises(ValueError, match="source rule"):
        load_review_policy(path)
```

- [ ] **Step 2: Run the tests to verify missing policy API**

Run: `pytest tests/test_specialist_runtime_policy.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement policy parsing and v1 migration**

Version 2 accepts these top-level keys and rejects unknown security-sensitive keys:

```json
{
  "version": 2,
  "components": [],
  "recipes": [],
  "coverage_rules": [],
  "sources": [],
  "generated_artifacts": [],
  "verdict_policy": {},
  "publishing": {},
  "exclude": {}
}
```

Recipe `execution` is exactly `coverage`, `dedicated`, or `independent`. Source rules require a concrete lowercase host and support `include_subdomains`, `path_prefixes`, `classification`, and `max_age_hours`; only HTTPS is allowed. Keep `pr_reviewer.specialists.load_specialist_config` as a warning compatibility facade that returns the migrated policy's legacy projection until Task 16 removes old callers.

- [ ] **Step 4: Add runtime environment parsing tests**

Test exact defaults and aliases:

```python
def test_runtime_config_uses_direct_defaults_and_legacy_aliases():
    config = RuntimeConfig.from_env({
        "SPECIALIST_MAX_TOOL_CALLS_PER_PASS": "17",
        "SPECIALIST_PHASE_SHARES": '{"planning":10,"initial":60,"followup":20,"finalization":10}',
    })
    assert config.review_deadline_sec == 7200
    assert config.concurrency == 1
    assert config.session_limits.tool_calls == 17
    assert config.deprecation_warnings == ("specialist_max_tool_calls_per_pass",)
```

- [ ] **Step 5: Run policy tests**

Run: `pytest tests/test_specialist_runtime_policy.py tests/test_specialists.py -q`

Expected: PASS, including v1 compatibility.

- [ ] **Step 6: Commit**

```bash
git add pr_reviewer/specialist_runtime/policy.py pr_reviewer/specialists.py tests/test_specialist_runtime_policy.py
git commit -m "feat(runtime): load versioned review policy"
```

---

### Task 3: Deterministic topology obligations and recipe accounting

**Files:**
- Create: `pr_reviewer/specialist_runtime/coverage.py`
- Test: `tests/test_specialist_runtime_coverage.py`
- Modify: `pr_reviewer/specialists.py`

**Interfaces:**
- Consumes: migrated `ReviewPolicy`, existing `build_topology`, `classify_file_roles`, PR classification, and changed files.
- Produces: `derive_obligations(topology, classification, policy) -> tuple[CoverageObligation, ...]`, `CoverageLedger`, `evaluate_coverage`, and `recipe_statuses`.

- [ ] **Step 1: Write failing obligation tests**

```python
from pr_reviewer.specialist_runtime.coverage import derive_obligations
from pr_reviewer.specialist_runtime.policy import ReviewPolicy, RecipePolicy


def test_matching_recipe_becomes_named_mandatory_obligations():
    policy = ReviewPolicy.minimal(recipes=(RecipePolicy(
        id="delivery", title="Delivery", objective="Trace retry",
        execution="coverage", match={"file_roles_any": ["messaging"]},
        expected_evidence=("producer", "consumer", "tests"),
    ),))
    topology = {
        "changed_files": ["worker/messaging/consumer.py"],
        "file_roles": ["messaging", "implementation"],
        "components": [{"id": "worker", "changed_files": ["worker/messaging/consumer.py"]}],
    }
    obligations = derive_obligations(topology, {}, policy)
    recipe_items = [item for item in obligations if item.recipe_id == "delivery"]
    assert {item.required_evidence for item in recipe_items} == {
        ("producer",), ("consumer",), ("tests",)
    }
    assert all(item.mandatory for item in recipe_items)
```

- [ ] **Step 2: Run the test and verify failure**

Run: `pytest tests/test_specialist_runtime_coverage.py -v`

Expected: FAIL because `coverage.py` is missing.

- [ ] **Step 3: Implement deterministic derivation**

Generate stable IDs from `origin`, `subject`, and evidence category. Built-in rules must cover changed implementation, relevant tests when present, changed schema propagation, messaging delivery, persistence/migration consistency, deployment artifacts, deterministic risk flags, and multi-component interactions. Recipe exclusions become `suppressed_by_policy`; non-matching recipes become `not_applicable`. Do not use model output in applicability or obligation IDs.

- [ ] **Step 4: Add satisfaction and recipe-status tests**

```python
def test_recipe_is_partial_until_every_obligation_has_evidence():
    ledger = CoverageLedger(obligations)
    ledger.attach_evidence(recipe_items[0].id, "E1")
    assert ledger.recipe_statuses()["delivery"] == "partially_covered"
    for item in recipe_items[1:]:
        ledger.attach_evidence(item.id, f"E-{item.id}")
    assert ledger.recipe_statuses()["delivery"] == "covered"
```

- [ ] **Step 5: Run coverage and existing classifier tests**

Run: `pytest tests/test_specialist_runtime_coverage.py tests/test_specialists.py tests/test_classifier.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pr_reviewer/specialist_runtime/coverage.py pr_reviewer/specialists.py tests/test_specialist_runtime_coverage.py
git commit -m "feat(runtime): derive mandatory review obligations"
```

---

### Task 4: Model grouping, assignment validation, overlap, and fallback

**Files:**
- Create: `pr_reviewer/specialist_runtime/assignments.py`
- Test: `tests/test_specialist_runtime_assignments.py`

**Interfaces:**
- Consumes: immutable obligations, topology, `RuntimeConfig`, and planner JSON.
- Produces: `planner_prompt`, `validate_assignment_plan`, `repair_prompt`, `fallback_assignment_plan`, and `AssignmentPlan`.

- [ ] **Step 1: Write failing assignment tests**

```python
import pytest

from pr_reviewer.specialist_runtime.assignments import AssignmentPlanError, validate_assignment_plan


def test_planner_cannot_omit_recipe_obligation(obligations, topology, runtime_config):
    raw = {"assignments": [{
        "id": "worker-flow", "title": "Worker flow",
        "objective": "Trace the worker", "obligation_ids": [obligations[0].id],
        "lenses": ["delivery"], "seed_paths": ["worker/a.py"],
        "expected_evidence": ["consumer"], "overlap_justification": "",
    }]}
    with pytest.raises(AssignmentPlanError, match="unassigned mandatory"):
        validate_assignment_plan(raw, obligations, topology, runtime_config)


def test_model_created_focus_preserves_recipe_identity(obligations, topology, runtime_config):
    raw = complete_plan_for(obligations, id="queue-loss-boundary")
    plan = validate_assignment_plan(raw, obligations, topology, runtime_config)
    assert "delivery" in plan.assignments[0].recipe_ids
```

- [ ] **Step 2: Verify tests fail**

Run: `pytest tests/test_specialist_runtime_assignments.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement planner schema and validation**

Require each assignment to include `id`, `title`, `objective`, `obligation_ids`, `lenses`, `seed_paths`, `boundary_paths`, `expected_evidence`, `estimated_turns`, `priority`, and `overlap_justification`. Validation enforces complete mandatory set coverage, `dedicated` recipes in distinct assignments, `independent` recipes in isolated assignments, one primary owner, justified shared ownership, session caps, and estimated deadline feasibility.

- [ ] **Step 4: Implement one bounded repair and deterministic fallback**

`repair_prompt` lists only validation errors and the previous plan. `fallback_assignment_plan` groups obligations by component plus cross-component boundary, preserves dedicated/independent requirements, sorts high risk first, and never discards mandatory obligations; obligations beyond hard session capacity remain explicitly unassigned for risk policy.

- [ ] **Step 5: Run assignment tests**

Run: `pytest tests/test_specialist_runtime_assignments.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pr_reviewer/specialist_runtime/assignments.py tests/test_specialist_runtime_assignments.py
git commit -m "feat(runtime): validate model-planned specialist assignments"
```

---

### Task 5: Content-addressed evidence and immutable wave snapshots

**Files:**
- Create: `pr_reviewer/specialist_runtime/evidence.py`
- Test: `tests/test_specialist_runtime_evidence.py`

**Interfaces:**
- Produces: `EvidenceStore.add`, `EvidenceStore.import_into_session`, `EvidenceStore.lookup_canonical`, `EvidenceStore.snapshot`, `EvidenceSnapshot`, and `canonical_evidence_key`.
- Consumes: existing redaction helpers and Task 1 evidence dataclasses.

- [ ] **Step 1: Write failing evidence tests**

```python
from pr_reviewer.specialist_runtime.evidence import EvidenceStore


def test_duplicate_success_reuses_evidence_without_claiming_independence():
    store = EvidenceStore()
    first = store.add_tool_result(
        session_id="S1", tool="read_file", arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
    )
    second = store.add_tool_result(
        session_id="S2", tool="read_file", arguments={"path": "a.py"},
        result={"status": "ok", "result": {"content": "x = 1"}},
    )
    assert second.id == first.id
    assert second.collector_session_id == "S1"
    assert "S2" in second.imported_by
```

- [ ] **Step 2: Verify failure, then implement canonical storage**

Run: `pytest tests/test_specialist_runtime_evidence.py -v`

Expected before implementation: import failure. Canonical keys use sorted JSON arguments, normalized source identity, status, and bounded content hash. Failed calls are recorded but do not satisfy obligations.

- [ ] **Step 3: Add immutable snapshot test**

```python
def test_wave_snapshot_does_not_change_when_store_grows():
    snapshot = store.snapshot()
    store.add_tool_result(
        session_id="S3", tool="read_file", arguments={"path": "b.py"},
        result={"status": "ok", "result": {"content": "y = 2"}},
    )
    assert snapshot.get_by_path("b.py") == ()
```

- [ ] **Step 4: Run evidence and redaction tests**

Run: `pytest tests/test_specialist_runtime_evidence.py tests/test_redact.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pr_reviewer/specialist_runtime/evidence.py tests/test_specialist_runtime_evidence.py
git commit -m "feat(runtime): add provenance-backed evidence store"
```

---

### Task 6: Allowlisted web discovery, access requests, and secure fetch

**Files:**
- Create: `pr_reviewer/specialist_runtime/web_evidence.py`
- Modify: `pr_reviewer/tool_executors.py`
- Modify: `pr_reviewer/conversation.py`
- Test: `tests/test_specialist_runtime_web.py`
- Modify test: `tests/test_web_search.py`

**Interfaces:**
- Consumes: `ReviewPolicy.sources`, `EvidenceStore`, fixed `SEARCH_URL`, and existing masking/truncation.
- Produces: `SearchProvider`, `SearxngSearchProvider`, `SourcePolicy.classify`, `SecureFetcher.fetch`, `SearchDiscovery`, and `SourceAccessRequest`.

- [ ] **Step 1: Write failing discovery-filter tests**

```python
def test_search_returns_snippets_only_for_approved_sources():
    provider = FakeSearchProvider([
        SearchCandidate("Official", "https://docs.example.com/api", "trusted snippet"),
        SearchCandidate("Blog", "https://blog.invalid/post", "unapproved content"),
    ])
    result = discover("api behavior", provider, source_policy("docs.example.com"))
    assert result.approved[0].snippet == "trusted snippet"
    assert result.unapproved[0].host == "blog.invalid"
    assert result.unapproved[0].snippet is None
    assert "unapproved content" not in result.to_tool_result()


def test_unapproved_candidate_creates_request_without_fetching():
    request = source_access_request(result.unapproved[0], "OB-model-api", "verify support")
    assert request.host == "blog.invalid"
    assert request.obligation_id == "OB-model-api"
```

- [ ] **Step 2: Run tests and confirm missing module**

Run: `pytest tests/test_specialist_runtime_web.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement provider and policy filtering**

The provider scans at most `search_scan_limit` results and returns at most `tool_max_search_results` approved candidates plus minimal unapproved metadata. Query validation applies secret masking, maximum length, credential-pattern rejection, and high-entropy token rejection. The model never controls the provider URL.

- [ ] **Step 4: Add redirect and DNS tests before implementing fetch**

```python
@pytest.mark.parametrize("target", [
    "https://127.0.0.1/x", "https://169.254.169.254/latest/meta-data",
    "https://[::1]/x", "http://docs.example.com/x",
])
def test_secure_fetch_rejects_private_or_non_https_target(target):
    with pytest.raises(SourceDenied):
        fetcher.fetch(target)


def test_redirect_must_remain_allowlisted():
    transport = FakeHttpTransport.redirecting(
        "https://docs.example.com/a", "https://evil.example/b"
    )
    with pytest.raises(SourceDenied, match="redirect"):
        SecureFetcher(policy, transport=transport, resolver=public_resolver).fetch(
            "https://docs.example.com/a"
        )
```

- [ ] **Step 5: Implement secure fetch**

Resolve every request and redirect host before connecting; reject loopback, private, link-local, multicast, reserved, unspecified, and metadata-service addresses with `ipaddress`. Enforce HTTPS, approved ports, host/path rules, redirect cap, byte cap, MIME allowlist, timeout, no cookies/credentials, HTML-to-text normalization, masking, and provenance containing original/final URL, retrieval time, content hash, MIME type, truncation, and policy hash.

- [ ] **Step 6: Replace raw search tool results**

Keep the tool names `web_search` and `web_fetch`, but inject `SourcePolicy` and return typed discovery/evidence JSON. Advertise `web_search` only when a fixed provider URL exists and approved source rules are non-empty.

- [ ] **Step 7: Run web/security regressions**

Run: `pytest tests/test_specialist_runtime_web.py tests/test_web_search.py tests/test_native_loop_exfil_redteam.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add pr_reviewer/specialist_runtime/web_evidence.py pr_reviewer/tool_executors.py pr_reviewer/conversation.py tests/test_specialist_runtime_web.py tests/test_web_search.py
git commit -m "feat(runtime): separate web discovery from approved evidence"
```

---

### Task 7: OpenAI-compatible role model gateway

**Files:**
- Create: `pr_reviewer/specialist_runtime/model_gateway.py`
- Test: `tests/test_specialist_runtime_model_gateway.py`
- Modify: `scripts/run_specialist_reviews.py`

**Interfaces:**
- Consumes: `Conversation.to_request_payload`, `run_chat_request`, `StreamWatchdog`, role-specific model configuration, response schema, and absolute request deadline.
- Produces: `ModelGateway.complete(request: ModelTurnRequest) -> ModelTurnResult` and `OpenAIModelGateway`.

- [ ] **Step 1: Write failing gateway tests**

```python
def test_role_model_override_and_deadline_bound_timeout(monkeypatch):
    calls = []
    gateway = OpenAIModelGateway(
        base_url="http://model/v1", api_key="secret", default_model="main",
        role_models={"planner": "plan-model"}, transport=lambda *a, **k: calls.append((a, k)) or stop_response("{}"),
    )
    result = gateway.complete(ModelTurnRequest(
        role="planner", conversation=conversation(), max_tokens=512,
        response_schema={"type": "object"}, tools_enabled=False,
        timeout_sec=20, stream=False,
    ))
    assert calls[0][0][2]["model"] == "plan-model"
    assert calls[0][0][4] == 20
    assert result.finish_reason == "stop"
```

- [ ] **Step 2: Run and verify import failure**

Run: `pytest tests/test_specialist_runtime_model_gateway.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Extract gateway behavior from `SequentialModelRunner`**

Move role selection, structured-output fallback, streamed/non-streamed retry, usage collection, request diagnostics, and watchdog integration into `OpenAIModelGateway`. Do not move session lifecycle, tool execution, evidence, or finalization policy into the gateway. Restrict the new runtime to `api_format="openai"`; fail configuration before the first request for another format.

- [ ] **Step 4: Run gateway and transport tests**

Run: `pytest tests/test_specialist_runtime_model_gateway.py tests/test_transport_stream_watchdog.py tests/test_sse_reassembler.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pr_reviewer/specialist_runtime/model_gateway.py scripts/run_specialist_reviews.py tests/test_specialist_runtime_model_gateway.py
git commit -m "refactor(runtime): extract OpenAI role model gateway"
```

---

### Task 8: Continuous specialist session checkpoint/resume/recovery state machine

**Files:**
- Create: `pr_reviewer/specialist_runtime/session.py`
- Test: `tests/test_specialist_runtime_session.py`
- Modify: `pr_reviewer/tool_loop.py`

**Interfaces:**
- Consumes: assignment, `Conversation`, `ModelGateway`, read-only tool executor, `EvidenceStore`, `CoverageLedger`, `BudgetLedger`, and session lease.
- Produces: `SpecialistSession.explore`, `request_checkpoint`, `apply_coverage_feedback`, `recover`, `finalize`, and `SessionResult`.

- [ ] **Step 1: Write the failing continuity regression**

```python
def test_coverage_feedback_resumes_same_conversation_and_budget():
    gateway = ScriptedGateway([
        tool_call_response("read_file", {"path": "a.py"}),
        checkpoint_response(inspected=["a.py"], unresolved=["OB-tests"]),
        tool_call_response("read_file", {"path": "tests/test_a.py"}),
        checkpoint_response(inspected=["a.py", "tests/test_a.py"], unresolved=[]),
    ])
    session = make_session(gateway, tool_calls=4, model_turns=8)
    first = session.explore()
    conversation_identity = id(session.conversation)
    session.apply_coverage_feedback(["OB-tests"])
    second = session.explore()
    assert id(session.conversation) == conversation_identity
    assert second.budget.model_turns == 4
    assert second.budget.tool_calls == 2
    assert gateway.requests[2].messages_contain("tests/test_a.py") is False
    assert gateway.requests[2].messages_contain("a.py") is True
```

- [ ] **Step 2: Run and verify failure**

Run: `pytest tests/test_specialist_runtime_session.py::test_coverage_feedback_resumes_same_conversation_and_budget -v`

Expected: FAIL because `SpecialistSession` is missing.

- [ ] **Step 3: Implement exploration and checkpoint transitions**

Use native tool calls, append every assistant call and tool result to the same `Conversation`, register successful results in `EvidenceStore`, and attach evidence IDs to checkpoint obligations. A no-tool response requests or parses a checkpoint; no-progress protection requests a checkpoint instead of a final report. Material controller feedback appends a user event and resets only the consecutive no-progress streak.

- [ ] **Step 4: Write recovery tests before implementation**

```python
def test_recovery_reconstructs_context_without_resetting_lifetime_state():
    session = make_session(repetitive_gateway(), tool_calls=5, model_turns=10, recoveries=1)
    session.explore()
    old_conversation = session.conversation
    checkpoint = session.latest_checkpoint
    session.recover("repetitive-transcript")
    assert session.conversation is not old_conversation
    assert session.latest_checkpoint == checkpoint
    assert session.budget.snapshot().recoveries == 1
    assert session.budget.snapshot().tool_calls > 0
    assert session.conversation_contains_evidence_ids(checkpoint.evidence_ids)
```

- [ ] **Step 5: Implement compaction, recovery, and one-time finalization**

Reuse `Conversation` compaction helpers before reconstruction. Recovery input contains immutable assignment, latest valid checkpoint, evidence bodies bounded by policy, current gaps, and remaining lifetime budget. `finalize()` disables tools, allows one schema repair, and falls back to the structured checkpoint projection. A second finalization call returns the existing result without a model request.

- [ ] **Step 6: Run session, conversation, and tool-loop tests**

Run: `pytest tests/test_specialist_runtime_session.py tests/test_conversation.py tests/test_native_tool_loop.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pr_reviewer/specialist_runtime/session.py pr_reviewer/tool_loop.py tests/test_specialist_runtime_session.py
git commit -m "feat(runtime): preserve continuous specialist sessions"
```

---

### Task 9: Deadline-aware scheduler and deterministic concurrency

**Files:**
- Create: `pr_reviewer/specialist_runtime/scheduler.py`
- Test: `tests/test_specialist_runtime_scheduler.py`

**Interfaces:**
- Consumes: validated assignments, session factory, immutable evidence/coverage snapshot, `RunDeadline`, concurrency, and event sink.
- Produces: `SessionScheduler.run_wave(assignments, phase) -> WaveResult`.

- [ ] **Step 1: Write failing order/concurrency tests**

```python
def test_wave_merge_is_independent_of_completion_order():
    slow_first = scheduler_with_completion_order(["S2", "S1"])
    fast_first = scheduler_with_completion_order(["S1", "S2"])
    assert slow_first.run_wave(assignments).coverage_projection == fast_first.run_wave(assignments).coverage_projection
    assert slow_first.run_wave(assignments).evidence_ids == fast_first.run_wave(assignments).evidence_ids


def test_finalization_reserve_blocks_new_exploration():
    deadline = fake_deadline(now=90, finalization_starts=90, ends=100)
    scheduler = SessionScheduler(concurrency=2, deadline=deadline, session_factory=factory)
    result = scheduler.run_wave(assignments, RunPhase.INITIAL)
    assert result.not_started == tuple(a.id for a in assignments)
```

- [ ] **Step 2: Verify failure and implement scheduler**

Run: `pytest tests/test_specialist_runtime_scheduler.py -v`

Expected before implementation: import failure. Use `ThreadPoolExecutor(max_workers=concurrency)`, pass one immutable wave-start snapshot to every session, collect by assignment ID, and merge in stable priority/ID order rather than completion order.

- [ ] **Step 3: Add cancellation and request-lease tests**

Assert high-risk assignments start first, pending low-risk sessions are cancelled at phase cutoff, and request timeouts are `min(configured_timeout, remaining_phase_seconds)`.

- [ ] **Step 4: Run scheduler tests**

Run: `pytest tests/test_specialist_runtime_scheduler.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pr_reviewer/specialist_runtime/scheduler.py tests/test_specialist_runtime_scheduler.py
git commit -m "feat(runtime): schedule deterministic specialist waves"
```

---

### Task 10: Coverage reconciliation and bounded negotiator follow-ups

**Files:**
- Create: `pr_reviewer/specialist_runtime/negotiation.py`
- Modify: `pr_reviewer/specialist_runtime/coverage.py`
- Test: `tests/test_specialist_runtime_negotiation.py`

**Interfaces:**
- Consumes: checkpoints, evidence/coverage ledger, assignments, remaining budget/deadline, and negotiator JSON.
- Produces: `reconcile_wave`, `NegotiationProposal`, `validate_negotiation`, and `fallback_next_action`.

- [ ] **Step 1: Write failing negotiator authority tests**

```python
@pytest.mark.parametrize("action", ["delete_obligation", "mark_covered", "grant_budget"])
def test_negotiator_cannot_change_controller_authority(action):
    raw = {"actions": [{"kind": action, "obligation_ids": ["OB1"]}]}
    with pytest.raises(NegotiationError):
        validate_negotiation(raw, state)


def test_resume_is_preferred_when_owner_has_useful_remaining_budget():
    proposal = validate_negotiation({"actions": [{
        "kind": "resume", "session_id": "S1", "obligation_ids": ["OB1"],
        "expected_evidence": ["tests"], "estimated_turns": 2,
        "reason": "owner inspected implementation but not tests",
    }]}, state)
    assert proposal.actions[0].session_id == "S1"
```

- [ ] **Step 2: Run tests and implement reconciliation/proposal validation**

Run: `pytest tests/test_specialist_runtime_negotiation.py -v`

Expected before implementation: import failure. Reconciliation uses deterministic evidence predicates. Negotiator actions are exactly `resume`, `consult`, `new_session`, or `record_unknown`; require expected new evidence and positive coverage gain.

- [ ] **Step 3: Implement deterministic fallback**

Resume the primary owner of the highest-risk uncovered obligation when it has budget and lease; otherwise create one narrow session if session capacity remains; otherwise record the policy-governed unknown.

- [ ] **Step 4: Run negotiation and coverage tests**

Run: `pytest tests/test_specialist_runtime_negotiation.py tests/test_specialist_runtime_coverage.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pr_reviewer/specialist_runtime/negotiation.py pr_reviewer/specialist_runtime/coverage.py tests/test_specialist_runtime_negotiation.py
git commit -m "feat(runtime): negotiate bounded coverage follow-ups"
```

---

### Task 11: Candidate adjudication, risk verdict policy, sparse handoff, and review notes

**Files:**
- Create: `pr_reviewer/specialist_runtime/adjudication.py`
- Test: `tests/test_specialist_runtime_adjudication.py`
- Modify: `pr_reviewer/enforcement.py`

**Interfaces:**
- Consumes: session candidate findings, evidence store, coverage/recipe status, critic/finalizer results, repository verdict policy, and publishing mode.
- Produces: `adjudicate_candidates`, `apply_runtime_verdict_policy`, `build_review_handoff`, `build_review_notes`, and `AdjudicatedReview`.

- [ ] **Step 1: Write failing evidence-authority tests**

```python
def test_critic_cannot_publish_candidate_without_retained_evidence():
    candidate = candidate_finding(evidence_ids=("MISSING",))
    review = adjudicate_candidates([candidate], critic_keep(candidate.id), empty_evidence_store())
    assert review.accepted == ()
    assert review.rejected[0].reason == "missing-retained-evidence"


def test_high_risk_unresolved_obligation_blocks_by_policy():
    result = apply_runtime_verdict_policy(
        model_verdict="approve", accepted=(),
        unresolved=(high_risk_obligation(block_when_unresolved=True),),
        allow_approve=True,
    )
    assert result.verdict == "request_changes"
    assert result.source == "incomplete-high-risk-coverage"
```

- [ ] **Step 2: Run tests and implement candidate normalization/adjudication**

Run: `pytest tests/test_specialist_runtime_adjudication.py -v`

Expected before implementation: import failure. Root-cause fingerprints use normalized changed causal file, category, and claim. The critic may keep, reject, merge, request verification, or downgrade to unknown, but cannot synthesize evidence.

- [ ] **Step 3: Write sparse handoff tests**

```python
def test_handoff_omits_per_finding_and_empty_sections():
    findings = [finding("DB connection leak"), finding("Transaction retries duplicate writes")]
    handoff = build_review_handoff(run_state(findings=findings, unknowns=(), source_requests=()))
    assert "DB connection leak" not in handoff.markdown
    assert "Transaction retries duplicate writes" not in handoff.markdown
    assert "database" in handoff.markdown.lower()
    assert "Unknowns" not in handoff.markdown


def test_unrelated_findings_do_not_get_artificial_theme():
    handoff = build_review_handoff(run_state(findings=[finding("Auth"), finding("Cache")]))
    assert handoff.finding_theme is None
```

- [ ] **Step 4: Implement `ReviewHandoff` and typed `ReviewNote` production**

The handoff contains change map, AI coverage/focus, optional one-line thread status, optional genuine aggregate theme, at most three review-emphasis areas, optional material coverage warning, and optional access-request count/link. Omit empty sections and every individual claim/evidence item. Notes are `finding`, `verification_request`, or `source_access_request`, each with stable fingerprint and related obligation/evidence IDs.

- [ ] **Step 5: Run adjudication and enforcement tests**

Run: `pytest tests/test_specialist_runtime_adjudication.py tests/test_enforcement.py tests/test_verdict_safety.sh -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pr_reviewer/specialist_runtime/adjudication.py pr_reviewer/enforcement.py tests/test_specialist_runtime_adjudication.py
git commit -m "feat(runtime): adjudicate evidence and build review handoff"
```

---

### Task 12: GitHub line/file review notes, general fallback, replies, and resolution

**Files:**
- Create: `pr_reviewer/github_review_notes.py`
- Create: `scripts/publish_specialist_review.py`
- Test: `tests/test_github_review_notes.py`
- Modify: `scripts/build_review_comments.py`
- Modify: `scripts/resolve_finding_threads.py`
- Modify: `scripts/publish_helpers.sh`

**Interfaces:**
- Consumes: `ReviewHandoff`, `ReviewNote[]`, PR diff/files, prior managed threads/comments, verdict policy, repo/PR/head SHA, and publishing mode.
- Produces: `choose_note_anchor`, `build_review_thread_variables`, `GitHubReviewPublisher.publish`, thread/comment state file, and compatibility wrappers.

- [ ] **Step 1: Write failing anchor tests**

```python
def test_anchor_prefers_changed_line_then_changed_file():
    diff = sample_diff("a.py", added_line=7)
    assert choose_note_anchor(note(file="a.py", line=7), diff).subject_type == "LINE"
    assert choose_note_anchor(note(file="a.py", line=99), diff).subject_type == "FILE"


def test_unanchored_finding_becomes_verification_request():
    normalized = normalize_note(note(kind="finding", file=None, line=None))
    assert normalized.kind == "verification_request"
    assert normalized.anchor is None
```

- [ ] **Step 2: Run tests and implement pure note/GraphQL builders**

Run: `pytest tests/test_github_review_notes.py -v`

Expected before implementation: import failure. Build GraphQL variables for `subjectType: LINE` with `path`, `line`, `side: RIGHT`, and `subjectType: FILE` with `path`. General verification/source requests use managed issue comments only when no honest file anchor exists.

- [ ] **Step 3: Add mocked publisher lifecycle tests**

Test this exact sequence for `review_comment`: update sticky handoff, query managed threads, reply/resolve existing fingerprints, create one pending review, add new line/file threads, submit `COMMENT`, then write IDs/URLs/resolution state. For `review_verdict`, submit `APPROVE` or `REQUEST_CHANGES` after existing approval guards. For `comment`, update only the handoff and artifact links.

- [ ] **Step 4: Implement publisher and compatibility wrappers**

Invoke `gh api graphql` with argument lists, never shell-expanded model text. Use `platform_comment_sticky` for the handoff. Preserve human-resolved threads; do not silently unresolve. Convert existing scripts into wrappers until Task 16 removes dead branches.

- [ ] **Step 5: Run publication/security regressions**

Run: `pytest tests/test_github_review_notes.py tests/test_build_review_comments.py tests/test_resolve_finding_threads.py tests/test_api_key_argv.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add pr_reviewer/github_review_notes.py scripts/publish_specialist_review.py scripts/build_review_comments.py scripts/resolve_finding_threads.py scripts/publish_helpers.sh tests/test_github_review_notes.py
git commit -m "feat(publish): add resolvable specialist review notes"
```

---

### Task 13: Review controller, terminal artifact, and deterministic degradation

**Files:**
- Create: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/specialist_runtime/events.py`
- Test: `tests/test_specialist_runtime_controller.py`

**Interfaces:**
- Consumes: policy, topology/context inputs, coverage engine, planner gateway, assignment validator, scheduler, negotiator, adjudicator, run deadline, and event sink.
- Produces: `ReviewController.run(ReviewInputs) -> ReviewResult` and `specialist-review-artifact.json` projection.

- [ ] **Step 1: Write failing happy-path controller test**

```python
def test_controller_runs_obligations_assignments_sessions_and_finalizer():
    controller = scripted_controller()
    result = controller.run(review_inputs())
    assert result.artifact["evaluation_status"] == "complete"
    assert result.artifact["recipes"]["delivery"]["status"] == "covered"
    assert result.handoff.markdown.startswith("## AI review handoff")
    assert result.notes[0].evidence_ids
    assert result.verdict in {"approve", "request_changes"}
```

- [ ] **Step 2: Run and verify missing controller**

Run: `pytest tests/test_specialist_runtime_controller.py -v`

Expected: FAIL during import.

- [ ] **Step 3: Implement explicit phase orchestration**

Transition through `PRECHECK`, `PLANNING`, `INITIAL`, `FOLLOWUP`, `FINALIZATION`, `PUBLISH_READY`, and `COMPLETE`. Emit events for every model request, session transition, budget change, evidence addition, coverage decision, recipe status, access request, recovery, degradation, candidate disposition, verdict source, and output artifact reference.

- [ ] **Step 4: Add failure-injection tests**

Cover planner failure to deterministic assignments, one specialist failure with reassignment, negotiator failure to deterministic next action, critic failure rejecting ambiguous candidates, finalizer failure to deterministic minimal handoff, and deadline exhaustion preserving the finalization reserve.

- [ ] **Step 5: Implement terminal degradation paths**

Every path returns a valid artifact and controlled `request_changes`/notice result when policy requires. No exception after run creation may erase prior events. Artifact JSON is written atomically through a temporary file and rename.

- [ ] **Step 6: Run controller tests**

Run: `pytest tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_state.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add pr_reviewer/specialist_runtime/controller.py pr_reviewer/specialist_runtime/events.py tests/test_specialist_runtime_controller.py
git commit -m "feat(runtime): orchestrate specialist review runs"
```

---

### Task 14: CLI, action inputs/outputs, and pipeline wiring

**Files:**
- Create: `pr_reviewer/specialist_runtime/cli.py`
- Replace: `scripts/run_specialist_reviews.py`
- Modify: `scripts/sections/config.sh`
- Modify: `scripts/sections/corpus.sh`
- Modify: `scripts/sections/review.sh`
- Modify: `scripts/run_review.sh`
- Modify: `action.yml`
- Test: `tests/test_specialist_runtime_cli.py`
- Modify test: `tests/test_action_inputs.py`
- Modify test: `tests/test_specialist_runner.py`
- Modify test: `tests/test_action_shell_syntax.py`

**Interfaces:**
- Consumes: current workspace files (`pr.json`, `pr-files.json`, `classification.json`, `pr.diff`, corpus, standards), action environment, and current-head policy.
- Produces: `specialist-ai-output.json`, `specialist-review-artifact.json`, `review-handoff.md`, `review-notes.json`, legacy `verdict/review_markdown/findings` outputs, and new `review_handoff/review_notes/specialist_artifact` outputs.

- [ ] **Step 1: Write failing CLI workspace test**

```python
def test_cli_writes_structured_handoff_notes_and_artifact(monkeypatch, tmp_path):
    write_review_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "build_controller", lambda config: scripted_controller())
    assert cli.main() == 0
    assert json.loads((tmp_path / "specialist-ai-output.json").read_text())["verdict"]
    assert (tmp_path / "review-handoff.md").read_text().startswith("## AI review handoff")
    assert isinstance(json.loads((tmp_path / "review-notes.json").read_text()), list)
    assert json.loads((tmp_path / "specialist-review-artifact.json").read_text())["schema_version"] == 2
```

- [ ] **Step 2: Implement CLI and reduce the script to a wrapper**

`scripts/run_specialist_reviews.py` must contain only path bootstrap, import, and `raise SystemExit(main())`. Keep output filenames so shell integration remains incremental.

- [ ] **Step 3: Add and wire exact action inputs**

Declare and document:

```text
review_policy_file=.github/ai-review-policy.json
specialist_review_deadline_sec=7200
specialist_phase_shares={"planning":10,"initial":60,"followup":20,"finalization":10}
specialist_concurrency=1
specialist_max_sessions=8
specialist_max_followup_sessions=2
specialist_max_model_turns_per_session=64
specialist_max_tool_calls_per_session=20
specialist_max_recoveries_per_session=1
```

Keep one-release warning aliases for `specialist_config_file`, `specialist_max_initial_passes`, `specialist_max_followup_passes`, and `specialist_max_tool_calls_per_pass`. Preserve role model, token, request-timeout, context, temperature, stream-watchdog, search URL, response cap, `system_prompt_file`, and publishing inputs. Deprecate specialist `packet` mode and the specialist use of comma-separated `allowed_source_hosts`; version-2 policy sources are authoritative.

- [ ] **Step 4: Wire structured outputs through review and publish steps**

Use `review-handoff.md` as specialist `review_markdown` compatibility output. Use `review-notes.json` as the new publisher input. Do not re-run the standard whole-PR model after a valid or policy-degraded specialist result. Replace the large inline action publish body with `python3 scripts/publish_specialist_review.py` for specialist mode; retain legacy publisher dispatch only for `review_strategy=single` during this task.

- [ ] **Step 5: Run wiring and shell tests**

Run: `pytest tests/test_specialist_runtime_cli.py tests/test_action_inputs.py tests/test_specialist_runner.py tests/test_action_shell_syntax.py -q`

Run: `bash tests/test_tool_loop_wiring.sh`

Expected: all commands PASS.

- [ ] **Step 6: Commit**

```bash
git add pr_reviewer/specialist_runtime/cli.py scripts/run_specialist_reviews.py scripts/sections/config.sh scripts/sections/corpus.sh scripts/sections/review.sh scripts/run_review.sh action.yml tests/test_specialist_runtime_cli.py tests/test_action_inputs.py tests/test_specialist_runner.py tests/test_action_shell_syntax.py
git commit -m "feat(action): wire the specialist session runtime"
```

---

### Task 15: Agent-facing downstream migration handoff and examples

**Files:**
- Create: `docs/migrations/specialist-session-runtime.md`
- Modify: `README.md`
- Modify: `examples/workflow-self-hosted.yml`
- Modify: `examples/workflow-cloud.yml`
- Test: `tests/test_specialist_migration_docs.py`

**Interfaces:**
- Consumes: the actual `action.yml` input/default/output contract from Task 14 and policy schema from Task 2.
- Produces: a standalone handoff that an agent in a consuming repository can execute without reading this implementation plan.

- [ ] **Step 1: Write failing documentation-contract tests**

```python
def test_migration_document_covers_required_repository_files():
    text = MIGRATION.read_text(encoding="utf-8")
    for required in (
        ".github/ai-review-rules.md",
        ".github/ai-review-specialists.json",
        ".github/ai-review-prompt.md",
        ".github/ai-review-policy.json",
        "review_policy_file",
        "specialist_review_deadline_sec",
        "publish_mode",
    ):
        assert required in text


def test_documented_new_inputs_exist_with_matching_defaults():
    action = parse_action_inputs_with_defaults()
    table = parse_migration_input_table()
    for name, row in table.items():
        if row["status"] in {"added", "retained", "deprecated"}:
            assert action[name] == row["default"]
```

- [ ] **Step 2: Run the test and confirm the handoff is missing**

Run: `pytest tests/test_specialist_migration_docs.py -v`

Expected: FAIL because the migration document does not exist.

- [ ] **Step 3: Write the migration handoff with exact configuration tables**

Include:

1. Behavior changelog: continuous sessions, deterministic obligations, recipe accounting, web policy, sparse sticky handoff, resolvable notes, direct budgets, deadline, and artifact schema.
2. Added/changed/retained/deprecated property table with exact default, recommendation for a large multilingual repository, and reason.
3. Before/after GitHub workflow snippets using OpenAI-compatible models.
4. File checklist for workflow action pin/permissions, `.github/ai-review-rules.md`, version-1 `.github/ai-review-specialists.json`, version-2 `.github/ai-review-policy.json`, and `.github/ai-review-prompt.md` configured with `system_prompt_mode: append`.
5. A complete version-2 policy example showing component, `coverage`/`dedicated`/`independent` recipes, generated artifacts, risk policy, and official documentation source rules.
6. Manual-trigger security warning: inspect changed policy/allowlist before adding the review label.
7. Expected handoff/note/artifact behavior and troubleshooting matrix.

- [ ] **Step 4: Link docs and update examples**

Add the migration link near the specialist input table and both example workflows. Set specialist examples to `publish_mode: review_comment`, version-2 `review_policy_file`, explicit `model_context_tokens`, `specialist_review_deadline_sec: 7200`, `specialist_concurrency: 1`, and `system_prompt_mode: append`. Explain that projects should raise concurrency only after confirming provider capacity and deterministic replay behavior.

- [ ] **Step 5: Run docs/input consistency tests**

Run: `pytest tests/test_specialist_migration_docs.py tests/test_action_inputs.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add docs/migrations/specialist-session-runtime.md README.md examples/workflow-self-hosted.yml examples/workflow-cloud.yml tests/test_specialist_migration_docs.py
git commit -m "docs: add specialist runtime migration handoff"
```

---

### Task 16: Representative PR replay and adversarial acceptance suite

**Files:**
- Create: `tests/fixtures/specialist_runtime/provider-turns.json`
- Create: `tests/fixtures/specialist_runtime/multilingual-pr/`
- Create: `tests/fixtures/specialist_runtime/web-policy-pr/`
- Create: `tests/test_specialist_runtime_replay.py`
- Modify: `scripts/eval_harness.py`
- Modify: `evals/corpus-agentic.json`

**Interfaces:**
- Consumes: recorded OpenAI-compatible turns, deterministic executor fixtures, review policy, expected obligations/findings/unknowns/runtime budgets.
- Produces: offline replay report and acceptance exit status.

- [ ] **Step 1: Add replay fixture schema and failing test**

```python
def test_multilingual_replay_accounts_for_every_expected_obligation():
    result = replay_fixture(FIXTURES / "multilingual-pr")
    assert set(result.artifact["coverage"]) == set(result.expected["obligation_ids"])
    assert result.unsupported_published_claims == ()
    assert result.elapsed_simulated_sec <= result.expected["deadline_sec"]
```

- [ ] **Step 2: Build representative fixtures**

The multilingual fixture contains Java API changes, TypeScript consumer changes, Python worker messaging, schema contract, migration, deployment, tests, version-1 recipes, and a model-created sharper assignment. The web-policy fixture contains one approved official source, one unapproved result whose snippet must remain hidden, one redirect escape, and one source-access request.

- [ ] **Step 3: Add failure-injection replay cases**

Record provider sequences for duplicate/no-progress checkpoint resume, repetitive-transcript reconstruction, invalid planner repaired once, failed critic, deadline cutoff, concurrent completion inversion, and publishing note anchor race.

- [ ] **Step 4: Extend eval harness output**

Report obligation accounting, recipe status, unsupported claims, accepted/rejected candidate counts, review-note anchor types, source denials/requests, model/tool turns, recoveries, phase timing, and finalization reserve. Exit nonzero for missing mandatory statuses, unsupported public claims, unsafe fetch, budget reset, or deadline violation.

- [ ] **Step 5: Run replay and security suites**

Run: `pytest tests/test_specialist_runtime_replay.py tests/test_native_loop_exfil_redteam.py tests/test_specialist_runtime_web.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/specialist_runtime tests/test_specialist_runtime_replay.py scripts/eval_harness.py evals/corpus-agentic.json
git commit -m "test(runtime): add multilingual specialist PR replay"
```

---

### Task 17: Remove obsolete specialist orchestration and compatibility dead code

**Files:**
- Modify: `pr_reviewer/specialists.py`
- Modify: `pr_reviewer/tool_loop.py`
- Modify: `scripts/run_specialist_reviews.py`
- Modify: `scripts/build_review_comments.py`
- Modify: `scripts/resolve_finding_threads.py`
- Modify: `scripts/sections/config.sh`
- Modify: `README.md`
- Modify/delete tests: `tests/test_specialists.py`, `tests/test_specialist_runner.py`, and obsolete specialist-only assertions in `tests/test_native_tool_loop.py`

**Interfaces:**
- Consumes: all replacement interfaces proven in Tasks 1-16.
- Produces: no legacy `SequentialModelRunner`, `run_focus`, report-gap fresh conversation, recipe candidate scheduler, or specialist `2 x rounds` mapping remains reachable.

- [ ] **Step 1: Add a dead-pattern guard test**

```python
def test_removed_specialist_architecture_is_not_present():
    sources = "\n".join(path.read_text(encoding="utf-8") for path in runtime_source_paths())
    for forbidden in (
        "class SequentialModelRunner",
        "def run_focus(",
        "max_rounds=max(4, max_tools * 2 + 2)",
        "initial_fallback_focuses(",
    ):
        assert forbidden not in sources
```

- [ ] **Step 2: Run the guard and verify it fails**

Run: `pytest tests/test_specialist_runtime_cli.py::test_removed_specialist_architecture_is_not_present -v`

Expected: FAIL while legacy symbols remain.

- [ ] **Step 3: Delete obsolete implementations and update imports**

Retain only topology/file-role compatibility functions still used by the new coverage engine. Remove dead tests rather than weakening their assertions; every removed behavior must already have a replacement test in `tests/test_specialist_runtime_*.py`.

- [ ] **Step 4: Run focused specialist tests**

Run: `pytest tests/test_specialist_runtime_*.py tests/test_specialists.py tests/test_specialist_runner.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pr_reviewer scripts README.md tests
git commit -m "refactor(runtime): remove legacy specialist orchestration"
```

---

### Task 18: Full verification, migration audit, and release-ready handoff

**Files:**
- Modify only if verification exposes a defect: files already listed in Tasks 1-17.
- Verify: `docs/migrations/specialist-session-runtime.md`
- Verify: `docs/superpowers/specs/2026-07-18-specialist-session-runtime-design.md`

**Interfaces:**
- Consumes: complete branch.
- Produces: verified implementation and an agent-consumable downstream handoff matching shipped behavior.

- [ ] **Step 1: Run the complete Python suite**

Run: `pytest tests/ -v --tb=short`

Expected: all tests PASS.

- [ ] **Step 2: Run standalone shell behavior tests affected by wiring/publishing**

Run each command separately:

```bash
pytest tests/test_action_shell_syntax.py -q
bash tests/test_tool_loop_wiring.sh
bash tests/test_approval_guardrails.sh
bash tests/test_cleanup_previous_native_reviews.sh
bash tests/test_check_review_needed.sh
```

Expected: every command exits `0`.

- [ ] **Step 3: Run offline replay acceptance**

Run: `pytest tests/test_specialist_runtime_replay.py -v`

Expected: PASS with zero unsupported public claims, every mandatory obligation accounted, no unsafe fetch, no budget reset, and no deadline violation.

- [ ] **Step 4: Audit migration documentation against action manifest**

Run: `pytest tests/test_specialist_migration_docs.py tests/test_action_inputs.py -v`

Expected: PASS. Manually compare every added/changed/deprecated input row with `action.yml`, and compare each mentioned repository file with the version-2 policy and prompt-loading code.

- [ ] **Step 5: Inspect representative rendered output**

Generate the multilingual fixture's `review-handoff.md` and `review-notes.json`. Confirm the sticky handoff has no per-finding evidence/list, themes are omitted when findings are disparate, each specific finding/request is a separate note, line/file anchors are honest, access requests contain no blocked snippet, and degradation appears only when material.

- [ ] **Step 6: Record final verification and commit only necessary fixes**

```bash
git status --short
git diff --check
```

Expected: only intentional branch changes are present and `git diff --check` exits `0`. If Steps 1-5 required corrections, stage each corrected file by its literal path (never a wildcard), verify the staged diff with `git diff --cached --check`, and commit it with message `fix(runtime): close specialist runtime verification gaps`. If no corrections were required, do not create an empty commit.
