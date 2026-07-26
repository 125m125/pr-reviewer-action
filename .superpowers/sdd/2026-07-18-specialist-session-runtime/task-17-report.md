# Task 17 Report: Legacy specialist orchestration removal

## Result

Task 17 removes the obsolete specialist scheduler/report/candidate pipeline and
publication compatibility aliases after the Task 1–16 runtime and publisher
interfaces replaced them. The single-review strategy and its native tool loop
remain supported. The Task 14 one-release input aliases remain available for
migration warnings.

## Deletion and migration inventory

- Reduced `pr_reviewer/specialists.py` from 823 to 250 lines.
  - Removed the version-1 configuration facade.
  - Removed planner-focus normalization and validation.
  - Removed recipe and deterministic focus generation.
  - Removed exclusion, overlap, marginal-coverage, and candidate scheduling.
  - Removed report normalization, report-gap generation, finding validation,
    root-cause merging, policy notices, and JSON dumping.
  - Retained only repository topology and file-role implementation needed by
    the session runtime: `build_topology`, `classify_file_roles`, and their
    private/internal topology helpers.
- Removed `adaptive_loop_budgets` and the old `2 × configured rounds` mapping.
  `run_tool_harness.py` now constructs `LoopBudgets` directly, so
  `TOOL_MAX_ROUNDS` is the exact planning-turn limit. Tool-only turns still use
  the independent request budget, and no-progress/repeated-call-set stop rules
  are unchanged.
- Kept `scripts/run_specialist_reviews.py` as the existing 14-line
  path-bootstrap/import/exit wrapper. No orchestration was reintroduced.
- Removed publication compatibility surfaces:
  - removed the dead `publish_specialist_review()` shell forwarding function;
  - renamed the live shared position parser from `legacy_diff_positions` to
    `diff_positions` and updated the real single-review comment builder;
  - removed `resolve_finding_threads.extract_marker_fingerprint` and updated
    the real thread matcher/tests to call
    `github_review_notes.extract_managed_fingerprint` directly.
- Removed obsolete `tests/test_specialists.py` scheduler/report/candidate
  tests and retained topology/file-role coverage only.
- Removed the old `2 × rounds` assertions from
  `tests/test_native_tool_loop.py`; the actual loop behavior, no-progress
  cutoff, repetition cutoff, streaming, repair, and tool execution assertions
  remain unchanged.
- Updated `README.md`, `action.yml`, and the migration table to describe the
  durable session runtime, exact native-loop turn budget, and warning-only
  packet migration settings.
- Updated `scripts/sections/config.sh` only to document that `packet` is
  accepted for the Task 14 one-release warning and is not a runtime branch.
  No Task 14 alias was removed.
- Updated `scripts/sections/review.sh` to replace the stale
  `sequential hybrid plan` analysis-engine label with
  `durable session runtime`.
- Corrected two stale shell assertions discovered by the broad verification:
  `publish_mode` uses Task 14's strategy-aware empty omission sentinel, and
  escalation/enforcement calls are indented in the split review section.

Net task diff before this report: 175 additions and 1,056 deletions across
runtime, publishing, documentation, and tests.

## Call-site proof

Command:

```text
rg -n "from pr_reviewer\.specialists import" pr_reviewer scripts tests
```

Result:

```text
pr_reviewer/specialist_runtime/cli.py:22:from pr_reviewer.specialists import build_topology
pr_reviewer/specialist_runtime/coverage.py:13:from pr_reviewer.specialists import classify_file_roles
tests/test_specialists.py:3:from pr_reviewer.specialists import build_topology, classify_file_roles
```

No production caller used the removed specialist functions.

Command:

```text
rg -n "diff_positions|extract_managed_fingerprint|publish_specialist_review.py" \
  pr_reviewer scripts action.yml tests
```

Result summary:

- `scripts/build_review_comments.py` directly imports and uses
  `github_review_notes.diff_positions`.
- `scripts/resolve_finding_threads.py` directly imports and uses
  `github_review_notes.extract_managed_fingerprint`.
- `action.yml` directly invokes `scripts/publish_specialist_review.py`.
- No shell forwarding function remains.

Command:

```text
rg -n "adaptive_loop_budgets|SequentialModelRunner|def run_focus\(|\
initial_fallback_focuses|max_rounds=max\(4|def schedule_focuses|\
normalize_specialist_report|load_specialist_config|legacy_diff_positions|\
extract_marker_fingerprint|publish_specialist_review\(\)|\
sequential hybrid plan" pr_reviewer scripts action.yml README.md
```

Result: no matches.

`packet` matches remain intentionally limited to the documented one-release
input, config validation, CLI warning collection, and warning text. There is no
`if tool_mode == "packet"` execution path.

## Removed test to replacement-test mapping

