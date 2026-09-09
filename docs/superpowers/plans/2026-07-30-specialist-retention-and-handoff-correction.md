# Specialist Retention and Handoff Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent candidate loss at checkpoint boundaries, give deterministic specialists actionable diff-oriented briefs, and publish an honest behavioral handoff for degraded reviews.

**Architecture:** Deterministic coverage remains authoritative. Session admission reserves a structured checkpoint/repair allowance and checkpoint prompts carry their schema contract in-band. Assignment serialization enriches immutable ownership with controller-derived obligation and change context, while handoff projection derives concise component/behavior summaries from changed hunks and retained review evidence.

**Tech Stack:** Python 3, pytest, existing specialist runtime and GitHub Action shell/Python integration.

## Global Constraints

- Preserve deterministic obligation ownership and fail-closed coverage decisions.
- Do not allow model output to create, delete, or reassign authoritative obligations.
- Keep all repository and model-controlled inputs untrusted.
- Do not turn unsupported concerns into findings.
- Prefer explicit degraded/unknown output over a misleading clean zero-findings result.
- Avoid new runtime dependencies.

---

### Task 1: Reliable structured checkpoint retention

**Files:**
- Modify: `pr_reviewer/specialist_runtime/session.py`
- Modify: `pr_reviewer/specialist_runtime/model_gateway.py` only if request rendering requires it
- Test: `tests/test_specialist_runtime_session.py`

**Interfaces:**
- Consumes: `SpecialistSession` lifetime budgets, `_CHECKPOINT_SCHEMA`, retained evidence and candidate state.
- Produces: an admitted structured checkpoint attempt with in-band schema instructions, bounded repair, and explicit candidate-loss diagnostics.

- [ ] **Step 1: Write failing tests**

Add focused tests proving:

```python
def test_exploration_reserves_checkpoint_and_repair_turns():
    """Exploration cannot consume the turns reserved for structured retention."""

def test_checkpoint_request_includes_compact_schema_contract():
    """The checkpoint user message describes required keys and candidate retention."""

def test_malformed_checkpoint_is_repaired_before_projection():
    """One malformed structured checkpoint receives one bounded repair request."""

def test_unrecoverable_candidate_text_is_reported_as_retention_unknown():
    """Fallback state cannot look like a trustworthy zero-findings checkpoint."""
```

- [ ] **Step 2: Verify the tests fail for the missing behavior**

Run:

```bash
pytest tests/test_specialist_runtime_session.py -q
```

Expected: the new assertions fail because exploration can consume the full turn budget, the schema is not in the user message, or lost candidate state is not diagnosed.

- [ ] **Step 3: Implement the minimum retention changes**

Reserve two model turns for checkpoint plus repair while exploring; finalization retains its existing bounded allowance. Add a compact JSON example/schema contract to checkpoint and repair messages. When structured checkpoint repair cannot recover material candidate-shaped output, add a bounded unknown/diagnostic rather than silently projecting an empty clean checkpoint.

- [ ] **Step 4: Verify focused tests**

Run:

```bash
pytest tests/test_specialist_runtime_session.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pr_reviewer/specialist_runtime/session.py pr_reviewer/specialist_runtime/model_gateway.py tests/test_specialist_runtime_session.py
git commit -m "fix(review): preserve specialist checkpoint findings"
```

### Task 2: Semantic deterministic assignments and diff-first exploration

**Files:**
- Modify: `pr_reviewer/specialist_runtime/assignments.py`
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Modify: `pr_reviewer/specialist_runtime/types.py` if a typed brief field is required
- Modify: `pr_reviewer/specialists.py`
- Test: `tests/test_specialist_runtime_assignments.py`
- Test: `tests/test_specialist_runtime_cli.py`
- Test: `tests/test_specialists.py`

**Interfaces:**
- Consumes: authoritative obligations, deterministic assignment ownership, topology, changed-file patches and repository recipes.
- Produces: immutable specialist assignment JSON containing human-readable obligation briefs and scoped changed-path/change-symbol context.

- [ ] **Step 1: Write failing tests**

Add tests proving:

