# Model Summaries and Role Continuity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every review role a validated whole-PR overview, preserve reasoning across model continuations, consolidate duplicate candidates, and make specialist follow-up budgets reflect actual tool capacity.

**Architecture:** Immutable local git diff facts remain authoritative. One bounded change-summary model role turns those facts into reusable prose under deterministic validation; a later handoff-summary role combines that overview with verified coverage facts. Structured roles share one continuation/repair mechanism and neutral conversation history retains reasoning separately from normal content.

**Tech Stack:** Python 3, existing OpenAI-compatible chat-completions transport, pytest, GitHub Actions composite workflow.

## Global Constraints

- Local immutable base/head git state is authoritative; GitHub patch text is optional enrichment.
- Model summaries may present controller facts but cannot create changed paths, obligations, findings, verdicts, or coverage.
- Reasoning stays internal, bounded, compactable, and is never published.
- Candidate deduplication must be conservative and retain the best defensible location plus all valid evidence.
- Run/session lifetime limits remain hard controller-owned bounds.
- No new runtime dependencies.

---

### Task 1: Immutable local-diff facts and reusable change overview

**Files:**
- Modify: `pr_reviewer/specialists.py`
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/specialist_runtime/types.py` if a typed overview is needed
- Test: `tests/test_specialists.py`
- Test: `tests/test_specialist_runtime_cli.py`
- Test: `tests/test_specialist_runtime_controller.py`

**Interfaces:**
- Produces: bounded `change_facts` from `base_sha...head_sha` and validated `change_overview` with `overview`, `key_changes`, `cross_component_effects`, and `uncertainties`.
- Consumers: planner, specialist assignment prompt, negotiator, critic, and finalizer contexts.

- [ ] Add failing tests proving local git diff facts remain populated when every GitHub `patch` field is absent, including Python hunk symbols, workflow keys, and Markdown/AsciiDoc headings.
- [ ] Add failing tests proving the change summarizer rejects unchanged paths, unknown components, verdicts, findings, and coverage claims, while malformed output falls back to bounded deterministic facts.
- [ ] Add a failing integration test proving the same validated overview reaches planner, specialist, negotiator, critic, and finalizer contexts.
- [ ] Run the focused tests and capture the expected failures.
- [ ] Implement one bounded structured `change_summarizer` role before planning, backed by immutable local-diff facts and deterministic validation.
- [ ] Run focused tests, commit as `feat(review): summarize immutable PR changes`, and write Task 1 RED/GREEN evidence to the SDD report.

### Task 2: Reasoning-preserving structured-role continuation

**Files:**
- Modify: `pr_reviewer/conversation.py`
- Modify: `pr_reviewer/tool_loop.py`
- Modify: `pr_reviewer/specialist_runtime/model_gateway.py`
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Test: `tests/test_conversation.py`
- Test: `tests/test_native_tool_loop.py`
- Test: `tests/test_specialist_runtime_cli.py`
- Test: `tests/test_specialist_runtime_controller.py`

**Interfaces:**
- Produces: neutral assistant reasoning/content/tool-call events and one shared bounded structured-role continuation/forced-JSON repair path for planner, negotiator, critic, finalizer, change summarizer, and handoff summarizer.

- [ ] Add failing tests proving a response containing both `reasoning_content` and normal `content` preserves both in the next request, including a tool-bearing turn.
- [ ] Add a failing critic regression matching run 30543173785: first response is reasoning-only with `finish_reason=length`, second request retains that reasoning and forces reasoning off/JSON.
- [ ] Add failing tests proving repair starts from retained reasoning/content rather than rebuilding a two-message conversation.
- [ ] Run focused tests and capture RED.
- [ ] Implement bounded compactable `assistant_reasoning` state with endpoint-compatible rendering; never expose it to verdict or publishing parsers.
- [ ] Route every structured role through the shared continuation/repair mechanism.
- [ ] Run focused tests, commit as `fix(review): preserve reasoning across role continuations`, and write Task 2 report evidence.

### Task 3: Conservative candidate consolidation and critic fallback

**Files:**
- Modify: `pr_reviewer/specialist_runtime/adjudication.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/specialist_runtime/types.py` if root-cause identity is typed
- Test: `tests/test_specialist_runtime_adjudication.py`
- Test: `tests/test_specialist_runtime_adjudication_adversarial.py`
- Test: `tests/test_specialist_runtime_controller.py`

**Interfaces:**
- Produces: one deterministic candidate per stable root-cause identity before criticism, critic-authorized merge decisions afterward, and at most one fallback verification request per identity.

- [ ] Add failing tests for repeated budget-validation, workflow-trigger, rationale-format, and location-normalization candidates from separate specialists.
- [ ] Prove unrelated concerns in the same file do not merge.
- [ ] Prove merge retains the most precise changed location, valid evidence union, related obligations, and strongest supported severity.
- [ ] Add a critic-degradation regression proving fallback emits one verification request per consolidated identity.
- [ ] Run focused tests and capture RED.
- [ ] Implement conservative controller-derived identity from normalized changed path, changed symbol/contract, and root-cause category; model-provided IDs alone are not authority.
- [ ] Run focused tests, commit as `fix(review): consolidate duplicate candidate roots`, and write Task 3 report evidence.

### Task 4: Tool-aware follow-up budgeting

**Files:**
- Modify: `pr_reviewer/specialist_runtime/negotiation.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/specialist_runtime/types.py`
- Modify: `scripts/sections/config.sh`
- Modify: `action.yml`
- Modify: `.github/workflows/ai-pr-review.yaml`
- Test: `tests/test_specialist_runtime_state.py`
- Test: `tests/test_specialist_runtime_controller.py`
- Test: `tests/test_action_inputs.py`
- Test: `tests/test_ai_pr_review_workflow.py`

**Interfaces:**
- Produces: negotiation resources containing remaining model turns and tool calls; resume/consult feasibility requires both; default dogfood capacity is 64 model turns and 128 tool calls.

- [ ] Add failing tests proving a tool-exhausted session cannot be resumed even when model turns remain, while a bounded new session or `record_unknown` remains feasible.
- [ ] Add a failing test proving multi-call tool turns can consume more tools than model turns without premature exhaustion under the new default.
- [ ] Add workflow/input tests for the 128-tool dogfood/default recommendation.
- [ ] Run focused tests and capture RED.
- [ ] Implement tool-aware negotiation without silent recharge; new sessions receive fresh controller-accounted budgets and hard run totals remain unchanged.
- [ ] Run focused tests, commit as `fix(review): budget follow-up tools explicitly`, and write Task 4 report evidence.

### Task 5: Model-written human handoff

**Files:**
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Modify: `pr_reviewer/specialist_runtime/adjudication.py`
- Modify: `pr_reviewer/github_review_notes.py` if handoff serialization changes
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Test: `tests/test_specialist_runtime_controller.py`
- Test: `tests/test_specialist_runtime_adjudication.py`
- Test: `tests/test_github_review_notes.py`

**Interfaces:**
- Consumes: validated change overview, successful evidence paths, covered obligations, specialist scopes, note themes, and degradation state.
- Produces: concise `what_changed_summary`, `ai_reviewed_summary`, and `human_focus` prose; deterministic fragments remain degraded fallback only.

- [ ] Add failing tests proving normal handoff is two or three behavioral sentences rather than a per-file list.
- [ ] Add failing tests rejecting invented paths/components, detailed finding claims, verdict changes, and unsupported coverage statements.
- [ ] Add a failing degradation test proving summarizer failure falls back to concise deterministic facts and preserves material coverage warnings.
- [ ] Run focused tests and capture RED.
- [ ] Implement bounded structured `handoff_summarizer` after adjudication; reuse the validated initial overview for “What changed” and synthesize “What the AI reviewed” only from successful coverage facts.
- [ ] Run focused and integrated suites, commit as `feat(review): write validated human handoffs`, update the dogfood workflow pin to the verified implementation commit, and write Task 5 report evidence.
