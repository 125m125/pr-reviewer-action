# GitHub Repository Access Requests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve denied cross-repository `gh_api` lookups as human-actionable, safely bounded repository-access requests instead of losing them as ordinary tool failures.

**Architecture:** Add a repository-specific immutable request type and deterministic endpoint parser, capture only the exact repository-allowlist denial in durable specialist state, and project validated requests through the existing note/handoff/artifact pipeline. Optional model purposes are removed before executor dispatch and retained only as masked explanatory context; they never influence authorization.

**Tech Stack:** Python dataclasses, native tool JSON schemas, specialist runtime controller/adjudication, pytest.

## Global Constraints

- Creating a request never authorizes or fetches repository content.
- Current-branch configuration and human-triggered review remain the only authorization path.
- Never infer wildcard repository access.
- Preserve existing GitHub API endpoint, path, token, response, deadline, fork, and budget guards.
- Derive the authoritative purpose from endpoint/assignment/obligation state; model purpose is optional, masked, and bounded.
- Only a repository-allowlist denial creates this request. Other tool failures remain failures.
- The sticky handoff remains summary-only; detailed request evidence lives in a resolvable general note and artifact.

---

### Task 1: Typed Repository Request and Deterministic Derivation

**Files:**
- Modify: `pr_reviewer/specialist_runtime/web_evidence.py`
- Test: `tests/test_specialist_runtime_web.py`

**Interfaces:**
- Produces `RepositoryAccessRequest` with `as_dict()`.
- Produces `repository_access_request(endpoint, obligation_id, assignment_objective, model_purpose, authority_reason)`.
- Returns `None` or raises `ValueError` for endpoints that do not identify a canonical `repos/owner/repo/...` resource.

- [ ] **Step 1: Write failing derivation tests**

Add tests proving a commit endpoint yields repository, canonical endpoint, exact SHA, controller-derived action-pin purpose, related obligation, masked/bounded model context, and authority reason. Add rejection tests for dot segments, invalid owners/repos, non-repository endpoint shapes, fragments/URLs, and empty obligation IDs.

```python
request = repository_access_request(
    "repos/125m125/pr-reviewer-action/commits/" + "a" * 40,
    "OB-workflow",
    "Verify changed workflow dependencies.",
    "Check the pinned action behavior.",
    "Repo not allowed: 125m125/pr-reviewer-action",
)
assert request.repository == "125m125/pr-reviewer-action"
assert request.revision == "a" * 40
assert "pinned action revision" in request.purpose
assert request.model_purpose == "Check the pinned action behavior."
```

- [ ] **Step 2: Run RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_specialist_runtime_web.py -q -k "repository_access"`

Expected: collection/import failure because the type and constructor do not exist.

- [ ] **Step 3: Implement the immutable type and parser**

Use strict repository-segment syntax, canonical slash-separated endpoints, exact commit-SHA extraction only for `/commits/<sha>`, `mask_runtime_text` for model context, and bounded strings. Do not perform network access.

- [ ] **Step 4: Run GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_specialist_runtime_web.py -q`

Expected: all specialist web tests pass.

### Task 2: Tool Purpose Hints and Denial Capture

**Files:**
- Modify: `pr_reviewer/conversation.py`
- Modify: `pr_reviewer/specialist_runtime/session.py`
- Test: `tests/test_conversation.py`
- Test: `tests/test_specialist_runtime_session.py`

**Interfaces:**
- Adds optional `purpose: string` to `gh_api`, `web_search`, and `web_fetch` schemas.
- Session strips `purpose` before `execute_tool` and request-key calculation.
- Session retains `source_access_requests` as a tuple containing validated website or repository request objects.

- [ ] **Step 1: Write failing schema and session tests**

Assert all three schemas accept optional purpose. In a session test, invoke:

```python
{"name": "gh_api", "arguments": json.dumps({
    "endpoint": "repos/125m125/pr-reviewer-action/commits/" + "a" * 40,
    "purpose": "Verify the changed workflow's pinned action.",
    "targets": ["O1"],
})}
```

Make the executor return the normalized error for `Repo not allowed`. Assert the executor never receives `purpose`, one repository request is retained, its obligation is `O1`'s controller-owned ID, and a duplicate denial deduplicates. Add negative cases for missing token, endpoint-prefix denial, HTTP error, and invalid endpoint.

