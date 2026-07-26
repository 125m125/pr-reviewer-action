# Task 16 report: representative specialist-runtime replay

## Outcome

Implemented an offline, deterministic acceptance replay for a multilingual
pull request and an adversarial web-policy fixture. The replay drives the
public specialist-runtime components and emits their schema-v2 artifact
projection; the eval harness grades that projection and returns a failing exit
status when an acceptance gate is violated.

The representative run passes all gates with 27/27 mandatory obligations
accounted for, both version-1 recipes migrated and covered, one expected
accepted finding, retained evidence at the current head, and no unsupported
public claims, unsafe fetches, budget resets, or deadline violations. The
recorded critic failure deliberately makes the run degraded while still
producing a conservative publishable result.

## Architecture and separation

- `pr_reviewer/specialist_runtime/replay.py` (770 lines): fixture validation,
  recorded provider adapters, deterministic read-only executor, the public
  `ReviewController`/`SpecialistSession` replay, artifact-v2 projection, and
  secure web-policy replay. Recorded provider data is declared as
  `openai-chat-completions` and materialized at the existing
  `ModelTurnResult` gateway boundary.
- `pr_reviewer/specialist_runtime/replay_adversarial.py` (332 lines):
  deterministic failure injections against the public session, coverage,
  evidence, adjudication, and review-note components.
- `scripts/eval_harness.py` (1,161 lines total; Task 16 delta 365 additions,
  4 deletions): corpus wiring, metrics, acceptance gates, report rendering,
  CLI selection, and exit policy only. It does not implement another runtime.
- `tests/test_specialist_runtime_replay.py`: acceptance, mutation-gate, web
  policy, fixture validation, corpus registration, and offline CLI tests.

The runtime path uses the real policy loader/migrator, obligation derivation,
controller, specialist session, evidence store, coverage merge, adjudication,
review-note projection, source policy, secure fetcher, and public artifact-v2
projection. Only model/transport responses, tool results, ordering, and the
clock are deterministic adapters.

## Fixture schema

`tests/fixtures/specialist_runtime/provider-turns.json` has:

- `schema_version: 1`
- `api_format: openai-chat-completions`
- `scenarios.multilingual` with `planner`, `specialist`, `critic`, and
  `finalizer` recorded semantic turns
- adversarial scenarios for `no_progress_resume`, `reconstruction`,
  `planner_repair`, `failed_critic`, `deadline_cutoff`,
  `completion_inversion`, and `note_anchor_race`

`multilingual-pr/fixture.json` supplies repository/head identity, runtime
budgets and phase shares, cross-language topology, a version-1 policy, exact
expected obligations, recipe statuses, finding IDs, acceptable unknowns, and
forbidden public text. The PR fixture contains:

- Java API and test
- TypeScript consumer and test
- Python messaging worker and test
- JSON schema contract
- SQL migration
- Kubernetes deployment
- version-1 cross-language and deployment recipes
- an initially invalid planner result followed by one sharper,
  model-created validated assignment

All 27 derived obligations are mandatory in this acceptance fixture.

`web-policy-pr/fixture.json` records one approved official source, one
unapproved result with a forbidden snippet, an allowlisted-host redirect to an
unapproved host, and a separate source-access request. Discovery metadata is
non-evidentiary, unapproved title/snippet content is never projected, the
redirect body is never consumed, the hostile host is never fetched, and the
access request becomes a real detailed `ReviewNote`.

## Offline acceptance metrics and gates

The report includes:

- obligation accounting and terminal statuses
- recipe statuses
- unsupported public claims
- accepted/rejected candidates and missing expected findings
- unknown accounting
- retained/referenced evidence and head-SHA agreement
- line/file/general review-note anchor counts
- source denials, access requests, and unsafe fetch attempts
- specialist/controller model turns, tool calls, recoveries, and budget
  histories
- simulated elapsed time, deadline, phase timing, and finalization reserve

Failure gates cover artifact schema, missing/invalid mandatory status,
obligation mismatch, recipe mismatch, unsupported public claims, unsafe
fetches, budget reset/exhaustion, deadline violation, missing expected
findings, unexpected unknowns, missing evidence, and head mismatch.

Mutation tests explicitly prove rejection for missing mandatory status,
unsupported public claim, unsafe fetch, decreased same-session budget
history, deadline overrun, missing expected finding, missing retained
evidence, and mismatched head SHA.

## Adversarial terminal observations

- Duplicate/no-progress checkpoint: resumes the same session; counters advance
  from four to six model turns and never reset.
