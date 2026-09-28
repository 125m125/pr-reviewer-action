# Component-Owned Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (if explicitly authorized) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce artificial review obligations through component ownership, retained group assessments, bounded cross-component evaluation, and one-level delegation.

**Architecture:** Deterministic policy-driven ownership replaces per-file and speculative topology obligations. Existing specialist sessions retain local assessments and source evidence; a bounded evaluator checks activated boundaries. The controller admits optional subset delegation and the existing negotiator prioritizes unresolved work.

**Tech Stack:** Python standard library/dataclasses, existing pytest suite, existing OpenAI/Anthropic-compatible gateway, composite GitHub Action and shell configuration. No new dependency or language parser.

**Spec:** `docs/superpowers/specs/2026-09-19-component-owned-review-design.md`.

**Status:** Decision discussion completed; D1–D6 below record the user's selections. Implementation has not started or been authorized by this document. The proposed interfaces remain subject to the marked implementation review gates.

## Risk legend

- 🟢 Clear/basic work using established patterns. Normal verification still required.
- 🟡 Some integration risk or behavior that needs focused tests/review.
- 🔴 Must-look area: consequential semantics, ownership, evidence acceptance, or an unresolved design choice. Not a claim that the task is infeasible.

## Global constraints

- Group-level assessments with explicit omissions; no mandatory per-file proof records.
- Explicit policy migration; no unmigrated legacy specialist execution path.
- A policy file is required; document and validate a minimal single-component quick start.
- Ordinary recipes become guidance; explicit independent review remains independent.
- Delegation is specialist-proposed and deterministically admitted. No additional LLM approval call.
- Delegate only a subset of remaining work. The parent retains substantive investigation.
- One delegation level; no child-created grandchildren.
- Cross-component gaps become leads, not automatic new sessions or findings.
- Unmatched paths enter one repository-remainder assignment and are visible in the action summary.
- Preserve immutable base/head revisions, repository/web authorization, redaction, tool guards, accepted evidence/candidates/checkpoints, and finalization reserves.
- All work, including evaluator calls and repairs, consumes existing global budgets/deadlines.
- No Git fetch/pull/push. Do not commit unrelated workflow changes, `docs/evaluations/`, or `.tmp-*` material.
- No live LLM tests until the user approves them; server restarts can overwrite valuable logs.
- Do not repin workflows or change movieHRdb in this planning task.

## 1. Current implementation seams

These are observed existing interfaces, not new modules to recreate:

| Existing file | Relevant seam |
| --- | --- |
| `policy.py` | `parse_review_policy`, `load_review_policy`, `ReviewPolicy`, recipe/source validation, current v1-to-v2 migration |
| `specialists.py` | Path roles, component topology, broad inferred relationships |
| `coverage.py` | `derive_obligations`, `CoverageLedger`, `SessionOwnership`, `reconcile_wave` |
| `assignments.py` | `Assignment`, `AssignmentPlan`, deterministic fallback, planner transformations, budget validation |
| `obligation_assessment.py` | `ObligationAssessmentLedger`, stable O# handles, immediate disposition validation |
| `session.py` | Resolution/candidate/lead tools, checkpoints, compaction, retained session results |
| `controller.py` | `_plan`, model role calls, run state, wave reconciliation, negotiation, critic and handoff |
| `scheduler.py`, `budget.py` | Fixed wave scheduling, leases, accounting, cancellation |
| `evidence.py` | Retained source records, provenance, snapshots and associations |
| `negotiation.py` | Compact actions, leads, capability/feasibility checks and fallback |
| `cli.py`, `model_gateway.py` | Role prompts/adapters, bounded structured calls, request logging, artifacts |

All filenames in this table except `specialists.py` are under `pr_reviewer/specialist_runtime/`.
Do not restructure these large modules as a side project. Two focused new modules
are proposed: `boundary_evaluation.py` and `delegation.py`, each with a narrow
responsibility; neither duplicates the existing scheduler or evidence store.

Important discovered dependencies:

- `assignments.py` currently rejects mandatory obligations without evidence categories.
- `CoverageObligation` is also consumed by candidate consequence validation.
- `ReviewPolicy.legacy_projection()` feeds existing topology helpers; removing a
  legacy runtime does not mean blindly deleting every internal adapter.
- The optional initial planner can currently merge/split the deterministic plan.
- The scheduler currently snapshots an assignment list for a wave. Live child
  admission needs an explicit scheduling choice, not a callback added casually.

## 2. Confirmed decisions

These selections were confirmed one at a time with the user.

