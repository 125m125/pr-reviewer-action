# Review Output Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Preserve specialist findings and make incomplete coverage distinguishable from concrete defects in human-facing review output.

**Architecture:** Keep deterministic coverage and adjudication state unchanged internally. Normalize candidate identity at the controller/adjudication boundary, filter generic coverage requests from published detail notes, derive a richer deterministic handoff fallback, and add bounded checkpoint diagnostics as additive artifact data.

**Tech Stack:** Python, pytest, existing specialist-runtime dataclasses and JSON artifact writers.

## Global Constraints

- Do not add runtime dependencies.
- Do not log raw model responses; diagnostics must be bounded and structured.
- Preserve existing artifact fields and strict internal coverage accounting.
- Every behavior change gets a failing regression test before production code.

### Task 1: Preserve colliding candidate IDs

**Files:**
- Modify: `pr_reviewer/specialist_runtime/controller.py`, candidate collection/consolidation boundary
- Modify: `pr_reviewer/specialist_runtime/adjudication.py`, candidate identity input handling
- Test: `tests/test_specialist_runtime_controller.py`, `tests/test_specialist_runtime_adjudication.py`

- [ ] Add a failing test proving two sessions that both emit `c1` reach adjudication as two distinct candidates rather than both being rejected.
- [ ] Run the focused tests and confirm the duplicate-ID assertion fails.
- [ ] Implement deterministic session-scoped IDs only for cross-session collisions, retaining original IDs in disposition metadata.
- [ ] Run focused tests and confirm both candidates remain available to the critic.

### Task 2: Aggregate coverage-only human output

**Files:**
- Modify: `pr_reviewer/specialist_runtime/controller.py`, coverage request construction and final note assembly
- Modify: `pr_reviewer/specialist_runtime/adjudication.py`, publication filtering if required by the existing note builder
- Test: `tests/test_specialist_runtime_controller.py`, `tests/test_specialist_runtime_adjudication.py`

- [ ] Add a failing test proving several generic unresolved obligations produce one aggregate handoff coverage warning and zero per-obligation detail notes.
- [ ] Run the focused test and confirm the current one-note-per-obligation behavior fails the assertion.
- [ ] Implement aggregation while preserving all obligation IDs in the artifact.
- [ ] Keep evidence-backed candidate verification requests publishable.
- [ ] Run focused tests.

### Task 3: Separate incomplete coverage from request-changes

**Files:**
- Modify: `pr_reviewer/enforcement.py` and the specialist controller verdict projection
- Test: `tests/test_specialist_runtime_policy.py`, `tests/test_specialist_runtime_controller.py`

- [ ] Add a failing test proving unresolved obligations without accepted findings do not select a defect-style request-changes verdict for publication.
- [ ] Run the focused test and confirm the current policy fails.
- [ ] Add an explicit coverage-incomplete outcome for publishing while retaining internal `evaluation_status` and blocking obligation IDs.
- [ ] Keep request-changes for accepted actionable findings or concrete verification requests.
- [ ] Run focused policy/controller tests.

### Task 4: Improve degraded handoff fallback

**Files:**
- Modify: `pr_reviewer/specialist_runtime/controller.py`, deterministic handoff projection
- Test: `tests/test_specialist_runtime_controller.py`, `tests/test_specialist_runtime_adjudication.py`

- [ ] Add a failing test proving a degraded run renders multiple behavioral sentences from a validated change overview instead of a file/component inventory or one generic sentence.
- [ ] Run the focused test and confirm the current fallback is too sparse.
- [ ] Implement bounded behavioral prose from validated overview facts, capped for sticky-comment size.
- [ ] Run focused handoff tests.

### Task 5: Emit bounded checkpoint diagnostics

**Files:**
- Modify: `pr_reviewer/specialist_runtime/session.py`, checkpoint finalization diagnostics
- Modify: `pr_reviewer/specialist_runtime/controller.py` or artifact projection for event serialization
- Test: `tests/test_specialist_runtime_session.py`, `tests/test_specialist_runtime_controller.py`

- [ ] Add a failing test proving retention uncertainty includes parse/repair/material-signal diagnostics without raw response text.
- [ ] Run the focused test and confirm diagnostics are currently absent.
- [ ] Add additive bounded diagnostic fields and journal payloads.
- [ ] Run focused diagnostics tests.

### Task 6: Full verification

- [ ] Run all specialist-runtime tests with `PYTHONPATH=.` and the repository virtualenv pytest executable.
- [ ] Run `git diff --check`.
- [ ] Review the artifact schema changes and ensure no raw model content is emitted.
