# Obligation Closure and Evidence Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make specialist coverage applicable, interactive, semantically validated, and bounded so absent evidence cannot cause repeated reads or a finding-free change request.

**Architecture:** Derive conditional evidence requirements from immutable PR state, then expose a controller-owned obligation-assessment ledger through cheap local session tools. Repository reads remain neutral evidence; checkpoints retain working memory, while negotiation admits only concrete novel actions.

**Tech Stack:** Python 3 dataclasses/enums, JSON policy parsing, native tool conversations, pytest.

## Global Constraints

- Legacy `expected_evidence` remains valid and unconditional.
- Models propose; only the controller mutates authoritative coverage state.
- Local obligation tools do not consume repository/web tool-call budget.
- Existing repository, command, web, revision, and boundary guards remain unchanged.
- Coverage limitations alone create neither findings nor request-changes verdicts.
- Every behavior change follows a demonstrated RED/GREEN test cycle.

---

### Task 1: Conditional Evidence Requirements

**Files:**
- Modify: `pr_reviewer/specialist_runtime/policy.py`
- Modify: `pr_reviewer/specialist_runtime/types.py`
- Modify: `pr_reviewer/specialist_runtime/coverage.py`
- Test: `tests/test_specialist_runtime_policy.py`
- Test: `tests/test_specialist_runtime_coverage.py`

**Interfaces:**
- Produces `EvidenceRequirementPolicy` and `RecipePolicy.evidence_requirements`.
- Creates obligations only for requirements whose `when` matches immutable topology/risk inputs.

- [ ] **Step 1: Write failing parser tests**

Test this accepted form plus unknown keys, invalid modes, unsafe paths, and duplicate IDs:

```python
"evidence_requirements": [{
    "id": "build-manifest",
    "category": "build manifest",
    "when": {"paths_any": ["**/pom.xml"]},
    "seed_paths": ["pom.xml", "**/pom.xml"],
    "mode": "required",
}]
```

- [ ] **Step 2: Verify RED**

Run `pytest tests/test_specialist_runtime_policy.py -q`; expect rejection of the new field.

- [ ] **Step 3: Implement policy values and parsing**

```python
@dataclass(frozen=True)
class EvidenceRequirementPolicy:
    id: str
    category: str
    when: Mapping[str, tuple[str, ...]]
    seed_paths: tuple[str, ...] = ()
    related_paths: tuple[str, ...] = ()
    mode: str = "required"
```

Reuse existing match/path normalization. Permit `required`, `optional`, and
`one_of:<slug>`. Include requirements in serialization and base/head safe-policy merging.

- [ ] **Step 4: Write failing derivation tests**

Prove a forced workflow recipe does not create its build-manifest obligation
unless a manifest changed; matching workflow requirements do activate; forcing
does not bypass `when`; legacy entries stay unconditional; optional requirements
are non-mandatory; one covered member satisfies a `one_of` group.

- [ ] **Step 5: Verify RED**

Run `pytest tests/test_specialist_runtime_coverage.py -q`; expect flat legacy derivation to fail the new cases.

- [ ] **Step 6: Implement conditional derivation**

Evaluate requirement matches using `_rule_matches`. Add stable requirement identity
and mode to `CoverageObligation`. Account for unmatched requirements without
assigning them. Preserve legacy obligation IDs.

- [ ] **Step 7: Verify GREEN**

Run `pytest tests/test_specialist_runtime_policy.py tests/test_specialist_runtime_coverage.py -q`.

### Task 2: Controller-Owned Assessment Ledger

**Files:**
- Create: `pr_reviewer/specialist_runtime/obligation_assessment.py`
- Modify: `pr_reviewer/specialist_runtime/types.py`
- Modify: `pr_reviewer/specialist_runtime/coverage.py`
- Test: `tests/test_specialist_runtime_obligation_assessment.py`
- Test: `tests/test_specialist_runtime_negotiation.py`

**Interfaces:**
- Produces `ObligationDisposition`, `ObligationAssessment`, `ObligationAttempt`, and `ObligationAssessmentLedger`.
- Consumes obligations, session ownership, and retained evidence snapshots.