| ID | Risk | Choice | Confirmed selection | Tasks |
| --- | --- | --- | --- | --- |
| D1 | 🔴 | Keep, restrict, or remove the initial LLM planner? | Skip its assignment-transformation call in the new method; retain the change summarizer and later negotiator | 3, 4, 11 |
| D2 | 🔴 | How are delegations admitted and scheduled? | Reuse lead infrastructure with distinct delegation semantics; deterministic admission/ownership transfer, then priority-based scheduling alongside pending work. No fixed scheduling-round cap; resource limits still apply | 9, 10 |
| D3 | 🔴 | How does a group assessment identify assessed scope? | Explicit paths grouped in one assessment; no per-path disposition. Unassessed paths and stated behavioral gaps remain schedulable | 5, 6 |
| D4 | 🔴 | Which participants are required when a shared contract changes? | Start with all configured participants; permit lightweight evidence-backed not-affected assessments after a relevant check, not exhaustive proof of absence | 2, 3, 7 |
| D5 | 🟡 | What happens when no policy file exists at all? | Fail before model calls with setup guidance. Provide a tested minimal single-component quick start; valid policies still use remainder ownership for unmatched paths | 2, 12 |
| D6 | 🟡 | Limits for bounded evaluator and evaluator repair? | Reuse existing model/context/output configuration and structured-response recovery behavior; no evaluator-specific overrides or special one-repair limit initially | 7, 8 |

Additional must-look review gates, not configuration questions:

- Coverage acceptance must not become either a trivial `evidence_ids != []` check
  or a new exact-phrase proof maze.
- Boundary status must describe a specific reviewed question, not certify an
  entire API just because both participants reported covered.
- Every initial, delegated, and independent work item must have a visible owner
  and remaining scope. Never count queued work as assessed.

## 3. Proposed interfaces and wire shapes

These names are the starting implementation contract, subject to the decision
answers. Reuse `CoverageObligation` and O# handles for substantive requirements;
do not introduce a parallel replacement obligation system.
Code examples below specify proposed shapes and test assertions; they are not
implemented APIs or complete runnable test files yet. Expand each into the named
test suite using its existing setup helpers during the failing-test step.

### Policy proposal (new version 3)

```json
{
  "version": 3,
  "components": [
    {"id": "java-backend", "paths": ["movieHRdb-backend/**"]},
    {"id": "python-worker", "paths": ["movieHRdb-pythonworker/**"]}
  ],
  "ownership_precedence": [],
  "boundaries": [
    {
      "id": "backend-worker-messages",
      "contract_paths": ["movieHRdb-asyncapi/**", "movieHRdb-protobuf-backend/**"],
      "participants": ["java-backend", "python-worker"],
      "contract_change_owner": "java-backend",
      "endpoint_paths": {
        "python-worker": ["movieHRdb-pythonworker/**/messaging/**"]
      },
      "objective": "Compare changed message construction and consumption, including identity and failure semantics."
    }
  ],
  "recipes": []
}
```

This is a minimal schema example, not the complete migrated movieHRdb policy.
Preserve existing responsibilities/invariants, sources, exclusions, generated
artifacts, publishing/verdict restrictions, and explicit evidence requirements.
`components` are review owners; shared source globs belong to `boundaries` and can
overlap an owner such as database. A shared source has one primary investigation
owner: its matched component, or `contract_change_owner` if none matches. The
boundary participants get relevant checks, not duplicate file ownership.
Conflicting fallback contract owners or component matches without explicit
precedence fail with a clear error. Missing ownership for ordinary files does not.

Use recipe `execution: integrated | independent` in v3. Old `coverage`/`dedicated`
values are rejected with migration guidance. No automatic conversion of explicit
evidence requirements into optional hints.

### Local assessment (extend the existing resolution shape)

```json
{
  "target": "O2",
  "disposition": "partially_covered",
  "reason": "The handler looks up showId as a database primary key; retry ordering was not checked.",
  "assessed_paths": ["movieHRdb-pythonworker/mhrdb/messaging/message_processing.py"],
  "omitted_paths": [],
  "evidence_ids": ["evidence:retained-source-id"],
  "next_actions": ["Inspect retry ordering if follow-up budget permits."]
}
```

Existing defect-assessment/candidate reporting remains supported; this example
shows only the coverage delta. Reuse `reason` for the semantic observation rather
than requiring several nearly identical summaries. The controller derives pending
paths by subtraction; an empty omission list does not mean all scope was covered.
Reference reads outside primary changed scope do not transfer ownership.

