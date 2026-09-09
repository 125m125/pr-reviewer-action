# Authoritative Review Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace model-owned assignment validity with a deterministic base plan, strengthen consequence validation, restore one sticky handoff, and produce a concise behavioral human-review summary.

**Architecture:** The controller creates complete valid ownership before calling a planner. The planner proposes optional bounded transformations that are reconciled independently; scheduling remains controller-owned. Candidate consequences, sticky identity, and handoff summaries are validated at deterministic boundaries.

**Tech Stack:** Python 3, pytest, shell publishing helpers, GitHub CLI; no new dependencies.

## Global Constraints

- Mandatory obligation ownership is controller-authoritative.
- Model output cannot exclude obligations, change risk, weaken recipe isolation, invent paths, or determine runtime capacity.
- Invalid model transformations do not discard or degrade the deterministic base plan.
- Finding evidence must support the claimed consequence, not merely the changed line.
- The sticky comment is located by its reserved managed marker.
- Handoff summaries remain concise; details live in review threads.

---

### Task 1: Deterministic base plan and controller-owned scheduling

**Files:**
- Modify: `pr_reviewer/specialist_runtime/assignments.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Test: `tests/test_specialist_runtime_assignments.py`
- Test: `tests/test_specialist_runtime_controller.py`

- [ ] Add failing tests proving all assignable mandatory/topology obligations receive base ownership without model estimates.
- [ ] Add a regression matching the dogfood capacity shape so canary topology obligations are assigned rather than removed by estimated-turn arithmetic.
- [ ] Implement the deterministic base plan and controller-derived scheduling weight.
- [ ] Remove model `estimated_turns` as a validity/capacity authority while preserving actual session/deadline budgets.
- [ ] Run assignment, controller, scheduler, and negotiation suites.

### Task 2: Bounded planner transformations

**Files:**
- Modify: `pr_reviewer/specialist_runtime/assignments.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Test: `tests/test_specialist_runtime_assignments.py`
- Test: `tests/test_specialist_runtime_controller.py`
- Test: `tests/test_specialist_runtime_cli.py`

- [ ] Add failing tests for partial reorder/merge/split proposals, invalid path/risk/isolation changes, and omitted obligations retaining base ownership.
- [ ] Define a transformation schema over existing assignment and obligation IDs.
- [ ] Apply each valid transformation independently; record ignored proposals diagnostically without degrading the review.
- [ ] Replace planner repair/fallback behavior with deterministic-base-plus-optional-improvements.
- [ ] Run focused planner/controller/replay tests.

### Task 3: Consequence-aware candidate authorization

**Files:**
- Modify: `pr_reviewer/specialist_runtime/session.py`
- Modify: `pr_reviewer/specialist_runtime/adjudication.py`
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Test: `tests/test_specialist_runtime_session.py`
- Test: `tests/test_specialist_runtime_adjudication.py`
- Test: `tests/test_specialist_runtime_adjudication_adversarial.py`

- [ ] Add failing tests where evidence confirms only a changed line while the claimed consequence remains hypothetical.
- [ ] Add controls for concrete failing inputs, violated invariants, affected consumers, and contradictory evidence.
- [ ] Tighten specialist/critic prompts to require the execution path and consequence-supporting evidence.
- [ ] Enforce consequence support before accepting a finding; preserve genuine unknown/verification handling.
- [ ] Run session and adjudication suites.

### Task 4: Marker-owned sticky comment

**Files:**
- Modify: `pr_reviewer/github_review_notes.py`
- Modify: `scripts/publish_specialist_review.py`
- Test: `tests/test_github_review_notes.py`
- Test: `tests/test_publish_specialist_review.py`

- [ ] Add failing tests with an older managed handoff plus unrelated newer comments and with multiple legacy managed handoffs.
- [ ] Find the newest exact handoff-marker comment and update its ID.
- [ ] Create only when no managed handoff exists.
- [ ] Preserve author/repository safety checks and review-thread behavior.
- [ ] Run publishing tests.

### Task 5: Behavioral human-review handoff

**Files:**
- Modify: `pr_reviewer/specialist_runtime/adjudication.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/github_review_notes.py`
- Modify: `scripts/publish_specialist_review.py`
- Test: `tests/test_specialist_runtime_adjudication.py`
- Test: `tests/test_specialist_runtime_controller.py`
- Test: `tests/test_github_review_notes.py`

- [ ] Add failing tests for concise “What changed,” “What the AI reviewed,” and maximum-three “Human focus” sections.
- [ ] Validate model-proposed summary items against changed paths/evidence.
- [ ] Derive deterministic behavioral fallback summaries from topology roles/signals.
- [ ] Remove the generic component inventory from normal handoffs.
- [ ] Keep detailed evidence and individual findings exclusively in review notes.
- [ ] Run handoff/controller/publishing tests.

### Task 6: Integrated verification and dogfood pin

**Files:**
- Modify: `.github/workflows/ai-pr-review.yaml`

- [ ] Run all specialist-runtime, publishing, and workflow tests plus `git diff --check`.
- [ ] Perform one whole-branch correctness and fail-safe review.
- [ ] Commit the complete implementation.
- [ ] Pin the dogfood workflow to the implementation commit in a separate commit.
