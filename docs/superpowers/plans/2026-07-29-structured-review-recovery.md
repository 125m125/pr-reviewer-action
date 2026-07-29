# Structured Review Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make specialist planning, negotiation, finalization, fallback assignment, and adjudication produce useful, anchored review findings under local-model output failures.

**Architecture:** Keep the controller authoritative and fail-safe. Structured model roles receive bounded continuation and a reasoning-disabled final response; parsing extracts exactly one JSON object without accepting ambiguous multiple-object output. Redundant planner evidence is derived from immutable obligations, deterministic fallback owns every assignable obligation, and platform-invariant false positives are rejected before publication.

**Tech Stack:** Python 3, pytest, existing specialist runtime and model gateway; no new dependencies.

## Global Constraints

- Preserve generic OpenAI-compatible and Anthropic-compatible endpoint support.
- Do not add `instructor` or another SDK-specific structured-output dependency.
- Mandatory obligations remain mandatory; no repair may silently drop or resolve them.
- Model-selected paths must remain within immutable obligation scope.
- Structured-role recovery is bounded and cannot exceed its explicit provider-call budget.
- Tolerant parsing accepts one unambiguous JSON object only.
- Verification requests remain available for genuine unknowns; only deterministic platform contradictions are rejected.

---

### Task 1: Structured role recovery

**Files:**
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Test: `tests/test_specialist_runtime_controller.py`
- Test: `tests/test_specialist_runtime_cli.py`

**Interfaces:**
- Consumes: `GatewayRoleAdapter.complete(RoleRequest)` and `ModelTurnRequest`.
- Produces: bounded negotiator/finalizer completion and `_json_object(text)` that returns one mapping or raises.

- [ ] Add failing tests showing a negotiator length response receives a reasoning-disabled continuation, and a finalizer fenced JSON object followed by prose is parsed.
- [ ] Run the focused tests and confirm the current one-shot/strict parser behavior fails them.
- [ ] Implement a shared bounded structured-role loop for negotiator/finalizer, preserving the planner’s separate four-call budget.
- [ ] Implement balanced single-object extraction; reject zero or multiple top-level objects.
- [ ] Run focused controller/CLI/replay tests.

### Task 2: Planner normalization and complete deterministic ownership

**Files:**
- Modify: `pr_reviewer/specialist_runtime/assignments.py`
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Test: `tests/test_specialist_runtime_assignments.py`
- Test: `tests/test_specialist_runtime_controller.py`

**Interfaces:**
- Consumes: immutable `CoverageObligation.required_evidence_categories`, scope, and seed hints.
- Produces: validated assignments whose `expected_evidence` is derived from assigned obligations, plus a fallback plan owning every assignable mandatory/topology obligation within capacity.

- [ ] Add failing tests for paraphrased/missing planner evidence and for fallback ownership of canary topology obligations.
- [ ] Confirm failures before production edits.
- [ ] Normalize `expected_evidence` from obligation IDs while retaining strict obligation and path validation.
- [ ] Partition fallback assignments so every assignable obligation has an owner; preserve dedicated/independent recipe constraints and configured capacity.
- [ ] Clarify the planner prompt that obligation IDs and immutable path sets are authoritative.
- [ ] Run assignment/controller/scheduler tests.

### Task 3: Platform-semantic adjudication guardrails

**Files:**
- Modify: `pr_reviewer/specialist_runtime/adjudication.py`
- Test: `tests/test_specialist_runtime_adjudication.py`
- Test: `tests/test_specialist_runtime_adjudication_adversarial.py`

**Interfaces:**
- Consumes: critic dispositions and evidence-backed candidate claims.
- Produces: notes only for actionable findings or genuine verification unknowns.

- [ ] Add a failing test where a critic requests verification solely because GitHub might use line zero.
- [ ] Confirm the invalid verification request currently survives.
- [ ] Reject verification requests contradicted by stable GitHub diff-location semantics, without broad keyword suppression.
- [ ] Add a control test proving a genuine location ambiguity remains a verification request.
- [ ] Run adjudication suites.

### Task 4: Integrated dogfood verification

**Files:**
- Modify: `.github/workflows/ai-pr-review.yaml`

**Interfaces:**
- Consumes: the implementation commit SHA.
- Produces: a workflow pin that exercises the complete fix without circular self-reference.

- [ ] Run all specialist-runtime unit suites and `git diff --check`.
- [ ] Review the complete diff for compatibility and fail-safe behavior.
- [ ] Commit implementation files.
- [ ] Pin the workflow to the full implementation commit SHA in a separate commit.
- [ ] Report any platform-dependent full-suite failures separately from changed-suite results.