Proposed type extensions:

```python
# In obligation_assessment.py; retain existing fields as well.
assessed_paths: tuple[str, ...] = ()
omitted_paths: tuple[str, ...] = ()
assessment_version: int = 0  # controller-generated, never supplied by the model

# In Assignment / SpecialistAssignment, with defaults for construction sites.
owner_component_id: str = ""
owned_changed_paths: tuple[str, ...] = ()
parent_assignment_id: str | None = None
delegation_depth: int = 0
```

### Boundary evaluator

```python
@dataclass(frozen=True)
class BoundaryEvaluation:
    boundary_id: str
    input_fingerprint: str
    outcome: str  # supported | insufficient_evidence | potential_contradiction
    reason: str
    evidence_ids: tuple[str, ...]
    missing_fact: str = ""
    suggested_investigation: str = ""
```

The controller attaches boundary ID/fingerprint from the request. The model need
not echo hashes or large ID lists. Model citations must refer to supplied retained
source evidence. Preserve the input packet/version in the artifact.

### Delegation proposal

```json
{
  "question": "Check changed cast/person mapping for identity and cardinality regressions.",
  "changed_paths": ["movieHRdb-backend/src/main/java/de/_125m125/movieHRdb/showManagement/mapper/ShowMapper.java"],
  "targets": ["O1"],
  "reason": "Independent from the vector synchronization path I am investigating.",
  "evidence_ids": []
}
```

Advertise as `request_delegation`; the returned controller ID and status are
authoritative. No model-authored session budget or child system prompt. Child
instructions inherit policy and selected requirements, not arbitrary authority
from the parent's text.

## 4. Task sequence

For each task: add focused failing tests, run them to confirm the failure, make
the smallest implementation, rerun the focused set, review the diff, and commit
only the task's named files when execution/commits are authorized. Do not create
placeholder modules or commit a production switch to an incomplete pipeline.

### Task 1 — 🟢 Establish a small reproducible baseline

**Files:** create `tests/fixtures/specialist_runtime/component-owned/fixture.json`;
create `tests/test_specialist_runtime_component_review.py`.

- [ ] Build a synthetic, public fixture with two Java owners, a Python owner,
  a shared message contract, deployment, an unmatched path, and an explicitly
  independent recipe. Do not copy private movieHRdb source or raw run artifacts.
- [ ] Add `load_component_fixture()` in the new test module returning the fixture
  as a dictionary with `policy`, `changed_files`, and `tracked_files` keys.
- [ ] Record current behavior on that fixture without enshrining the inflated
  obligation count as desired behavior. Preserve existing candidate/publication
  behavior using the existing replay fixture as the regression baseline.
- [ ] Confirm the virtual environment and run the current targeted baseline:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_specialist_runtime_coverage.py tests/test_specialist_runtime_assignments.py tests/test_specialist_runtime_replay.py -q
```

**Gate:** failures present before implementation are recorded separately; no live
model call or external repository access is needed.

### Task 2 — 🔴 Introduce explicit v3 policy semantics (D4, D5)

**Files:** modify `policy.py`, `types.py` only for shared policy-facing values if
needed, `tests/test_specialist_runtime_policy.py`; migrate synthetic fixture.

**Interface:** preserve `parse_review_policy(data) -> ReviewPolicy` and
`load_review_policy(...) -> ReviewPolicy`; add frozen `BoundaryPolicy` in
`policy.py`, `ReviewPolicy.boundaries`, and `ownership_precedence`.

- [ ] Add failing parser tests for version 3, old version rejection, old execution
  rejection, duplicate IDs, unknown participant/owner, invalid path, explicit
  overlapping ownership precedence, and D5's missing-file behavior.
- [ ] Reject a missing policy before model calls with the expected path and a link
  to the quick start. Do not confuse missing policy with unmatched changed paths
  under a valid policy; the latter remain reviewable through the remainder owner.
- [ ] Parse boundaries with the existing path normalization/strict-key helpers.
  Validate `contract_change_owner` is a participant and precedence IDs are known.
- [ ] Remove automatic v1 migration from the active parser path. Remove fallback
  to a legacy specialists file; if that file is the only policy, return an
  actionable migration error rather than silently choosing a minimal policy.
- [ ] Audit current-branch policy authorization/serialization: v3 owner/boundary
  data must survive unchanged; source and publishing restrictions stay enforced.

```python
def test_old_policy_requires_explicit_migration():
    with pytest.raises(ValueError, match="migration"):
        parse_review_policy({"version": 2, "components": [], "recipes": []})
