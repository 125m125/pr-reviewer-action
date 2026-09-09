# Prioritized Diff and Handoff Diagnostics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep large pull-request reviews useful by prioritizing bounded diff sections, allowing repository-local ordering overrides, and publishing human-readable handoff diagnostics.

**Architecture:** A small Python selector will receive the authoritative changed-file manifest and raw diff, rank only those file sections, and emit a bounded diff with explicit omission markers. Shell configuration will load an optional project priority file without changing access boundaries. Handoff rendering will consume structured effect summaries rather than Python object representations, while planner fallback reasons will remain visible in the structured runtime summary.

**Tech Stack:** Bash workflow sections, Python 3 standard library, pytest, shell behavior tests.

## Global Constraints

- Priority configuration may only reorder or quota paths already present in the authoritative PR diff.
- Omitted or truncated content must be explicitly marked and must not be treated as evidence that content is absent.
- Raw model responses and secrets must not be copied into workflow logs or public comments.
- The deterministic specialist plan remains authoritative when optional planning fails.

---

### Task 1: Priority selector and configuration tests

**Files:**
- Create: `pr_reviewer/diff_prioritizer.py`
- Create: `scripts/prioritize_diff.py`
- Test: `tests/test_diff_prioritizer.py`
- Modify: `scripts/sections/config.sh`
- Test: `tests/test_review_scope.sh`

- [x] Write failing tests for default category ordering, byte quotas, omitted-path markers, and project glob overrides.
- [x] Run the focused tests and verify they fail because the selector/configuration does not exist.
- [x] Implement the minimal selector using only changed-file paths and per-file diff sections.
- [x] Run the focused tests and verify they pass.

### Task 2: Integrate prioritized diff into corpus construction

**Files:**
- Modify: `scripts/sections/context.sh`
- Modify: `scripts/sections/config.sh`
- Modify: `scripts/sections/corpus.sh`
- Modify: `.github/workflows/ai-pr-review.yaml`
- Test: `tests/test_ai_pr_review_workflow.py`

- [x] Add the optional `.github/ai-review-diff-priorities.json` input/default and invoke the selector after the authoritative diff and file manifest are collected.
- [x] Replace the blunt full-diff truncation in the corpus with the prioritized output while retaining the bounded changed-file metadata index.
- [x] Add explicit configuration documentation and omission markers.
- [x] Run workflow and shell syntax/input tests.

### Task 3: Clean handoff prose and planner diagnostics

**Files:**
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Test: `tests/test_specialist_runtime_controller.py`
- Test: `tests/test_specialist_runtime_cli.py`

- [x] Add failing regression coverage proving cross-component dictionaries never appear in `what_changed` prose and planner fallback reasons appear in the bounded runtime summary.
- [x] Extract effect summaries from structured mappings instead of stringifying mappings.
- [x] Preserve structured cross-component effects for specialist orientation while keeping them out of human prose unless rendered as a sentence.
- [x] Project planner fallback diagnostics into `specialist-review-summary.md` without exposing raw model output.
- [x] Run focused specialist-runtime tests.

### Task 4: Verification

- [x] Run the prioritizer, workflow, shell, and specialist-runtime focused suites.
- [x] Run the full Python test suite (1929 passed; 21 existing Windows-environment failures in chmod/subprocess/`gh` integration tests).
- [ ] Review the final diff for boundary and secret-handling regressions.
