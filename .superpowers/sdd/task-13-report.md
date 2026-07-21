# Task 13 report: review controller and terminal artifact

## Outcome

Implemented only Task 13 of the specialist-session runtime plan.

- `ReviewController.run(ReviewInputs) -> ReviewResult` owns the legal controller
  transition chain `precheck -> planning -> initial -> followup -> finalization
  -> publish_ready -> complete`.
- The controller composes the existing coverage, assignment validation/fallback,
  scheduler, reconciliation, negotiation, adjudication, sparse handoff/note, and
  runtime verdict APIs. It does not duplicate their policy decisions.
- Initial and follow-up waves receive an exact immutable `WaveSnapshot`; that
  snapshot's coverage projection is passed unchanged to `reconcile_wave`.
- Each admitted worker owns isolated evidence/coverage state. Only completed,
  identity-validated results are merged by the controller thread; timed-out
  workers remain daemonized and quarantined from finalization and artifacts.
- Planner, repair, negotiator, critic, and finalizer roles receive typed
  `RoleRequest` values with absolute phase leases, bounded request timeouts,
  token caps, current policy, and PR intent. Late role results are discarded.
- The terminal `specialist-review-artifact.json` is validated, canonical JSON,
  numeric schema version 2, strict finite JSON, and written only beneath the
  controller-owned output root through a private same-directory temporary file,
  fsynced, and atomically replaced.
- `EventJournal` adds a thread-safe, append-only, bounded, redacted event owner.
  Observer failures cannot abort or erase a run; observer latency and concurrency
  are bounded.
- Task 12's GitHub publisher is not invoked. `publish_ready` exposes only the
  deterministic verdict result, sparse handoff, and typed note set for later
  Task 14 wiring.

The two unrelated untracked July 12 documents were not edited, staged, or
committed.

## TDD record

### Genuine controller import RED