```

Run `tests/test_specialist_runtime_policy.py`. Keep the production parser switch
and bundled policy migration in the coherent release of Tasks 2–8/11–12; do not
publish an action version that rejects its own bundled configuration.

### Task 3 — 🔴 Derive owners and substantive checks, not a topology explosion

**Files:** modify `specialists.py`, `coverage.py`, `types.py`,
`tests/test_specialists.py`, `tests/test_specialist_runtime_coverage.py`, and
`tests/test_specialist_runtime_component_review.py`.

**Interface:** keep `derive_obligations(topology, classification, policy)`; add
owner/boundary/participant metadata to `CoverageObligation` where needed. Use
existing component projection instead of creating a second topology engine.

- [ ] Test two Java components remain distinct owners; language is not ownership.
- [ ] Replace individual generic implementation obligations with one substantive
  changed-behavior requirement per owner, including the remainder owner.
- [ ] Stop mandatory obligations for broad inferred component edges and generic
  risk flags; keep their bounded orientation/priority information as hints.
- [ ] Attach integrated recipe objective/invariants to the owner. Explicit
  evidence requirements stay predicates within that question, not separate jobs.
- [ ] Create local participant requirements plus a controller-owned combined
  requirement for an activated configured boundary. Mark the combined requirement
  unassignable to ordinary specialist resolution; only the evaluator can support it.
- [ ] Use D4's participant rule. Contract-only changes initiate their configured
  owner; consumers can be unchanged. Unmatched files remain in the remainder group.
- [ ] Keep independent requirements and low-priority failed-test triage; associate
  relevant failed tests with owner work rather than creating a privileged test lane.

```python
# Core property for the new fixture: growth in files does not create jobs per file.
assert implementation_requirement_count(with_40_java_files) == implementation_requirement_count(with_4_java_files)
# Test-local helper counts origin == "component" obligations, not all requirements.
```

**Gate:** compare obligation categories before/after. Explain every surviving
required category. Do not advertise a count such as 12–20 as a hard guarantee.

### Task 4 — 🟡 Build component assignments and explicit policy exceptions (D1)

**Files:** modify `assignments.py`, `controller.py` (`_plan`), `cli.py` role setup,
`tests/test_specialist_runtime_assignments.py`, `tests/test_specialist_runtime_controller.py`.

**Interface:** keep `AssignmentPlan`; add
`component_assignment_plan(obligations, topology, config) -> AssignmentPlan` in
`assignments.py`. Keep read authorization separate from `owned_changed_paths`.

- [ ] Test one initial assignment per affected owner plus explicit independent
  assignments. No per-file or cross-product subdivision to meet a numeric target.
- [ ] Exclude evaluator-owned combined requirements from assignable work, but
  retain them in the coverage ledger. Queued owners remain visibly unreviewed.
- [ ] Give assignments bounded changed context, group inventory, objectives,
  invariants, hints, and canonical requirement handles.
- [ ] Apply D1: bypass the optional planner's transformation
  call. Do not remove the change summarizer or negotiator. Document obsolete
  planner inputs and their migration instead of leaving misleading active controls.
- [ ] Verify capacity/deadline ordering does not silently drop owners or invent
  completion when only some initial assignments can run.

```python
assert len([a for a in plan.assignments if a.owner_component_id == "java-a" and not a.overlap_justification]) == 1
assert combined_boundary_id not in set().union(*(set(a.obligation_ids) for a in plan.assignments))
```

Run assignment/controller focused tests. Review whether any old planner/family
helper remains reachable before deleting it; no unrelated legacy-harness rewrite.

### Task 5 — 🔴 Retain group assessments without false completion (D3)

**Files:** modify `obligation_assessment.py`, `coverage.py`, `types.py`,
`tests/test_specialist_runtime_obligation_assessment.py`, `tests/test_specialist_runtime_coverage.py`.

**Interface:** extend `ObligationAssessment` and `ObligationAttempt` with assessed
and omitted paths; extend `ObligationAssessmentLedger.propose` with keyword-only
path arguments. Add `partially_covered` to the disposition vocabulary.

- [ ] Write tests for an assessment of 2 out of 5 paths, empty assessments,
  supplementary out-of-owner evidence, invalid paths, and contradictory overlap.
- [ ] Resolve changed paths against immutable ownership. D3 requires exact paths,
  not model globs; group updates can contain many paths without per-path statuses.
- [ ] Require a substantive reason and eligible retained evidence for supported
  work. Do not require each path to have its own evidence ID or matching phrase.
  Record that this checks provenance/scope, not the truth of the model's claim.
- [ ] Derive unassessed scope by set difference. Partial updates accumulate with
  explicit revisions; conflicting or withdrawn assessments must not remain covered.
- [ ] Expose both unassessed paths and behavioral gaps within assessed paths to
  negotiation. A file marked assessed does not erase an unchecked retry/identity
  question. Follow-ups remain risk-based and do not create one job per omitted file.
- [ ] For boundary inapplicability, accept a concise reason backed by at least one
  relevant successful retained search/lookup/inspection, absent known contradictory
  evidence. No exact wording or exhaustive call graph is required. Failed, blocked,
  or materially truncated searches alone are insufficient. Preserve the scoped
  conclusion: no affected usage found in the checks performed, not proven absence.
- [ ] Reject specialist attempts to close evaluator-owned requirements. Preserve
  valid local work/candidates when another assessment row is rejected.
- [ ] Preserve explicit policy evidence categories and independent verification
  rules without accidentally retaining the old generated evidence-category jobs.

```python
assert accepted_group_paths == {"backend/a.py", "backend/b.py"}
assert remaining_paths == {"backend/c.py", "backend/d.py", "backend/e.py"}
assert component_status != ObligationStatus.COVERED
```

**Must-look:** evidence eligibility currently also affects consequence proof in
`adjudication.py`. Verify those tests remain unchanged in meaning; do not broaden
candidate admission just because owner scopes become larger.

### Task 6 — 🟡 Use the same assessments in tools, checkpoints, and compaction

**Files:** modify `session.py`, `cli.py` specialist prompt, `types.py` checkpoint
projection; tests in `test_specialist_runtime_session.py` and
`test_specialist_runtime_request_attempts.py`.

**Interface:** extend existing `propose_obligation_resolution` and checkpoint
assessment rows; do not add an alternative coverage-reporting tool.

- [ ] Add schema/prompt tests for group-level reporting, partial results, actual
  behavior in `reason`, and explicit omissions. Do not demand the full inventory
  be repeated every checkpoint.
- [ ] Route both tool and checkpoint proposals through the Task 5 validator with
  identical actionable feedback. Existing accepted rows survive focused repair.
- [ ] Persist assessment versions/evidence outside the conversational window;
  restore them after compaction, checkpoint recovery, and session cutoff.
- [ ] Keep private `proposed_next_actions` distinct from schedulable leads and
  human focus. Candidate reporting still asks about concrete defects, not only
  satisfying coverage bookkeeping.

```python
assert tool_feedback["reason"] == checkpoint_feedback["reason"]
assert restored_assessment.evidence_ids == accepted_assessment.evidence_ids
assert restored_assessment.assessed_paths == accepted_assessment.assessed_paths
```

Run the assessment, session, and request-attempt suites together. Ensure the new
fields do not introduce strict-response recursion or extra compulsory checkpoints.

### Task 7 — 🔴 Build and validate bounded boundary evaluations (D4, D6)

**Files:** create `boundary_evaluation.py`,
`tests/test_specialist_runtime_boundary_evaluation.py`; modify `types.py` for shared
result values only if necessary, `cli.py` for the tools-disabled role.

**Interfaces in the new module:**

```python
build_boundary_context(boundary, assessments, evidence, *, max_bytes: int) -> dict
validate_boundary_evaluation(raw: object, context: Mapping) -> BoundaryEvaluation
```

`boundary` is `BoundaryPolicy`; `assessments` are accepted `ObligationAssessment`
values; `evidence` is `EvidenceSnapshot`. Returned context contains relevant
contract/implementation records, source limits, and the controller fingerprint.

- [ ] Test matching producer/consumer IDs and schema-valid mismatched IDs, using
  actual source excerpts rather than summary-only assertions.
- [ ] Select cited retained content, validate provenance/revision, and preserve
  source line/range/truncation data. A model summary is not source evidence.
- [ ] Include accepted not-affected assessments and their lookup evidence when
  narrowing participants for a specific question. Do not demand full consumer
  implementation proof for accepted inapplicability; contradictory new evidence
  reopens it. This does not relax defect-candidate proof requirements.
- [ ] Fingerprint semantic inputs deterministically with canonical JSON/hash;
  exclude timestamps, usage, and unrelated assessment updates.
- [ ] If required evidence cannot fit, return an explicitly incomplete packet;
  skip or refuse supported evaluation with a precise diagnostic, never silently
  drop one participant. No mandatory new input-selection LLM.
- [ ] Define only the three agreed outcomes. A supported result needs valid
  supplied citations and no missing required participant/evidence. The LLM judges
  compatibility; deterministic validation must not pretend to prove semantics.
- [ ] Use the existing role adapter's bounded recovery for malformed output under
  D6, including existing configured repair limits. Do not impose an evaluator-only
  one-repair rule. A failed role retains local results and leaves the combined
  check incomplete.

```python
with pytest.raises(ValueError, match="source evidence"):
    validate_boundary_evaluation({"outcome": "supported", "reason": "Both summaries agree", "evidence_ids": []}, context_without_sources)
