# Delegated Tool Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a specialist ask one focused question of a large read-only tool result without placing the raw result in its conversation, and prefer Markdown when fetching documentation.

**Architecture:** `SpecialistSession` advertises and executes a controller-owned `delegate_tool_summary` wrapper around its existing evidence tools. The wrapper retains the original result, runs one tools-disabled structured summary turn (plus one focused repair), verifies quoted excerpts against the retained source, and returns only a normal-size derived result. Existing executor guards and model/session budgets remain authoritative.

**Tech Stack:** Python standard library, existing specialist runtime, pytest.

**Spec:** Approved design in the 2026-09-05 task conversation.

## Global Constraints

- Only existing read-only evidence tools may be delegated; recursion and state-changing specialist tools are forbidden.
- The original bounded source is authoritative evidence; the summary is derived evidence.
- The default summary output allowance is twice `specialist_max_tokens`; a repair receives half that allowance.
- The source limit is configurable and otherwise derived from the real model context after output, prompt, and safety reserves.
- The visible wrapper result remains bounded by the normal tool-result limit.

---

### Task 1: Prefer Markdown web responses

**Files:**
- Modify: `pr_reviewer/specialist_runtime/web_evidence.py`
- Test: `tests/test_specialist_runtime_web.py`

**Interfaces:**
- Consumes: `SecureFetcher.fetch(url, ...)`
- Produces: an HTTP `Accept` header preferring Markdown, then plain text, then HTML and structured formats.

- [ ] Write a fetch test that captures the request and expects the negotiated order.
- [ ] Run the test and confirm it fails on the old header.
- [ ] Change the header and confirm the focused web tests pass.

### Task 2: Add delegated evidence summarization

**Files:**
- Modify: `pr_reviewer/specialist_runtime/session.py`
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Modify: `pr_reviewer/conversation.py`
- Test: `tests/test_specialist_runtime_session.py`
- Test: `tests/test_conversation.py`

**Interfaces:**
- Consumes: `delegate_tool_summary` arguments `{tool_name, arguments, target, question, targets?}`.
- Produces: `{status, evidence_id, source_evidence_id, summary, relevant_excerpts, uncertainties, source_truncated}`.

- [ ] Write failing tests for advertisement, successful isolation/retention, forbidden inner tools, verified excerpts, and one repair.
- [ ] Run focused tests and confirm failures are caused by the missing feature.
- [ ] Add the minimum session-owned wrapper using the existing executor, evidence store, gateway, deadline, and budget ledgers.
- [ ] Run focused tests and refactor only after they pass.

### Task 3: Wire configurable limits and documentation

**Files:**
- Modify: `action.yml`
- Modify: `scripts/sections/config.sh`
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Modify: `README.md`
- Modify: `docs/migrations/specialist-session-runtime.md`
- Test: `tests/test_specialist_runtime_cli.py`

**Interfaces:**
- Consumes: `SPECIALIST_DELEGATED_SUMMARY_MAX_TOKENS` and `SPECIALIST_DELEGATED_SUMMARY_MAX_SOURCE_BYTES`.
- Produces: validated optional overrides; empty values derive limits from specialist/model context configuration.

- [ ] Write failing configuration tests for defaults and overrides.
- [ ] Wire action inputs and environment variables.
- [ ] Document intended use, limits, and evidence semantics.
- [ ] Run focused and broad portable test suites.