- [ ] **Step 2: Run RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_conversation.py tests/test_specialist_runtime_session.py -q -k "purpose or repository_access_request"`

Expected: schema assertions and retained-request assertions fail.

- [ ] **Step 3: Implement minimal capture**

Pop and bound `purpose` before the executor call. Detect only the exact structured repository-allowlist error shape after execution. Call Task 1 derivation for explicitly targeted obligation IDs, or current assignment gaps when untargeted. Preserve existing web-search request creation and incorporate the optional purpose as supplemental context without replacing its controller-derived baseline.

- [ ] **Step 4: Run GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_conversation.py tests/test_specialist_runtime_session.py -q`

Expected: both suites pass.

### Task 3: Controller, Artifact, Handoff, and Detail Notes

**Files:**
- Modify: `pr_reviewer/specialist_runtime/adjudication.py`
- Modify: `pr_reviewer/specialist_runtime/controller.py`
- Test: `tests/test_specialist_runtime_adjudication_adversarial.py`
- Test: `tests/test_specialist_runtime_controller.py`

**Interfaces:**
- Adds defensive parsing/rendering for `RepositoryAccessRequest`.
- `build_source_access_request_notes()` accepts both website and repository request types.
- Handoff `access_request_count` counts the union of valid typed requests.
- Artifact `source_access_requests` retains the repository-specific object with `kind=repository_access_request`.

- [ ] **Step 1: Write failing projection tests**

Create a repository request for an owned obligation and assert one general note contains repository, endpoint, revision, derived purpose, model context, authority reason, and the statement that no content was retrieved. Assert the sticky handoff contains only `Source access requests: 1 open`. Add hostile mapping tests for mismatched repository/endpoint, unknown obligation, unsupported kind, overlong purpose, and injected markdown.

- [ ] **Step 2: Run RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_specialist_runtime_adjudication_adversarial.py tests/test_specialist_runtime_controller.py -q -k "repository_access or source_access_request"`

Expected: repository requests are rejected or omitted by the source-only projection.

- [ ] **Step 3: Implement defensive union projection**

Validate repository request dictionaries against a strict field allowlist and canonical endpoint/repository relationship. Render a repository-specific note; reuse the current stable fingerprinting, deduplication, handoff count, event journal, and artifact paths. Keep `comment` mode note-free while retaining artifact/handoff state.

- [ ] **Step 4: Run GREEN**

Run: `.venv\Scripts\python.exe -m pytest tests/test_specialist_runtime_adjudication_adversarial.py tests/test_specialist_runtime_controller.py -q`

Expected: both suites pass.

### Task 4: Migration Guidance, Regression Verification, and Dogfood Pin

**Files:**
- Modify: `docs/migrations/specialist-session-runtime.md`
- Modify: `.github/workflows/ai-pr-review.yaml`
- Test: `tests/test_ai_pr_review_workflow.py`

**Interfaces:**
- Documents `tool_allowed_gh_api_repos`, typed requests, optional purposes, human approval, and bounded action-pin behavior.
- Dogfood workflow pins the exact runtime implementation commit in a separate pin-only commit.

- [ ] **Step 1: Update migration documentation**

Document that repository and website allowlists are separate, denied repository calls become requests rather than evidence, granting `owner/repo` does not preload the repository, and project owners should use a narrow repository list rather than `*`.

- [ ] **Step 2: Run focused verification**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_specialist_runtime_web.py tests/test_conversation.py tests/test_specialist_runtime_session.py tests/test_specialist_runtime_adjudication.py tests/test_specialist_runtime_adjudication_adversarial.py tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_cli.py tests/test_specialist_runtime_replay.py -q
git diff --check
```

Expected: all tests pass and diff check is clean.

- [ ] **Step 3: Run the broad portable suite**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests -q --tb=short --ignore=tests/test_api_key_argv.py --ignore=tests/test_evidence_providers.py --ignore=tests/test_resolve_finding_threads.py
```

Expected: all portable tests pass. Retain the exact known Windows-only exclusions in the final report.

- [ ] **Step 4: Commit runtime changes**

Stage only files named in this plan and commit with:

```text
Add typed GitHub repository access requests
```

- [ ] **Step 5: Repin and verify workflow**

Update `.github/workflows/ai-pr-review.yaml` to the exact full SHA from Step 4, run `.venv\Scripts\python.exe -m pytest tests/test_ai_pr_review_workflow.py -q`, and commit only the workflow as:

```text
Repin dogfood repository access requests
```