The first production-independent test imported the wished-for Task 13 API.

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_specialist_runtime_controller.py -v --tb=short
```

Result before `controller.py` existed:

```text
ModuleNotFoundError: No module named 'pr_reviewer.specialist_runtime.controller'
```

The minimal public dataclasses/controller shell then made that single import test
GREEN (`1 passed`).

### Happy-path orchestration RED/GREEN

The next test constructed a scripted planner, specialist, critic, and finalizer,
then asserted obligations, recipe coverage, typed notes, all controller phases,
and the on-disk artifact.

Initial RED:

```text
TypeError: ReviewController() takes no arguments
```

After the minimal end-to-end orchestration implementation, the test passed.

### Failure and authority RED/GREEN cycles

- The full failure suite initially exposed the absent terminal candidate
  disposition projection (`KeyError: candidate_dispositions`); the artifact now
  retains every critic disposition separately from accepted/rejected candidates.
- A typed source-access test exposed an invalid assumed `.fingerprint` attribute
  (`IndexError` after terminal degradation). The controller now derives a stable
  request reference from the real `SourceAccessRequest.as_dict()` contract.
- A catastrophic-component test exposed a fabricated `Approve` label in the
  notice fallback. The fallback now says `Human review required`, has no blocking
  finding, and is not publisher-ready.
- Deadline tests exposed synchronous model callbacks and late-result races. Role
  calls now run behind typed phase leases in daemon workers and emit accepted
  request completion only after a timely result.
- Isolation tests exposed shared worker evidence, provisional ghost ownership,
  assignment-ID/session-ID aliasing, and stale initial leases on resume. Worker
  state is now private until completion, controller-issued session IDs are
  verified across results/checkpoints/evidence/candidates, and resumed sessions
  receive the actual follow-up lease.
- Artifact adversarial tests exposed raw checkpoint working state, string schema
  version `1.0`, mismatched artifact/reference identities, non-finite JSON,
  arbitrary output paths, and a publisher-ready emergency projection. Explicit
  bounded projections, numeric schema version 2, one artifact identity function,
  output-root confinement, terminal-status validation, and a non-publishable
  emergency artifact close those paths.
- Event adversarial tests exposed sensitive-key spelling gaps, short inline
  secrets, non-finite values, hostile `__str__`, `KeyboardInterrupt`, and
  unbounded slow observers. These values are now redacted/sanitized and external
  observation is daemonized with four bounded slots.

## Failure matrix

| Injected failure | Deterministic terminal behavior |
|---|---|
| Planner exception or invalid/failed repair | Existing `fallback_assignment_plan`; mandatory overflow remains explicitly unassigned/unknown |
| Specialist construction/exploration failure | Scheduler isolates it; controller attempts one feasible fallback follow-up assignment, otherwise records the policy-owned unknown |
| Negotiator exception/invalid proposal | Existing `fallback_next_action` built from live session budget, lease, deadline, ownership, and capacity projections |
| Critic exception | Conservative deterministic decisions; ambiguous candidates are rejected and cannot affect verdict |
| Finalizer exception/invalid type | Existing sparse handoff builder from controller facts; final static fallback contains no finding detail |
| Exploration cutoff | No new session is constructed; unresolved obligations enter risk policy while the positive finalization reserve remains available |
| Unexpected controller component failure | All remaining legal phases close; result is a non-publisher-ready human-review notice with no fabricated blocker |
| Artifact writer failure | Prior target remains untouched; in-memory schema-valid artifact records `artifact_write.status=failed`, redacted error, and the appended failure event |
| Invalid/out-of-root artifact path | No filesystem write occurs; the in-memory artifact is terminal, degraded, and non-publisher-ready |
| Worker still running at cutoff | Worker-local state is quarantined; no evidence/session/candidate is merged and the session is never finalized |
| Emergency artifact projection | Minimal schema-v2 artifact marks all derived obligations/recipes unresolved and cannot be published |

No recovery grants budget, replaces a lifetime ledger, broadens assignment/source
authority, or invents evidence/findings.

## Terminal artifact

The canonical artifact contains:

- repository, PR, base/head SHA, deterministic artifact ID, schema version;
- policy/config digests and phase allocation/finalization reserve;
- validated assignment plan source/repair/overflow, assignments, durable session
  ownership, explicitly projected checkpoints, states, and lifetime budgets;
- bounded evidence provenance references and content hashes, never bodies,
  secrets, transcripts, or hidden model reasoning;
- every obligation and repository recipe with explicit terminal status;
- unknowns and typed source-access requests;
- accepted candidates, rejected candidates, all dispositions, and downgraded
  candidate unknowns;
- sparse handoff, typed note IDs/evidence references, deterministic verdict and
  source, degradation, publisher-ready state, and stable event references.

Artifact validation runs before the first byte is written. The same-directory
temporary file is private, flushed/fsynced, and replaced atomically. A failed
write never truncates or replaces the prior target. The resolved target must be
beneath `artifact_output_root`; traversal and external absolute targets fail
closed without touching the filesystem.

## Authority and invariant self-review

- The controller derives obligations once from immutable topology,
  classification, and current-head policy. It owns one central `CoverageLedger`
  and `EvidenceStore`; each wave worker receives private stores cloned from the
  exact wave snapshot, and only completed snapshots merge centrally.
- Planner JSON can only group work after `validate_assignment_plan`; one repair is
  bounded, then the established fallback takes over.
- Durable `SessionOwnership` is projected with the coverage module's public
  `session_ownership_for_assignment` helper. Negotiator JSON never supplies
  budgets, leases, counts, caps, statuses, or ownership.
- Every reconciliation receives the exact pre-wave coverage snapshot. Failed
  reconciliation restores that trusted baseline instead of retaining optimistic
  session mutations.
- Candidate publication and severity gating go through the existing changed-file,
  obligation, and retained-evidence authority checks. Critic/finalizer model
  output cannot create evidence or choose the final verdict.
- Exploration admission and follow-up feasibility use `RunDeadline`; final
  specialist/finalizer calls recheck the absolute deadline. No exploration uses
  the finalization reserve.
- Controller-issued session identities are independent of assignment IDs and
  bind factory construction, results, checkpoints, newly collected evidence,
  candidates, ownership, and `(assignment_id, session_id)` result keys.
- Scheduler completion order is normalized before controller events/artifact
  projection. A test with opposite completion order and different monotonic clock
  origins produces byte-equivalent semantic artifact mappings.
- Event payloads are sequence-owned, bounded in depth/items/string length,
  secret-redacted, and immutable after append.
- A controlled `request_changes` comes only from the existing severity,
  unresolved-high-risk, or approval-disabled policy. A controller notice has no
  blocking finding and is not publisher-ready.

## Verification

Focused controller/state:

```text
42 passed
```

Full specialist runtime:

```text
360 passed
```

Full Python suite with UTF-8 mode:

```text
1541 passed, 21 failed, 2 warnings
```

The 21 failures exactly match the approved Windows baseline categories: one
mode-bit assertion, evidence-provider shell/subprocess path behavior, and
extensionless fake-`gh` resolve fixtures. No specialist-runtime test failed.

Compilation and `git diff --check` completed with exit code 0.

## Remaining integration concern

Task 14 must construct the production session factory/model-role adapters, set
the CLI workspace as `artifact_output_root`, and pass the resulting
publisher-ready handoff/notes/verdict to Task 12. Production model calls must
continue using the existing gateway's absolute request deadlines and bounded
transport timeouts.
