# Review Pipeline Dogfood Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Fix duplicate sticky publication, note anchoring, and sparse handoff quality, then dogfood them with two inert review canaries.

**Architecture:** Keep GitHub publication authority deterministic, strengthen the model location contract without relaxing validation, derive fallback handoff orientation from structured state, and isolate deliberate defects under evaluation fixtures.

**Tech Stack:** Python 3, pytest, GitHub GraphQL/REST publication helpers.

## Global Constraints

- Never infer a line or file that is not supported by changed-file state.
- Never duplicate detailed claims or evidence in the sticky handoff.
- Canary code must not be imported or executed by production action paths.
- Preserve existing managed-thread and deterministic fallback safety.

---

### Task 1: Sticky publication identity

Modify `pr_reviewer/github_review_notes.py` and
`tests/test_github_review_notes.py`. Write a failing test reproducing a newly
created sticky that is not yet returned by list queries. Patch the known comment
ID when refreshing with the review URL, verify RED/GREEN, and commit.

### Task 2: Candidate locations and useful handoff

Modify specialist prompts/adjudication plus focused tests. Require exact
`path`/`path:line` candidate locations, preserve honest file anchors, and
derive a concise behavior-oriented change map, actual coverage,
truthful prepared-note status, and at most three human emphasis areas for
deterministic/degraded handoffs without inferring GitHub thread resolution.
Verify that detailed findings remain absent and commit.

### Task 3: Inert dogfood findings

Add an evaluation-only Python fixture containing exactly two realistic review
defects. Commit no answer key, category description, target line, or exact
expected finding. Add only a generic test proving the fixture is outside
production imports and review-corpus configuration. Keep the manual oracle in
ignored local evaluation records. Commit.

### Task 4: Integration verification

Run publisher, specialist-runtime, and workflow tests. Review the complete
stacked diff for realistic regressions and verify tracked working-tree
cleanliness. Do not push.