- Repetitive transcript: reconstructs once with the recorded
  `repetitive-transcript` reason and retains the checkpoint.
- Invalid planner: exactly one repair request; source is
  `model_repaired_validated`.
- Failed critic: reaches a terminal degraded state and uses the conservative
  fallback.
- Deadline cutoff: reserves finalization time and does not cross the deadline.
- Concurrent completion inversion: stable coverage/evidence projection for
  both completion orders.
- Publishing note anchor race: stable line/file anchor results for both
  completion orders.

## TDD and verification evidence

RED observations:

- Initial replay test collection failed because
  `evaluate_specialist_replay` did not exist.
- The offline CLI test failed because `--offline-specialist-only` was absent.
- Gate mutation tests failed before the finding/evidence/head gates and
  mapping-aware budget-history checks were added.

GREEN commands:

```text
python -m pytest tests/test_specialist_runtime_replay.py -q
16 passed in 1.40s

python -m pytest tests/test_specialist_runtime_replay.py tests/test_native_loop_exfil_redteam.py tests/test_specialist_runtime_web.py -q
116 passed in 1.75s

python -m pytest tests/test_eval_harness.py tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_session.py tests/test_specialist_runtime_state.py -q
152 passed in 1.98s

python -m pytest tests -k specialist_runtime -q
426 passed, 1184 deselected in 4.27s
```

Offline corpus command:

```text
python scripts/eval_harness.py --corpus evals/corpus-agentic.json --offline-specialist-only --output <workspace-report>
PASS; 27 obligations; 2 covered recipes; 1 accepted finding; 0 unsafe fetches; 3 specialist model turns; 4 controller requests; 27 tool calls; 0 recoveries; 9.7/300 simulated seconds; 30-second finalization reserve
```

Fresh full Windows baseline:

```text
python -m pytest tests/ -q
21 failed, 1589 passed, 2 warnings in 16.74s
```

This exactly matches the approved 21-failure Windows baseline: one POSIX mode
assertion in `test_api_key_argv.py`, twelve Bash/Windows evidence-provider
execution failures, and eight fake-`gh` finding-thread failures. No Task 16 or
specialist-runtime test failed.

## Hygiene

`git diff --check` is clean. The two pre-existing untracked July 12 documents
were not read, modified, staged, or removed:

- `docs/superpowers/plans/2026-07-12-stream-loop-watchdog.md`
- `docs/superpowers/specs/2026-07-12-large-review-reliability-design.md`

No dependencies or network access were added.

# Review round 1 fixes (2026-07-26)

All six review findings were resolved. The replay now treats fixture
expectations only as private comparisons and obtains observations through the
same public controller, session, gateway, parser, evidence, and publishing
boundaries used by the runtime.

## Finding disposition

### Candidate provenance and adjudication

- Removed the fixture-created `CandidateFinding` and the
  `ReviewInputs.candidate_findings` injection path from the replay.
- Extended the public `SpecialistSession` checkpoint schema to carry complete
  candidate objects. The session admits a candidate only when its ID was
  declared by the same checkpoint, its supporting evidence is retained, all
  cited evidence belongs to the assigned obligation set, and the candidate has
  non-empty related obligations and review detail.
- Collector identity is stamped by the live session and model identity is
  derived from retained evidence. The controller remains the sole adjudicator.
- A corrupt recorded checkpoint/final turn that omits the candidate now
  produces no accepted candidates and trips `missing_expected_finding`.
- A direct session test proves that a forged candidate with unretained evidence
  is discarded while the evidence-backed candidate is collected.

### Public-claim authorization

- The evaluator no longer trusts `observed.unsupported_public_claims`.
- It derives unsupported claims independently from all three public surfaces:
  controller handoff markdown, review notes, and accepted findings.
- Accepted findings are checked against retained evidence IDs, citation
  category/tool/hash/source provenance, assigned obligations, and collector
  identity. Handoff and note text must match the deterministic controller
  projections of those structured records.
- Separate mutations inject a novel claim into each public surface and each
  mutation activates the mandatory `unsupported_public_claim` gate. A caller
  supplied flag is deliberately ignored.

### Real completion inversion and deadline cutoff

- Both completion-order cases execute a full public `ReviewController` with two
  public `SpecialistSession` instances. A `threading.Event` controls the
  checkpoint release order; there are no timing sleeps or wall-clock races.
- The two controller artifacts are compared for coverage and retained-evidence
  equality, both requested completion orders must be observed, both runs must
  be terminal, and all recorded gateway turns must be consumed.
