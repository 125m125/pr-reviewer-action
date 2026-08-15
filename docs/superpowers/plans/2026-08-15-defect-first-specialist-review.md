# Defect-First Specialist Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retain concrete defects immediately and simplify the bookkeeping and summary protocols that currently displace defect discovery.

**Architecture:** Extend the existing durable-session tool surface rather than adding a service. Keep assignment validity, candidate ownership, checkpoint validation, and summary provenance controller-owned.

**Tech Stack:** Python 3, pytest, GitHub composite action configuration.

## Global Constraints

- No new dependency or model-owned authority.
- Candidate identifiers are short session-local controller handles.
- Existing checkpoint and artifact formats remain backward compatible.
- Use strict RED/GREEN cycles for every behavior change.

---

### Task 1: Immediate candidate lifecycle

**Files:**
- Modify: `pr_reviewer/specialist_runtime/session.py`
- Test: `tests/test_specialist_runtime_session.py`

**Interfaces:**
- Produces: exploration tools `report_candidate` and `withdraw_candidate`.
- Retains: `candidate_findings` and `_candidate_statuses` as controller state.

- [ ] Add failing tests for reporting, withdrawing, ownership, and audited tombstones.
- [ ] Run the focused tests and confirm failures come from missing tools.
- [ ] Implement the two local tools using the existing candidate parser and evidence validation.
- [ ] Run the focused session tests to green.

### Task 2: Balanced assignment-local work

**Files:**
- Modify: `pr_reviewer/specialist_runtime/assignments.py`
- Test: `tests/test_specialist_runtime_assignments.py`

**Interfaces:**
- Produces: ordinary assignments containing at most six obligations.
- Produces: `changed_context` ranked against each assignment's scope and seeds.

- [ ] Add failing tests for a 19-obligation ordinary group and documentation-heavy changed manifests.
- [ ] Confirm the existing plan leaves an oversized group or irrelevant context first.
- [ ] Split ordinary groups deterministically and rank local changed context.
- [ ] Run assignment tests to green.

### Task 3: Tolerant compact checkpoints

**Files:**
- Modify: `pr_reviewer/specialist_runtime/session.py`
- Test: `tests/test_specialist_runtime_session.py`

**Interfaces:**
- Consumes: the existing obligation assessment ledger.
- Produces: normalized update dictionaries and bounded validation diagnostics.

- [ ] Add failing tests for safe aliases, omitted empty arrays, implicit accepted decisions, and exact repair diagnostics.
- [ ] Confirm current parsing rejects the observed near-miss outputs.
- [ ] Normalize only the approved aliases/defaults and record the rejection reason at the validation site.
- [ ] Run session tests to green.

### Task 4: Small aligned summary contracts

**Files:**
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Test: `tests/test_specialist_runtime_controller.py`
- Test: `tests/test_specialist_runtime_cli.py`

**Interfaces:**
- Produces: at most five change groups.
- Produces: a two-string handoff proposal with deterministic provenance.

- [ ] Add failing tests for numeric change limits, two-field handoff input/output, and path-free validated AI summaries.
- [ ] Confirm the current model contracts require oversized arrays or discard path-free prose.
- [ ] Shrink prompts/schemas and attach provenance in controller code.
- [ ] Run controller and CLI tests to green.

### Task 5: Migration, integrated verification, and dogfood pin

**Files:**
- Modify: `docs/migrations/specialist-session-runtime.md`
- Modify: `.github/workflows/ai-pr-review.yaml`
- Test: `tests/test_specialist_migration_docs.py`
- Test: `tests/test_ai_pr_review_workflow.py`

**Interfaces:**
- Documents: candidate lifecycle, assignment-local context, checkpoint aliases, and summary behavior.
- Pins: the dogfood workflow to the verified runtime commit.

- [ ] Update migration guidance and its focused assertions.
- [ ] Run all affected Python suites and `git diff --check`.
- [ ] Commit the runtime and documentation changes.
- [ ] Pin the dogfood workflow to the runtime commit and verify its workflow test.
