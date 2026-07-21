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
- The terminal `specialist-review-artifact.json` is validated, canonical JSON,
  schema-versioned, written through a private same-directory temporary file,
  fsynced, and atomically replaced.
- `EventJournal` adds a thread-safe, append-only, bounded, redacted event owner.
  Observer failures cannot abort or erase a run.
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

No recovery grants budget, replaces a lifetime ledger, broadens assignment/source
authority, or invents evidence/findings.

## Terminal artifact

The canonical artifact contains:

- repository, PR, base/head SHA, deterministic artifact ID, schema version;
- policy/config digests and phase allocation/finalization reserve;
- validated assignment plan source/repair/overflow, assignments, durable session
  ownership, checkpoints, states, and lifetime budgets;
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
write never truncates or replaces the prior target.

## Authority and invariant self-review

- The controller derives obligations once from immutable topology,
  classification, and current-head policy and retains the same `CoverageLedger`,
  `EvidenceStore`, event journal, session objects, and lifetime ledgers throughout
  the run.
- Planner JSON can only group work after `validate_assignment_plan`; one repair is
  bounded, then the established fallback takes over.
- Durable `SessionOwnership` is projected with the existing coverage module's
  canonical assignment-ownership function. Negotiator JSON never supplies
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
27 passed
```

Full specialist runtime:

```text
344 passed
```

Full Python suite with UTF-8 mode:

```text
1525 passed, 21 failed, 2 warnings
```

The 21 failures exactly match the approved Windows baseline categories: one
mode-bit assertion, evidence-provider shell/subprocess path behavior, and
extensionless fake-`gh` resolve fixtures. No specialist-runtime test failed.

Compilation and `git diff --check` completed with exit code 0.

## Remaining integration concern

Task 14 must construct the production session factory/model-role callbacks and
pass the resulting publisher-ready handoff/notes/verdict to Task 12. Arbitrary
in-process test callbacks cannot be forcibly preempted; production model calls
must continue using the existing gateway's absolute request deadlines and
bounded transport timeouts. The controller rechecks phase/deadline authority at
every boundary and degrades late results deterministically.
