# Planner Reasoning Continuation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve truncated planner reasoning and force a bounded JSON answer instead of immediately degrading to deterministic fallback.

**Architecture:** Add a planner-specific bounded role adapter that owns one temporary conversation across at most three physical requests. Keep controller validation and fallback unchanged.

**Tech Stack:** Python 3, pytest, OpenAI-compatible chat-completions payloads.

## Global Constraints

- At most one reasoning continuation and one reasoning-disabled final request.
- Reuse the same absolute planning deadline.
- Do not weaken planner validation, evidence rules, or context limits.
- The configured planner token limit governs planner physical requests.

---

### Task 1: Planner continuation adapter

**Files:**
- Modify: `pr_reviewer/specialist_runtime/cli.py`
- Modify if needed: `pr_reviewer/specialist_runtime/controller.py`
- Test: `tests/test_specialist_runtime_cli.py`
- Test: `tests/test_specialist_runtime_controller.py`

**Interfaces:**
- Consumes: `RoleRequest`, `ModelTurnRequest`, `ModelTurnResult`, and the compact planner context projector.
- Produces: a mapping parsed from the planner's bounded multi-request conversation.

- [ ] **Step 1: Write failing tests**

Add provider-transport tests where the first two responses contain only
`reasoning_content` and `finish_reason: length`, followed by valid JSON.
Assert the second payload retains first-turn reasoning, the third payload
retains accumulated reasoning and sends `reasoning_effort: none`, and no
fourth request occurs. Add a test that the planner sends 8192 output tokens
when the generic session limit is 4096.

- [ ] **Step 2: Verify the tests fail**

Run:

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\pytest.exe -q tests/test_specialist_runtime_cli.py tests/test_specialist_runtime_controller.py
```

Expected: failures showing the current adapter makes only one request,
discards reasoning, and sends 4096 planner output tokens.

- [ ] **Step 3: Implement the minimal bounded planner adapter**

Build the planner conversation once, append intermediate reasoning only after
a truncated response, issue one continuation, then issue one
reasoning-disabled JSON request. Parse only complete JSON objects and return
immediately on success. Preserve the existing deadline and response-format
settings on every request.

- [ ] **Step 4: Verify focused and specialist-runtime tests**

Run:

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\pytest.exe -q tests/test_specialist_runtime_cli.py tests/test_specialist_runtime_controller.py
.\.venv\Scripts\pytest.exe -q tests/test_specialist_runtime_*.py
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

Stage only the planner continuation implementation, its tests, and these
design/plan documents. Commit with:

```text
fix(planner): preserve truncated reasoning before finalization
```