```

**Gate:** a supported result must name the specific question it supports. It is
not an API-wide certificate and cannot authorize a finding or source access.

### Task 8 — 🔴 Schedule evaluation, evidence-gap leads, and invalidation

**Files:** modify `controller.py`, `negotiation.py`, `budget.py` only if needed,
`coverage.py`, `cli.py`; tests in controller, negotiation, request-attempt and new
boundary-evaluation suites.

**Interface:** a controller `_evaluate_ready_boundaries(state)` reconciliation
step; reuse `_model_request`/role budget accounting. Store latest evaluations and
input fingerprints in `_RunState` and structured output.

- [ ] After accepted local reconciliation, identify ready changed packets; no
  need to wait for every component to finish or have status covered.
- [ ] Charge evaluator/repair calls against global limits and the active lease.
  Protect specialist finalization and critic/publishing reserves. Record skipped
  evaluations explicitly when capacity/time runs out.
- [ ] Convert actionable gaps/contradictions into existing `InvestigationLead`
  records with stable boundary/question identity. Update the lead when evidence
  changes rather than creating duplicates.
- [ ] Let the negotiator select resume/new cross-component investigation/leave
  unresolved. A new investigation receives both sides' permitted evidence and
  consumes normal session capacity; it is not launched by validator failure.
- [ ] Local results survive evaluator disagreement/failure. Changed semantic
  inputs mark prior support stale until reevaluated; unrelated metadata does not.
- [ ] A contradiction reported before all sides finish can form a lead without
  waiting for a ready packet. No automatic infinite evaluate/investigate cycle.

```python
assert calls_for_same_fingerprint == 1
assert len(leads_for_boundary_question) == 1
assert new_sessions_after_inconclusive_evaluation == 0  # until negotiator chooses one
```

This finishes the first coherent runtime milestone: component ownership plus
boundary evaluation. Child delegation remains unadvertised until Tasks 9–10 pass.

### Task 9 — 🔴 Admit subset delegation and transfer ownership safely (D2)

**Files:** create `delegation.py`, `tests/test_specialist_runtime_delegation.py`;
modify `assignments.py`, `coverage.py`, `controller.py`, `types.py`.

**Interfaces:** `validate_delegation(raw, assignment, owned_paths, remaining_paths,
limits) -> DelegationProposal`; controller commits a `DelegationDecision` containing
status, request ID, child assignment ID when accepted, and reason. These dataclasses
live in `delegation.py`. Validation alone must not mutate ownership.

Reuse the existing lead store/lifecycle and scheduler queue; `delegation.py` owns
validation/transfer logic, not a parallel scheduling system. Add an explicit kind
and delegation ownership metadata to lead records. Ordinary leads must never
transfer ownership. An accepted delegation may be queued without yet running.

- [ ] Test whole-remainder rejection, nonexistent/unowned paths, already delegated
  work, unauthorized evidence, depth > 0, exhausted capacity, and duplicate requests.
- [ ] Admit only a proper subset of remaining changed work or an explicitly
  separable requirement; preserve meaningful parent work. No semantic benefit LLM
  or invented numerical minimum task size. Return concrete rejection reasons.
- [ ] Use a controller-serialized transaction for capacity reservation, child
  assignment creation, delegation-lead retention, and ownership transfer. If any
  step fails, parent ownership remains. Recording an assignment does not start an
  LLM session or guarantee that priority-based scheduling will execute it.
- [ ] Retain pre-transfer accepted evidence/candidates and assessment provenance.
  A late parent update cannot claim completion for now-transferred scope.
- [ ] Separate exclusive work ownership from read access. Child gets authorized
  reference context; independent policy checks remain deliberate exceptions.

```python
assert rejected_whole_remainder.status == "rejected"
assert parent_owned_after | child_owned_after == original_owned
assert not parent_owned_after & child_owned_after
```

**Gate:** two concurrent proposals cannot receive the same primary work or spend
the same remaining session capacity. No paused session is counted as free capacity
if the existing hard limit counts it as an allocated lifetime session.

### Task 10 — 🔴 Run children without starving the parent or losing results (D2)

**Files:** modify `session.py`, `scheduler.py`, `controller.py`, `negotiation.py`,
`cli.py`; tests in delegation, scheduler, controller, session suites.

- [ ] Add `request_delegation` only for eligible primary sessions and route it
  through the controller-owned admission boundary, not direct recursive spawning.
- [ ] Apply D2 through existing lead/negotiation infrastructure. Admit under the
  controller lock and consider queued delegations with other work at safe scheduling
  points, based on risk/value and remaining resources. Do not add a second queue,
  automatic launch-on-admission, or a fixed number of scheduling rounds. Parent
  need not block or wait for child results in its tool call.
- [ ] Copy relevant policy, owned diff, selected accepted evidence and concise
  observations; do not copy the full parent conversation or expand authorization.
- [ ] Keep child candidate/evidence/assessment publication central. Parent feedback
  contains authoritative scope status and bounded results, not a demand to rewrite
  child findings. Ordinary critic handles deduplication as before.
- [ ] On child failure, keep accepted results, leave outstanding scope pending,
  and let controller/negotiator explicitly return or reassign it. Never silently
  return it while the parent still believes transfer is active.
- [ ] Children cannot delegate, but can report leads. Compaction cannot forget
  transfer status; authoritative continuation context lists current ownership.
- [ ] Test more than two useful scheduling decisions when resources allow, and
  verify total-session/turn/tool/deadline limits still stop work. A delegated item
  never selected stays explicitly unreviewed and is not silently returned/covered.

```python
assert "request_delegation" not in child.advertised_tools
assert retained_child_candidate.collector_session_id == child.session_id
assert parent_tool_response["status"] == "queued"  # not a fabricated child result
```

**Must-look:** exercise a single-worker scheduler. Queue admission must not deadlock
a parent waiting on its own child or starve every queued initial component.

### Task 11 — 🟡 Integrate final coverage, logs, critic inputs, and human output

**Files:** modify `controller.py`, `cli.py`, `adjudication.py` only for projection
compatibility; tests in CLI, controller, adjudication and replay suites.

- [ ] Update artifacts for owners, unmatched paths, group scope, delegation
  decisions, evaluator evidence versions/outcomes, skipped work, and cost.
- [ ] Log bounded start/completion/failure messages with mode and reasons, including
  missing source packets, repair, stale evaluation and denied delegation. Keep
  full model content in artifacts, not console output.
- [ ] Extend the existing action-summary tables with pending coverage, ownership,
  evaluator counts and unmatched-path warning; avoid exposing the whole graph in PR.
- [ ] Keep handoff behavioral and self-contained. Local completion cannot become
  a claim of end-to-end compatibility. No automatic verification notes for every gap.
- [ ] Preserve candidate critic/adjudication/remediation logic and retained evidence
  access across parent/child sessions. New obligation metadata must not weaken
  violated-invariant or affected-consumer proof checks.

```python
if required_boundary_incomplete:
    assert handoff.status != "AI review complete"