- [ ] **Step 1: Write failing lifecycle tests**

Use the desired API:

```python
result = ledger.propose(
    session_id="session-1", target="O2", disposition="not_applicable",
    reason="No build manifest or build command changed.",
    evidence_ids=(changed_files.id, workflow_diff.id), next_actions=(),
)
assert result.accepted
assert ledger.assessment("O2").disposition.value == "not_applicable"
```

Reject unknown/unowned handles, unknown/ineligible evidence, covered without
evidence/conclusion, unresolved without a novel next action, and not-applicable
without changed-state evidence.

- [ ] **Step 2: Verify RED**

Run `pytest tests/test_specialist_runtime_obligation_assessment.py -q`; expect missing module/API.

- [ ] **Step 3: Implement lifecycle and attempt records**

Add dispositions `pending`, `covered`, `not_applicable`, `exhausted`, `blocked`,
and `unresolved`. Generate stable assignment-local `O1` handles. Store bounded,
masked conclusions, evidence deltas, next actions, validation outcomes, and
normalized action fingerprints. Return detached snapshots.

- [ ] **Step 4: Write failing reconciliation tests**

Prove a neutral read stays pending; an accepted covered proposal covers; closure
dispositions leave negotiation while remaining auditable; independent coverage
still needs a fresh independent collection; model categories remain forbidden.

- [ ] **Step 5: Verify RED**

Run `pytest tests/test_specialist_runtime_negotiation.py -q`; expect current automatic association behavior to fail.

- [ ] **Step 6: Integrate assessments into reconciliation**

Reconcile accepted assessment snapshots against evidence eligibility. Do not infer
semantic coverage from checkpoint evidence lists. Preserve distinct closure states
in artifacts instead of treating them as covered.

- [ ] **Step 7: Verify GREEN**

Run `pytest tests/test_specialist_runtime_obligation_assessment.py tests/test_specialist_runtime_negotiation.py -q`.

### Task 3: Interactive Tools and Checkpoint Fallback

**Files:**
- Modify: `pr_reviewer/specialist_runtime/session.py`
- Modify: `pr_reviewer/conversation.py`
- Modify: `pr_reviewer/specialist_runtime/types.py`
- Test: `tests/test_specialist_runtime_session.py`

**Interfaces:**
- Adds `explain_obligation`, `get_obligation_status`, and `propose_obligation_resolution`.
- Adds optional short `targets` to eligible repository-read schemas.
- Uses the Task 2 assessment ledger without changing external executor authority.

- [ ] **Step 1: Write failing local-tool tests**

Assert local tools use short handles, return bounded structured results, do not
invoke the external executor, and do not consume repository tool calls. Reject
handles owned by another assignment.

- [ ] **Step 2: Verify RED**

Run `pytest tests/test_specialist_runtime_session.py -q -k "obligation_tool or target_handle or local_tool_budget"`.

- [ ] **Step 3: Implement local dispatch and neutral targeted reads**

Dispatch local tools before budget reservation. Replace model-facing
`obligation_ids` with short `targets`. Return read metadata like:

```json
{"evidence_id":"evidence:...","changed":true,
 "eligible_targets":["O2"],"coverage_effect":"neutral_evidence_retained"}
```

Do not associate an untargeted read with every current gap.

- [ ] **Step 4: Write failing checkpoint persistence tests**

Add a bounded `obligation_updates` fallback and prove it uses the same validator.
Prove accepted interactive assessments, handles, and attempt history survive
compact-resume and emergency reconstruction without being repeated in checkpoints.

- [ ] **Step 5: Verify RED**

Run `pytest tests/test_specialist_runtime_session.py -q -k "obligation_update or assessment_persists or compact_resume"`.

- [ ] **Step 6: Implement fallback and prompt contract**

Validate checkpoint updates through the assessment ledger. Keep coverage maps,
evidence metadata, and attempt history controller-owned. Tell specialists that
coverage is not evidence-seeking at all costs and that closure outcomes are valid.

- [ ] **Step 7: Verify GREEN**

