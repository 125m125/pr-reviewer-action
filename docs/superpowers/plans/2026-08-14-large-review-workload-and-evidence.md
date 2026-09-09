# Large Review Workload and Evidence Implementation Plan

> Approved design: `docs/superpowers/specs/2026-08-14-large-review-workload-and-evidence-design.md`

## Objective

Make large specialist reviews bounded, semantically focused, and useful without
weakening atomic coverage accounting, evidence authority, or secret handling.
Each task follows RED -> minimal GREEN -> focused regression -> commit.

## Task 1: Preserve source semantics while redacting real secrets

**Files:**

- Modify: `scripts/redact.py`
- Modify: `pr_reviewer/specialist_runtime/evidence.py`
- Test: `tests/test_redact.py`
- Test: `tests/test_specialist_runtime_evidence.py`
- Test: `tests/test_dogfood_canaries.py`

1. Add failing tests proving `{api_token}`, `$TOKEN`, `${TOKEN}`, `%TOKEN%`, and
   identifier expressions survive repository-source redaction, while literal
   credential values remain masked with their key/operator intact.
2. Add a source-aware redaction result/metadata path. Keep the existing generic
   log redactor compatible, but use syntax-preserving masking for repository
   source and diff evidence.
3. Assert model-facing evidence identifies controller redaction and never makes
   the replacement look like application behavior.
4. Run the three focused test modules and commit.

## Task 2: Build bounded review families and enforce a global lease

**Files:**

- Modify: `pr_reviewer/specialist_runtime/coverage.py`
- Modify: `pr_reviewer/specialist_runtime/assignments.py`
- Modify: `pr_reviewer/specialist_runtime/policy.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/specialist_runtime/types.py`
- Test: `tests/test_specialist_runtime_coverage.py`
- Test: `tests/test_specialist_runtime_assignments.py`
- Test: `tests/test_specialist_runtime_controller.py`
- Test: `tests/test_specialist_runtime_policy.py`

1. Add failing tests for deterministic family stability, isolation of critical
   and independent obligations, size splitting, and omission of duplicate
   per-file implementation obligations for non-production roles.
2. Introduce controller-owned family metadata while retaining atomic obligation
   IDs and statuses. Fan out a family assessment only after validating every
   member; retain unmatched members as open.
3. Add failing tests for adaptive desired session counts and shared global limits
   of 320 model turns and 640 tool calls. Verify `max_sessions` remains the hard
   cap and exhausted normal work becomes a coverage limit.
4. Add global lease accounting and risk-priority admission without multiplying
   limits by session count. Include family/workload and lease diagnostics.
5. Run the four focused modules and commit.

## Task 3: Keep planner context useful and batch related diff reads

**Files:**

- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Modify: `pr_reviewer/specialist_runtime/assignments.py`
- Modify: `pr_reviewer/conversation.py`
- Modify: `pr_reviewer/tool_executors.py`
- Modify: `pr_reviewer/specialist_runtime/session.py`
- Test: `tests/test_specialist_runtime_cli.py`
- Test: `tests/test_specialist_runtime_assignments.py`
- Test: `tests/test_tool_harness_command_guardrails.py`
- Test: `tests/test_specialist_runtime_session.py`

1. Add failing planner-projection tests showing a non-empty manifest always
   retains assignment/family summaries, changed-path counts, components, and
   roles before optional detail. Add prompt assertions explaining safe merge to
   free split capacity.
2. Reorder and bound planner projection accordingly, with retained/omitted
   diagnostics.
3. Add failing schema/executor tests for `read_pr_diff(paths=[...])`: maximum
   eight authorized paths, compatibility with `path`, immutable revision use,
   shared byte cap, per-path status/truncation, and separate evidence records.
4. Implement batching and update specialist guidance to prefer related
   production/test paths.
5. Run focused tests and commit.

## Task 4: Make checkpoints and compacted evidence converge

**Files:**

- Modify: `pr_reviewer/specialist_runtime/session.py`
- Modify: `pr_reviewer/specialist_runtime/types.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Test: `tests/test_specialist_runtime_session.py`
- Test: `tests/test_specialist_runtime_controller.py`
- Test: `tests/test_specialist_runtime_replay.py`

1. Add failing tests for canonical extraction of exactly one checkpoint object,
   ignored controller-owned extras, validated legacy obligation updates, and no
   authoritative state mutation from unknown keys.
2. Add a semantic progress fingerprint based on candidate lifecycle, accepted
   assessments, retained evidence, and concrete next actions. Prove reworded
   summaries do not reset the no-progress guard.
3. Extend `read_compacted_evidence` with required controller-owned target and
   purpose enum. Add failing tests for first recovery, repeated recovery denial,
   state-change reauthorization, pinned recovered evidence, and no-progress
   accounting.
4. Implement the retrieval ledger and bounded diagnostics; preserve old
   checkpoints through canonicalization.
5. Run focused session/controller/replay tests and commit.

## Task 5: Correct clean-review and deterministic handoff semantics

**Files:**

- Modify: `pr_reviewer/enforcement.py`
- Modify: `pr_reviewer/specialist_runtime/adjudication.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/github_review_notes.py`
- Test: `tests/test_enforcement.py`
- Test: `tests/test_specialist_runtime_adjudication.py`
- Test: `tests/test_specialist_runtime_controller.py`
- Test: `tests/test_github_review_notes.py`

1. Add failing matrix tests for blocking findings, concrete verification notes,
   incomplete high-risk coverage, and clean complete coverage with approval
   disabled.
2. Separate recommendation from GitHub event authorization so a clean review
   says `No blocking findings identified` and publishes a COMMENT when approval
   is disabled.
3. Add failing handoff tests for absent/rejected model summaries. Build a concise
   factual `What the AI reviewed` fallback from covered families, accepted
   conclusions, changed evidence paths, and degraded stages; forbid correctness,
   completeness, and merge-safety claims.
4. Run focused publishing tests and commit.

## Task 6: Expose configuration, migrate projects, and verify end to end

**Files:**

- Modify: `action.yml`
- Modify: `scripts/sections/config.sh`
- Modify: `.github/workflows/ai-pr-review.yaml`
- Modify: `README.md`
- Modify: `docs/migrations/specialist-session-runtime.md`
- Modify: specialist artifact/diagnostic projection as needed
- Test: `tests/test_action_inputs.py`
- Test: `tests/test_ai_pr_review_workflow.py`
- Test: relevant specialist runtime and replay suites

1. Add failing input/config tests for total model/tool leases and their defaults.
2. Wire the inputs end to end. Set dogfood to 12 sessions, 320 total model turns,
   and 640 total tool calls. Document why tools exceed turns and how downstream
   projects should select limits.
3. Add artifact/log assertions for families, global consumption, planner
   omissions, multi-path reads, checkpoint normalization/progress, compacted
   recovery decisions, source-redaction counts, and handoff source.
4. Run the affected suites, then `pytest tests/ -q` and `git diff --check`.
   Classify any environment-only failures explicitly rather than hiding them.
5. Request a focused code review, fix only concrete reachable defects, rerun
   verification, commit runtime changes, then make a separate workflow pin-only
   commit pointing at the runtime commit SHA. Do not push.