assert candidate_statistics_before_projection == candidate_statistics_after_projection
```

Run existing publication-related tests in addition to runtime suites; human output
must not regress to raw obligation/evidence ID lists.

### Task 12 — 🟢 Documentation, migration, and repository configuration

**Files:** modify `docs/review-policy-authoring.md`,
`docs/migrations/specialist-session-runtime.md`, `README.md`,
`.github/ai-review-policy.json`; inspect `.github/ai-review-rules.md` and
`.github/ai-review-prompt.md` for old obligation-hunting instructions.
Create `docs/migrations/component-owned-review.md`.

- [ ] Document v3 schema, exact matching/precedence semantics, boundary activation,
  required-policy behavior from D5, and error examples for old configurations.
- [ ] Add a quick start with a complete minimal v3 policy declaring one repository-
  wide component, its exact file location, and validation/run instructions. Validate
  the published example using the production parser. Explain how to add boundaries
  later, without requiring elaborate setup for small repositories.
- [ ] Provide an agent handoff table: old property/behavior, replacement,
  recommendation, and why. Include ordinary vs independent recipes, components,
  contract-only routing, explicit evidence requirements and unchanged sources.
- [ ] Include a complete validated movieHRdb-style example with role-distinct
  owners and separate contract boundaries. Validate it using the real parser;
  do not present the abbreviated section 3 example as production-ready policy.
- [ ] Migrate this repository's actual policy deliberately; do not mechanically
  convert every old dedicated recipe to independent.
- [ ] Explain planner setting changes from D1, new tool behavior, and evaluator
  budget usage. No new knobs solely for hypothetical future tuning.
- [ ] Update old fixtures that execute live policy parsing; retain old versions
  only as rejection/migration test inputs, not a supported runtime path.

```powershell
.venv/Scripts/python.exe -m pytest tests/test_specialist_runtime_policy.py tests/test_specialist_runtime_cli.py -q
```

No automatic writes to sibling movieHRdb: the validated example/handoff supports
its later explicit migration. Workflow pin changes need a separate user request.

### Task 13 — 🔴 Full regression, adversarial replay, and controlled rollout

**Files:** modify `replay.py` / `replay_adversarial.py` only where new state requires
it; extend existing replay, scheduler, controller, performance tests and the Task 1
component fixture.

- [ ] Run focused tests after each task; run the full Python suite before the
  implementation is called complete. Use the existing shell/CI environment for
  shell tests rather than assuming PowerShell can execute bash scripts directly.
- [ ] Replay component-only, contract-only, database-only, deployment-only, two
  Java owners, unknown paths, independent checks and mismatched identifiers.
- [ ] Inject oversized source packets, failed/length-limited evaluator responses,
  late results, exhausted budgets, concurrent delegation, and child quarantine.
- [ ] Check no work disappears, no scope is silently covered, retained results
  survive failure, and no auxiliary call bypasses global accounting.
- [ ] Compare planned jobs/checks and prompt sizes to the existing synthetic
  baseline. A lower count is not sufficient if coverage/finding tests regress.
- [ ] Review 🔴 tasks before any action pin update. Commit scoped changes only;
  the user pushes and starts live runs.

```powershell
.venv/Scripts/python.exe -m pytest tests/test_specialist_runtime_component_review.py tests/test_specialist_runtime_boundary_evaluation.py tests/test_specialist_runtime_delegation.py -q
.venv/Scripts/python.exe -m pytest tests/ -q
git diff --check
```

Live validation order, after user approval:

1. Small planted-defect PR: ensure candidates, critic and suggestions still work.
2. movieHRdb PR 205 (if still representative): compare local coverage, repeated
   reads, checkpoint cost, boundary outcomes, defect quality and elapsed time.
3. A normal PR with different component/contract shape; do not immediately tune
   every prompt based on one model run.

Record the exact action revision, policy revision, model settings and active
budgets. Archive each run before overwriting local logs. No promised runtime
reduction until measured; one local model still does inference sequentially.

## 5. Execution dependencies and review gates

```text
1 baseline -> 2 policy -> 3 requirements -> 4 assignments
                                      \-> 5 assessments -> 6 session integration
                                           \-> 7 evaluator -> 8 scheduling
