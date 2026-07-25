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