- The deadline case executes another full controller/session run using a
  thread-safe fake monotonic clock. The first provider turn advances exactly to
  the initial cutoff. The run finishes at 14/20 simulated seconds, preserves a
  two-second finalization reserve, consumes both recorded turns, and reaches a
  terminal state without crossing the deadline.

### Mandatory adversarial gates and CLI behavior

- Every adversarial predicate is now enumerated and mandatory: same-session
  resume, no budget reset, checkpoint reconstruction, planner repair source,
  conservative critic fallback, deadline cutoff/reserve/terminal state,
  completion coverage/evidence/order/terminal state, and stable note anchors.
- A false or missing predicate adds `adversarial_failure`; the offline CLI
  returns exit code 2.
- An in-process CLI mutation test flips a measured completion predicate and
  proves the non-zero exit and report gate.

### Web policy registration

- Registered `web-source-policy` as a second typed offline corpus entry.
- Its replay measures one allowlisted fetch, two denials, one source-access
  request, and zero unsafe transport attempts through the public discovery and
  fetch-policy functions.
- The evaluator compares those measurements to private expectations, rejects
  unsafe attempts and leaked discovery/redirect text, and exposes the measured
  web metrics in the offline report.
- A CLI mutation changes the measured unsafe-attempt count to one and proves
  exit code 2 with the `unsafe_fetch` gate.

### Recorded provider boundary

- Replaced provider behavior macros with explicit OpenAI-compatible
  `choices[].message` response bodies, including tool calls, structured JSON
  turns, and the critic error body.
- `_RecordedProvider` and the adversarial `_RecordedGateway` use the production
  `OpenAIModelGateway` parser boundary. They validate request role, tool
  enablement, response-schema name, OpenAI message/tool/structured-output
  payload shape, exact request order, and complete turn consumption.
- Planner assignments, specialist tool calls, checkpoints, candidates, critic
  failure, and finalizer output are all explicit recorded data. Corrupt request
  expectations and corrupt turn bodies now fail replay.
- Removed the multilingual fixture's detailed expected finding body; private
  expectations retain only finding IDs and acceptance counts.

## Expected-versus-observed audit

- Expectations: obligation IDs, recipe IDs, mandatory IDs, expected finding
  IDs, acceptable unknown IDs, deadline, head SHA, and web comparison counts.
- Observations: controller artifact, session-collected candidates, retained
  evidence, public notes/handoff, gateway request log, controller timing and
  budget state, adversarial controller outputs, and measured web transport
  requests.
- Removed synthetic runtime observations for unsupported claims and zero-valued
  web metrics. Runtime source metrics default to empty because that fixture has
  no web activity; the registered web fixture supplies the measured values.

## TDD evidence for the fix round

RED:

```text
python -m pytest tests/test_specialist_runtime_replay.py -q
11 failed, 12 passed
```

The failures reproduced the missing real completion/deadline observations,
provider macros, candidate-input injection, caller-controlled unsupported
claims, non-mandatory adversarial predicates, and absent web corpus entry.

```text
python -m pytest tests/test_specialist_runtime_session.py::test_checkpoint_collects_only_evidence_backed_candidate_objects -q
1 failed
```

The forged candidate remained visible in the checkpoint and the session
collected no candidate objects before the public session hook was added.

GREEN:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_specialist_runtime_replay.py -q
26 passed in 3.09s

.\.venv\Scripts\python.exe -m pytest tests/test_specialist_runtime_replay.py tests/test_native_loop_exfil_redteam.py tests/test_specialist_runtime_web.py -q
126 passed in 3.46s

.\.venv\Scripts\python.exe -m pytest tests/test_eval_harness.py tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_session.py tests/test_specialist_runtime_state.py -q
153 passed in 2.04s