4 + 5 + 8 -> 9 delegation admission -> 10 child execution
2..10 -> 11 output integration + 12 migration/docs -> 13 release verification
```

The first coherent milestone includes Tasks 1–8 and the applicable output/docs
work. The second adds delegation, then full release verification. These are
internal implementation milestones, not a request to ship two incompatible policy
versions or enable a half-built pipeline.

If later authorized, parallelize only isolated pure-module work after agreeing
shared types; do not concurrently edit controller/session ownership state from
several implementers. No subagents are being started for this initial plan.

## 6. Spec coverage / self-review checklist

- [x] Goals and non-goals: Tasks 1, 3, 13; no language parsers or finding quotas.
- [x] Owner/contract configuration and explicit migration: Tasks 2, 12.
- [x] Initial component ownership and independent exceptions: Tasks 3, 4.
- [x] Group assessment, omissions and checkpoint continuity: Tasks 5, 6.
- [x] Real-source cross-component evaluation and lead scheduling: Tasks 7, 8.
- [x] Subset delegation and failure-safe ownership: Tasks 9, 10.
- [x] Global budgets, session capacity and cutoffs: Tasks 8, 9, 10, 13.
- [x] Artifacts, concise human output and unchanged finding validation: Task 11.
- [x] Concrete migration handoff and movieHRdb example: Task 12.
- [x] Controlled evidence-based evaluation: Task 13.

D1–D6 are settled and incorporated into the affected tasks. The risk markings
remain review gates, not unresolved user choices. Obtain implementation approval
before execution; if a new material design choice emerges, ask one question at a
time and wait for the user's answer.