Run `pytest tests/test_specialist_runtime_session.py -q`.

### Task 4: Novel-Action Negotiation and Bounded Closure

**Files:**
- Modify: `pr_reviewer/specialist_runtime/negotiation.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/specialist_runtime/types.py`
- Test: `tests/test_specialist_runtime_negotiation.py`
- Test: `tests/test_specialist_runtime_controller.py`

**Interfaces:**
- Consumes Task 2 assessment/attempt snapshots.
- Offers resume/consult only for controller-approved concrete novel actions.

- [ ] **Step 1: Write failing negotiation projection tests**

Assert unresolved targets expose bounded last conclusion, attempt count, evidence
delta, and next actions. Resume requires a novel next action. Closed targets are
absent and internal obligation IDs remain hidden.

- [ ] **Step 2: Verify RED**

Run `pytest tests/test_specialist_runtime_negotiation.py -q -k "novel or exhausted or compact_negotiation"`.

- [ ] **Step 3: Implement novelty-aware validation/fallback**

Carry a controller-normalized action fingerprint. Reject attempted actions.
Deterministic fallback selects a feasible novel action; with none, it records the
appropriate closure rather than resuming because budget remains.

- [ ] **Step 4: Write failing controller follow-up tests**

Cover one no-evidence follow-up becoming exhausted, repeated actions rejected, new
evidence sources permitting a distinct attempt, normal risk allowing one no-progress
follow-up, high risk allowing at most one additional distinct attempt, and concise
reasoned journal events.

- [ ] **Step 5: Verify RED**

Run `pytest tests/test_specialist_runtime_controller.py -q -k "novel_action or no_progress or obligation_closure"`.

- [ ] **Step 6: Implement bounded follow-up behavior**

Pass assessment state into negotiation. Compare evidence/action deltas after each
follow-up. Apply closure without creating another session. Preserve closure across waves.

- [ ] **Step 7: Verify GREEN**

Run `pytest tests/test_specialist_runtime_negotiation.py tests/test_specialist_runtime_controller.py -q`.

### Task 5: Verdict, Handoff, Migration, and End-to-End Verification

**Files:**
- Modify: `pr_reviewer/specialist_runtime/adjudication.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `docs/migrations/specialist-session-runtime.md`
- Test: `tests/test_specialist_runtime_adjudication.py`
- Test: `tests/test_specialist_runtime_controller.py`

**Interfaces:**
- Produces one concise handoff limitation for material blocked/exhausted high-risk areas.
- Guarantees coverage-only limitations do not create notes or request changes.

- [ ] **Step 1: Write failing verdict/handoff tests**

Prove no findings plus incomplete high-risk coverage yields a notice, no detailed
verification threads, and at most one human-focus limitation. Not-applicable stays
auditable but hidden from normal handoff; concrete major findings still request changes.

- [ ] **Step 2: Verify RED**

Run `pytest tests/test_specialist_runtime_adjudication.py tests/test_specialist_runtime_controller.py -q -k "coverage_only or not_applicable or exhausted or blocked"`.

- [ ] **Step 3: Implement verdict/handoff projection**

Separate finding policy from coverage completeness. Keep coverage in artifacts and
aggregate material limitations into one sparse handoff fact, never a detail note.

- [ ] **Step 4: Update migration documentation**

Document conditional requirements, modes, closure outcomes, obligation tools,
checkpoint fallback, and migration away from broad unconditional evidence lists.

- [ ] **Step 5: Run specialist-runtime verification**

Run:

```powershell
pytest tests/test_specialist_runtime_policy.py tests/test_specialist_runtime_coverage.py tests/test_specialist_runtime_obligation_assessment.py tests/test_specialist_runtime_session.py tests/test_specialist_runtime_negotiation.py tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_adjudication.py -q
```

- [ ] **Step 6: Run full verification**

Run:

```powershell
pytest tests -q
git diff --check
```

Separate known Windows-only environment failures from regressions with exact test names.

- [ ] **Step 7: Review and commit**

Inspect only the scoped diff, stage no unrelated untracked files, commit, and do not push.