.\.venv\Scripts\python.exe -X utf8 -m pytest tests -k specialist_runtime -q
437 passed, 1184 deselected in 5.90s
```

Offline CLI:

```text
.\.venv\Scripts\python.exe scripts\eval_harness.py --corpus evals\corpus-agentic.json --offline-specialist-only --output <workspace-report>
Offline specialist replays: 2 (PASS)
```

Measured report highlights: runtime 27/27 obligations covered, one accepted
evidence-backed finding, no unsupported claims, no failed adversarial
predicates; web 1 approved fetch, 2 denials, 1 source-access request, 0 unsafe
attempts, and no leaked text.

Fresh full Windows baseline:

```text
.\.venv\Scripts\python.exe -X utf8 -m pytest tests -q
21 failed, 1600 passed, 2 warnings in 18.37s
```

The same platform-only baseline remains: one POSIX mode assertion, twelve
Bash/Windows evidence-provider execution failures, and eight fake-`gh`
finding-thread failures. No specialist-runtime test failed.

## Module responsibility audit

- `replay.py` (658 lines): fixture validation, recorded main provider adapter,
  runtime replay orchestration, and measured web-policy replay.
- `replay_adversarial.py` (656 lines): explicit recorded failure providers plus
  real public session/controller adversarial executions.
- `session.py` (852 lines): public specialist lifecycle, now including
  evidence-backed checkpoint candidate collection.
- `eval_harness.py` (1513 lines): offline corpus dispatch and independent
  acceptance/security/adversarial/web evaluation.

The split keeps provider transport/parsing in the existing gateway and model
parser rather than duplicating those responsibilities in replay code.

## Final hygiene

`git diff --check` is clean. No dependency or network access was added. The two
pre-existing untracked July 12 documents remain untouched and unstaged.

# Review round 2 fixes (2026-07-26)

The remaining handoff-authorization gap is closed. Sparse handoff markdown is
now rendered by one production projection and the evaluator independently
reconstructs that projection from authoritative artifact state.

## Root cause and correction

`_unsupported_handoff_lines` previously accepted aggregate-theme and
source-request lines by prefix. It also treated the handoff's own
`thread_status`, `coverage_warning`, change-map, focus, and emphasis fields as
authorization for their rendered text. An attacker could therefore mutate both
a structured field and its markdown line and make unsupported content appear
self-consistent.

The correction separates authority from rendering:

- `render_review_handoff` is now the single production renderer for the entire
  sparse handoff.
- `ReviewHandoff` retains the structured status and the three focus partitions
  (specialist, recipe, and coverage-boundary) instead of only their flattened
  union. The publishing parser and payload projection preserve these fields.
- The controller records every filtered finalizer focus field in its event
  journal. The evaluator derives change-map/focus/emphasis values from that
  controller event rather than from handoff markdown.
- Recommendation and status come from the artifact verdict/evaluation state.
  Thread count and highest severity come from accepted findings. Aggregate
  theme is permitted only for at least two accepted findings with the same
  recognized production orientation category. Material-warning text comes
  from degradation state.
- Source-request count is reconstructed with
  `build_source_access_request_notes`, which uses the same production
  obligation, URL, host, and deduplication validation as handoff/note
  construction. The handoff field cannot manufacture a count.
- Every structured field is compared to the reconstructed projection, then the
  complete normalized markdown is compared to the production render. There
  are no prefix-authorized public lines.

Sparse summary semantics are unchanged: detailed claims and evidence remain in
typed findings/notes rather than the handoff.

## TDD evidence

RED:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_specialist_runtime_replay.py -k forged_structured_handoff_lines -q
6 failed, 26 deselected in 0.96s
```

All initial mutations passed through without a failure gate: forged
source-request value, forged source-request count, forged aggregate-theme
value, aggregate theme with only one finding, forged thread status, and forged
material-warning text.

The mutation matrix was then extended to cover the remaining structured lines:
change map, human-review emphasis, specialist focus, repository recipe focus,
and coverage-boundary focus. A positive control adds one valid source request
for a real obligation and proves the derived count remains accepted.

GREEN:

```text
.\.venv\Scripts\python.exe -m pytest tests\test_specialist_runtime_replay.py -k "forged_structured_handoff_lines or source_request_count_derived" -q
12 passed, 26 deselected in 1.70s

.\.venv\Scripts\python.exe -m pytest tests\test_specialist_runtime_replay.py tests\test_eval_harness.py -q
86 passed in 4.77s

.\.venv\Scripts\python.exe -m pytest tests\test_specialist_runtime_replay.py tests\test_native_loop_exfil_redteam.py tests\test_specialist_runtime_web.py -q
138 passed in 5.10s

.\.venv\Scripts\python.exe -X utf8 -m pytest tests -k specialist_runtime -q
449 passed, 1184 deselected in 7.65s

.\.venv\Scripts\python.exe -m pytest tests\test_github_review_notes.py -q
115 passed in 1.03s

.\.venv\Scripts\python.exe -m pytest tests\test_specialist_runtime_controller.py tests\test_specialist_runtime_cli.py -q
71 passed in 2.17s
```

Offline corpus:

```text
.\.venv\Scripts\python.exe scripts\eval_harness.py --corpus evals\corpus-agentic.json --offline-specialist-only --output <workspace-report>
Offline specialist replays: 2 (PASS)
```

`git diff --check` is clean. No dependencies or network access were added. The
two pre-existing untracked July 12 documents remain untouched and unstaged.