```python
def test_assignment_brief_explains_each_owned_obligation():
    """Every assigned ID has subject, explanation, risk, evidence and predicate context."""

def test_assignment_brief_contains_scoped_changed_behavior():
    """The brief includes relevant changed paths and hunk/symbol summaries."""

def test_assignment_prompt_requires_diff_first_investigation():
    """Specialists are told to inspect assigned diffs before whole files."""

def test_deterministic_group_titles_describe_behavior_not_capacity_bucket():
    """Fallback combined assignments receive stable semantic titles/objectives."""
```

- [ ] **Step 2: Verify the tests fail**

Run:

```bash
pytest tests/test_specialist_runtime_assignments.py tests/test_specialist_runtime_cli.py tests/test_specialists.py -q
```

Expected: FAIL because current assignments expose opaque IDs and `combined:N` objectives without change summaries.

- [ ] **Step 3: Implement semantic briefs**

Serialize controller-authoritative obligation details without granting model authority. Derive stable semantic titles/objectives from recipe, component, interaction, and test subjects. Attach only relevant changed paths and bounded hunk/symbol summaries. Add an explicit diff-first exploration sequence and clarify tool-result completeness/truncation semantics.

- [ ] **Step 4: Verify focused tests**

Run:

```bash
pytest tests/test_specialist_runtime_assignments.py tests/test_specialist_runtime_cli.py tests/test_specialists.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add pr_reviewer/specialist_runtime/assignments.py pr_reviewer/specialist_runtime/cli.py pr_reviewer/specialist_runtime/types.py pr_reviewer/specialists.py tests/test_specialist_runtime_assignments.py tests/test_specialist_runtime_cli.py tests/test_specialists.py
git commit -m "fix(review): give specialists semantic diff briefs"
```

### Task 3: Honest behavior-oriented handoff and degraded candidate-loss notes

**Files:**
- Modify: `pr_reviewer/specialists.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/specialist_runtime/adjudication.py`
- Modify: `pr_reviewer/github_review_notes.py` if note projection needs a new diagnostic kind
- Test: `tests/test_specialists.py`
- Test: `tests/test_specialist_runtime_controller.py`
- Test: `tests/test_specialist_runtime_adjudication.py`
- Test: `tests/test_github_review_notes.py`

**Interfaces:**
- Consumes: changed hunks, topology/component roles, reviewed evidence paths, coverage state and session retention diagnostics.
- Produces: at most five concise behavioral/component summaries, an accurate reviewed summary, and a resolvable verification note or explicit coverage warning for material candidate loss.

- [ ] **Step 1: Write failing tests**

Add tests proving:

```python
def test_modified_python_function_is_a_changed_contract_fact():
    """A changed body is attributed using its diff hunk/function context."""

def test_handoff_prioritizes_material_code_over_plan_documents():
    """Large runtime changes cannot be displaced by the first five docs/workflow paths."""

def test_ai_reviewed_uses_retained_code_evidence_paths():
    """Reviewed Python components appear when evidence proves they were inspected."""

def test_candidate_retention_failure_is_not_reported_as_clean_zero():
    """Material discarded candidates produce an honest degraded warning/note."""
```

- [ ] **Step 2: Verify the tests fail**

Run:

```bash
pytest tests/test_specialists.py tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_adjudication.py tests/test_github_review_notes.py -q
```

Expected: FAIL because modified function bodies are not summarized and candidate-loss diagnostics do not reach the human handoff.

- [ ] **Step 3: Implement behavior-oriented projection**

Parse bounded diff hunk headers and surrounding declaration context for modified Python functions. Rank security/runtime/orchestration/publishing behavior above tests and documentation while still representing material docs when space remains. Build “What the AI reviewed” from retained evidence paths and covered obligations. Surface material candidate-retention loss as a concise coverage warning and, when a defensible changed path exists, a resolvable verification note without promoting it to a defect.

- [ ] **Step 4: Verify focused tests**

Run:

```bash
pytest tests/test_specialists.py tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_adjudication.py tests/test_github_review_notes.py -q
```

Expected: PASS.

- [ ] **Step 5: Run the integrated suite**

Run:

```bash
pytest tests/ -q
```

Expected: PASS with no failures.

- [ ] **Step 6: Commit**

```bash
git add pr_reviewer/specialists.py pr_reviewer/specialist_runtime/controller.py pr_reviewer/specialist_runtime/adjudication.py pr_reviewer/github_review_notes.py tests/test_specialists.py tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_adjudication.py tests/test_github_review_notes.py
git commit -m "fix(review): publish honest behavioral handoffs"
```