| Removed legacy coverage | Task 1–16 replacement coverage |
|---|---|
| version-1 config loader and invalid-config tests | `test_v1_recipe_defaults_to_coverage_and_remains_named`, `test_v2_policy_rejects_unknown_top_level_key`, and `test_runtime_config_uses_direct_defaults_and_legacy_aliases` in `test_specialist_runtime_policy.py` |
| arbitrary planner focuses, singular lens repair, exclusions, recipe matching, and scheduler merging | assignment-schema/authority tests in `test_specialist_runtime_assignments.py`, especially `test_planner_cannot_omit_recipe_obligation`, `test_model_created_focus_preserves_recipe_identity`, `test_dedicated_and_independent_recipes_are_isolated`, and deterministic fallback-capacity tests |
| marginal recipe/fallback candidate scheduling | `test_matching_recipe_becomes_named_mandatory_obligations`, `test_topology_rules_include_artifacts_risks_and_component_interactions`, and recipe lifecycle tests in `test_specialist_runtime_coverage.py`; wave ordering/capacity tests in `test_specialist_runtime_scheduler.py` |
| deterministic fallback focuses | topology-derived obligation coverage plus `test_fallback_prioritizes_high_risk_and_keeps_capacity_overflow_explicit` and negotiation fallback tests |
| report-gap fresh-conversation behavior | `test_coverage_feedback_resumes_same_conversation_and_budget`, recovery lifetime-state tests, and checkpoint/finalization tests in `test_specialist_runtime_session.py` |
| coverage-gap string matching | evidence-predicate reconciliation tests in `test_specialist_runtime_negotiation.py`, `test_recipe_is_partial_until_every_obligation_has_evidence`, and session scope/evidence-category tests |
| report normalization and evidence/causal-chain filtering | checkpoint retained-evidence tests in `test_specialist_runtime_session.py` and `test_critic_cannot_publish_candidate_without_retained_evidence` / structured-note downgrade tests in `test_specialist_runtime_adjudication.py` |
| candidate scope, deduplication, critic rejection, and root-cause merging | adjudication fingerprint, critic authority, provenance-preserving merge, and missing-evidence anti-laundering tests in `test_specialist_runtime_adjudication.py` |
| legacy diff parser and policy notice | `test_diff_positions_preserves_linux_backslash_filename`, Task 12 anchor/publisher tests in `test_github_review_notes.py`, and CLI policy/warning tests in `test_specialist_runtime_cli.py` |
| specialist runner orchestration tests | durable session continuity/recovery tests, scheduler/controller tests, and `test_specialist_runner_is_only_a_path_bootstrap_and_cli_wrapper` |
| old `2 × rounds` mapping tests | Task 17 dead-pattern guard plus the unchanged native-loop budget, no-progress, repetition, and wiring regressions |

No live assertion was weakened to preserve a removed implementation. The two
shell-test changes align stale assertions with already-shipped Task 14 and
section-split behavior.

## TDD record

### RED

Command:

```text
.\.venv\Scripts\python.exe -m pytest \
  tests/test_specialist_runtime_cli.py::test_removed_specialist_architecture_is_not_present -v
```

Result: failed as intended on `def schedule_focuses(`. The initial bare
`pytest` attempt failed because `pytest` was not on PowerShell's PATH; rerunning
through the repository virtualenv produced the expected architecture failure.

The broad shell sweep also supplied RED evidence for two stale tests:

- `test_approval_guardrails.sh` failed because it expected
  `publish_mode: comment` instead of Task 14's empty omission sentinel.
- `test_review_escalation.sh` stopped because its unindented grep did not match
  the split review section's indented calls.

### GREEN

Dead-pattern guard:

```text
1 passed
```

Focused specialist runtime:

```text
pytest tests/test_specialist_runtime_*.py tests/test_specialists.py \
  tests/test_specialist_runner.py -q
468 passed in 7.50s
```

Native loop and action regressions:

```text
pytest tests/test_native_tool_loop.py tests/test_run_native_loop_wiring.py \
  tests/test_tool_max_requests.py tests/test_action_inputs.py \
  tests/test_action_shell_syntax.py -q
91 passed in 2.17s
```

Focused publisher on POSIX:

```text
pytest tests/test_github_review_notes.py tests/test_build_review_comments.py \
  tests/test_resolve_finding_threads.py tests/test_api_key_argv.py -q
183 passed in 1.91s
```

Full POSIX Python suite:

```text
pytest tests/ -q --tb=short
1614 passed in 47.45s
```

The WSL run emitted one pytest cache warning because the Windows-created
`.pytest_cache` was not writable from the mounted Linux environment; test
execution and workspace files were unaffected. A Windows full run reached
1,593 passing tests and 21 expected POSIX-host failures (Bash command
execution, extensionless fake `gh`, and POSIX chmod); the isolated WSL run
resolved all 21.

Standalone shell behavior tests:

```text
for f in tests/test_*.sh; do bash "$f"; done
24 scripts passed
```

The external `smoke_test.sh` and `forgejo_e2e_smoke.sh` were intentionally not
run; they require live external services and are not part of the standalone
local behavior set.

Static verification:

```text
python -m compileall -q pr_reviewer scripts tests
git diff --check
bash -n scripts/sections/config.sh scripts/sections/review.sh \
  scripts/publish_helpers.sh tests/test_approval_guardrails.sh \
  tests/test_review_escalation.sh
```

All completed with exit code 0 and no output.

## Self-review

- Confirmed `scripts/run_specialist_reviews.py` is still exactly the Task 14
  bootstrap/import/exit wrapper and contains no orchestration.
- Confirmed the specialist action arm invokes the Task 12 structured publisher
  directly.
- Confirmed the two single-review publishing scripts remain because they have
  live action callers; only compatibility aliases/wrappers were removed.
- Confirmed `TOOL_MAX_ROUNDS` now maps directly to
  `LoopBudgets.max_rounds`; the unchanged no-progress and repeated-call-set
  tests pass.
- Confirmed `specialist_config_file`,
  `specialist_max_initial_passes`,
  `specialist_max_followup_passes`,
  `specialist_max_tool_calls_per_pass`, `specialist_tool_mode=packet`, and
  `specialist_packet_max_bytes` remain documented migration inputs/warnings.
- Confirmed no new project dependency or manifest change.
- Confirmed `git diff --check`, Python compilation, Bash syntax, focused tests,
  full tests, and local shell tests are clean.

## Preserved files and branch

- Branch preserved: `codex/specialist-session-runtime`.
- The two pre-existing untracked July 12 documents remain unmodified and
  untracked:
  - `docs/superpowers/plans/2026-07-12-stream-loop-watchdog.md`
  - `docs/superpowers/specs/2026-07-12-large-review-reliability-design.md`
