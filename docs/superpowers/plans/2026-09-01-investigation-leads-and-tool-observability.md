# Investigation Leads and Tool Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Separate checkpoint continuation memory from scheduler authority, route evidence-backed cross-assignment concerns as bounded investigation leads, and expose safe per-tool runtime statistics in the GitHub Actions job summary.

**Architecture:** Reuse the existing specialist session, negotiation projection, controller run state, event journal, artifact, and summary writer. Sessions admit compact lead records and tool counters; the controller deduplicates and routes leads as `L#` negotiation targets without creating obligations. The handoff summarizer phrases focus from bounded session summaries and controller-approved focus facts; tool reporting is projected from controller-owned terminal state.

**Tech Stack:** Python 3 dataclasses/enums, pytest, existing specialist runtime event/artifact model, Bash/GitHub Actions job summary Markdown.

**Spec:** `docs/superpowers/specs/2026-09-01-investigation-leads-and-tool-observability-design.md`

## Global Constraints

- Checkpoint `proposed_next_actions` are same-session continuation memory only.
- Investigation leads never expand immutable paths, revisions, tool allowlists, or external-access permissions.
- Leads do not create or satisfy coverage obligations.
- Sensitive tool arguments, purposes, queries, credentials, and endpoint URLs never enter the job summary.
- Reuse the existing event journal and specialist summary; add no dependency or parallel scheduler.
- A run admits at most 32 canonical investigation leads.

---

### Task 1: Isolate checkpoint continuation memory

**Files:**
- Modify: `pr_reviewer/specialist_runtime/negotiation.py:230-355`
- Modify: `pr_reviewer/specialist_runtime/session.py:4576-4604`
- Modify: `pr_reviewer/specialist_runtime/controller.py:3985-4051`
- Test: `tests/test_specialist_runtime_negotiation.py`
- Test: `tests/test_specialist_runtime_session.py`
- Test: `tests/test_specialist_runtime_controller.py`

**Interfaces:**
- Consumes: `ObligationAssessment.next_actions` as the only follow-up actions for `U#` targets.
- Produces: negotiation and handoff projections that never contain checkpoint `proposed_next_actions`; explicit resume feedback that expires old model todos.

- [ ] **Step 1: Write failing negotiation and handoff tests**

Add a negotiation test whose checkpoint contains `("Search unrelated docs",)` while the accepted obligation assessment contains `("Read the changed consumer",)`. Assert:

```python
target = compact_negotiation_context(state)["targets"][0]
assert target["next_actions"] == ("Read the changed consumer",)
assert "Search unrelated docs" not in json.dumps(target)
```

Update the handoff checkpoint-summary test to assert:

```python
summary = observed["specialist_checkpoint_summaries"][0]
assert "proposed_next_actions" not in summary
```

- [ ] **Step 2: Run the focused tests and confirm they fail**

Run:

```powershell
pytest tests/test_specialist_runtime_negotiation.py tests/test_specialist_runtime_controller.py -q
```

Expected: failures show checkpoint actions still entering negotiation and handoff context.

- [ ] **Step 3: Remove the two authority leaks**

In `compact_negotiation_context`, derive `next_actions` only from the accepted assessment:

```python
next_actions = assessment.next_actions if assessment is not None else ()
```

Remove `proposed_next_actions` from `_specialist_checkpoint_summaries`. Keep working summaries and completed steps for `ai_reviewed_summary` orientation.

In `apply_coverage_feedback`, explicitly override previous model todos:

```python
self.conversation.add_user(
    "The previous checkpoint proposed_next_actions have expired. Follow only "
    "these controller-accepted actions: " + json.dumps(next_actions)
)
```

The immediate compaction paths `_compact_validated_epoch` and `_reconstruct_from_valid_checkpoint` continue to carry the list because they resume the same session without returning control to negotiation.

- [ ] **Step 4: Add a session test for immediate compaction versus later resume**

Assert the epoch-continuation payload still includes the checkpoint actions, then call `apply_coverage_feedback` and assert the newest user event states that old actions expired and names only accepted obligation actions.

- [ ] **Step 5: Run the focused tests**

Run:

```powershell
pytest tests/test_specialist_runtime_negotiation.py tests/test_specialist_runtime_session.py tests/test_specialist_runtime_controller.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```powershell
git add pr_reviewer/specialist_runtime/negotiation.py pr_reviewer/specialist_runtime/session.py pr_reviewer/specialist_runtime/controller.py tests/test_specialist_runtime_negotiation.py tests/test_specialist_runtime_session.py tests/test_specialist_runtime_controller.py
git commit -m "Keep checkpoint todos out of review scheduling"
```

### Task 2: Admit and resolve investigation leads inside a specialist session

**Files:**
- Modify: `pr_reviewer/specialist_runtime/types.py:31-158`
- Modify: `pr_reviewer/specialist_runtime/assignments.py:69-90`
- Modify: `pr_reviewer/specialist_runtime/session.py:210-290, 934-994, 1084-1300, 2253-2485, 5214-5228`
- Test: `tests/test_specialist_runtime_session.py`

**Interfaces:**
- Produces: `InvestigationLead`, `InvestigationLeadStatus`, `LeadResolution`, `SessionResult.investigation_leads`, and `SessionResult.lead_resolutions`.
- Produces tools: `report_investigation_lead` for all normal specialist sessions and `resolve_investigation_lead` only for sessions assigned a lead.
- Consumes: retained `EvidenceRecord` IDs, normalized changed paths, and optional `Assignment.investigation_leads`.

- [ ] **Step 1: Write failing tool-contract tests**

Add tests asserting the ordinary tool catalogue includes `report_investigation_lead`, but not `resolve_investigation_lead`. Submit:

```python
{
    "summary": "The permission may be insufficient for artifact upload.",
    "affected_paths": [".github/workflows/ci.yml"],
    "evidence_ids": [retained.id],
    "next_action": "Check the official upload-artifact permission contract.",
    "required_capability": "web",
}
```

Assert the result is accepted and the session snapshot contains one lead. Add rejection cases for an unknown evidence ID, an unobserved path, an invalid capability, and a 33rd distinct lead.

- [ ] **Step 2: Run the session tests and confirm they fail**

Run:

```powershell
pytest tests/test_specialist_runtime_session.py -q -k "investigation_lead"
```

Expected: the tool schemas and lead result fields do not yet exist.

- [ ] **Step 3: Add immutable lead domain values**

Add to `types.py`:

```python
class InvestigationLeadStatus(str, Enum):
    OPEN = "open"
    SCHEDULED = "scheduled"
    RESOLVED_CANDIDATE = "resolved_candidate"
    RESOLVED_NO_ISSUE = "resolved_no_issue"
    BLOCKED = "blocked"
    DROPPED = "dropped"


@dataclass(frozen=True)
class InvestigationLead:
    lead_id: str
    summary: str
    affected_paths: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    next_action: str
    required_capability: str
    origin_session_id: str
    status: InvestigationLeadStatus = InvestigationLeadStatus.OPEN
    assigned_session_id: str | None = None
    resolution_reason: str = ""
    candidate_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LeadResolution:
    lead_id: str
    status: InvestigationLeadStatus
    reason: str
    evidence_ids: tuple[str, ...] = ()
    candidate_ids: tuple[str, ...] = ()
```

Add `investigation_leads: tuple[InvestigationLead, ...] = ()` to `Assignment` and detached lead/resolution tuples to `SessionResult`.

- [ ] **Step 4: Add the compact report and resolution schemas**

`report_investigation_lead` requires `summary`, `evidence_ids`, `next_action`, and `required_capability`; it accepts at most eight affected paths and eight evidence IDs. `required_capability` is one of `none`, `repository`, `tests`, or `web`.

`resolve_investigation_lead` is appended only when `Assignment.investigation_leads` is non-empty. It requires:

```json
{
  "target": "L1",
  "status": "resolved_no_issue",
  "reason": "The documented contract confirms no extra permission is required.",
  "evidence_ids": ["evidence:..."]
}
```

Allowed resolution statuses are `resolved_no_issue` and `blocked`.

- [ ] **Step 5: Implement deterministic session admission**

Normalize lead text and paths, resolve cited evidence IDs against the session store, and permit an affected path only when it is changed or is the `source_path` of cited retained evidence. Derive a canonical ID without model input:

```python
identity = hashlib.sha256(json.dumps(
    {"summary": summary.casefold(), "paths": paths, "next_action": next_action.casefold()},
    sort_keys=True,
).encode("utf-8")).hexdigest()[:16]
lead_id = f"lead:{identity}"
```

Return the existing lead on a duplicate identity. Store at most 32 distinct leads. When a one-lead assignment admits a candidate, append a `resolved_candidate` resolution referencing the admitted candidate ID. Silence creates no resolution.

- [ ] **Step 6: Implement focused lead feedback for resumed sessions**

Add:

```python
def apply_investigation_lead_feedback(
    self, target: str, lead: InvestigationLead,
) -> None:
    ...
```

It registers the controller target, advertises `resolve_investigation_lead`, appends a user message containing only that lead and permitted next action, and resets the no-progress streak. It does not alter the obligation ledger.

- [ ] **Step 7: Run the session suite**

Run:

```powershell
pytest tests/test_specialist_runtime_session.py -q
```

Expected: all session tests pass.

- [ ] **Step 8: Commit**

```powershell
git add pr_reviewer/specialist_runtime/types.py pr_reviewer/specialist_runtime/assignments.py pr_reviewer/specialist_runtime/session.py tests/test_specialist_runtime_session.py
git commit -m "Add specialist investigation lead tools"
```

### Task 3: Route controller-owned `L#` targets without creating obligations

**Files:**
- Modify: `pr_reviewer/specialist_runtime/negotiation.py:74-355, 430-830`
- Modify: `pr_reviewer/specialist_runtime/controller.py:1765-1815, 3290-3610, 5750-5825`
- Test: `tests/test_specialist_runtime_negotiation.py`
- Test: `tests/test_specialist_runtime_controller.py`

**Interfaces:**
- Consumes: canonical `InvestigationLead` records collected from `SessionResult`, plus `SessionResources.advertised_tools`.
- Produces: `NegotiationState.investigation_leads`, mixed `U#`/`L#` compact targets, and `NegotiationAction.lead_ids` with exactly one of `obligation_ids` or `lead_ids` populated.
- Produces: one-obligation-free follow-up `Assignment` per new-session lead action.

- [ ] **Step 1: Write failing negotiation tests for `L#` targets**

Create an open web lead whose origin session advertises `web_search`. Assert:

```python
context = compact_negotiation_context(state)
lead_target = next(item for item in context["targets"] if item["handle"] == "L1")
assert lead_target["allowed_actions"][0] == "resume"
assert lead_target["next_actions"] == (lead.next_action,)
```

Add cases where the origin lacks the capability but another session can be consulted, no session is feasible but `new_session` is allowed, and no route remains so only `record_unknown` is allowed. Assert checkpoint todos never create an `L#` target.

- [ ] **Step 2: Run negotiation tests and confirm they fail**

Run:

```powershell
pytest tests/test_specialist_runtime_negotiation.py -q -k "lead"
```

Expected: negotiation state does not accept leads or advertised-tool capabilities.

- [ ] **Step 3: Extend compact negotiation and validation**

Add `advertised_tools: tuple[str, ...] = ()` to `SessionResources`, `investigation_leads` to `NegotiationState`, and `lead_ids: tuple[str, ...] = ()` to `NegotiationAction`.

Project open leads after obligations in stable canonical-ID order. `L#` summaries contain the bounded lead summary, required capability, and concrete next action. Validate compact proposals by resolving the target against a tagged union of obligation and lead targets. A full action must contain exactly one non-empty target collection:

```python
if bool(obligation_ids) == bool(lead_ids):
    errors.append("action must target exactly one obligation set or one lead")
```

Capability matching uses advertised tool names:

```python
required_tools = {
    "none": frozenset(),
    "repository": frozenset({"read_file", "read_pr_diff", "git_grep"}),
    "tests": frozenset({"read_test_results"}),
    "web": frozenset({"web_search", "web_fetch"}),
}
```

Any intersection is sufficient for a non-`none` capability; actual calls remain guarded by existing policy.

- [ ] **Step 4: Write failing controller lifecycle tests**

Simulate two sessions reporting the same canonical lead. Assert one controller lead remains, the journal records one admission and one deduplication, and no coverage obligation is added. Add scenarios for resume, consult, new session, candidate resolution, explicit no-issue resolution, blocked access, and the 32-lead run cap.

- [ ] **Step 5: Implement controller admission and transitions**

Add `investigation_leads: dict[str, InvestigationLead]` to `_RunState`. After every wave, merge session leads and resolutions before reconciliation, journal bounded `investigation_lead_*` transitions, and ignore invalid/foreign resolutions.

When applying a lead action:

- `record_unknown` marks the lead `blocked` with the controller reason;
- `resume` or `consult` invokes `apply_investigation_lead_feedback` with the chosen `L#` target;
- `new_session` creates one narrow `Assignment` with empty obligation fields, lead paths as seeds/boundaries, `investigation_leads=(lead,)`, and existing remaining turn/tool limits.

Do not call `fallback_assignment_plan` for a lead.

- [ ] **Step 6: Let the negotiation loop drain either work type**

Change the controller loop condition from only uncovered obligations to:

```python
def has_followup_work() -> bool:
    return bool(reconciliation.uncovered_obligation_ids) or any(
        lead.status is InvestigationLeadStatus.OPEN
        for lead in state.investigation_leads.values()
    )
```

Progress includes a lead status transition as well as new evidence or covered obligations. Retire only the selected unproductive `U#` or `L#` target.

- [ ] **Step 7: Run negotiation and controller suites**

Run:

```powershell
pytest tests/test_specialist_runtime_negotiation.py tests/test_specialist_runtime_controller.py -q
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```powershell
git add pr_reviewer/specialist_runtime/negotiation.py pr_reviewer/specialist_runtime/controller.py tests/test_specialist_runtime_negotiation.py tests/test_specialist_runtime_controller.py
git commit -m "Route controller-owned investigation leads"
```

### Task 4: Keep human focus bounded and expose safe tool statistics

**Files:**
- Modify: `pr_reviewer/specialist_runtime/session.py:1084-1300, 2622-2925, 5214-5228`
- Modify: `pr_reviewer/specialist_runtime/controller.py:4053-4200, 4700-4937, 4976-5313`
- Modify: `pr_reviewer/specialist_runtime/cli.py:150-205, 1744-1805, 2183-2353`
- Modify: `docs/migrations/specialist-session-runtime.md`
- Test: `tests/test_specialist_runtime_session.py`
- Test: `tests/test_specialist_runtime_controller.py`
- Test: `tests/test_specialist_runtime_cli.py`
- Test: `tests/test_step_summary.sh`

**Interfaces:**
- Produces: `SessionResult.advertised_tools` and `SessionResult.tool_activity` aggregate rows.
- Produces artifact fields: `investigation_leads`, `tool_activity`, and bounded lead/tool transition reasons in the existing event stream.
- Produces job-summary sections: `AI specialist tools` and `External access policy`.

- [ ] **Step 1: Write failing tool-activity tests**

Run one session that advertises `read_file`, `web_search`, and the local controller tools; exercise successful, rejected, deferred, errored, duplicate, and evidence-producing calls. Assert the detached snapshot reports each tool with:

```python
{
    "tool": "read_file",
    "calls": 1,
    "successful": 1,
    "rejected": 0,
    "deferred": 0,
    "errored": 0,
    "evidence_retained": 1,
}
```

- [ ] **Step 2: Implement one central activity recorder**

Store the advertised schema names once after `_advertise_obligation_associations`. Add a private helper used by every `_execute_calls` exit path:

```python
def _record_tool_activity(
    self, name: str, status: str, *, evidence_retained: int = 0,
) -> None:
    counters = self._tool_activity.setdefault(name, Counter())
    counters["calls"] += 1
    counters[status] += 1
    counters["evidence_retained"] += evidence_retained
```

Deferred calls are recorded once and may be retried after compaction. Duplicate successful requests count as rejected/replayed with zero new evidence, matching existing no-progress semantics.

- [ ] **Step 3: Write failing artifact and handoff tests**

Assert the artifact contains canonical leads and aggregate tool rows, and that `_validate_artifact` rejects malformed rows. Feed the handoff summarizer a checkpoint containing a provocative proposed action and assert it never receives that field. Assert its human-focus input contains only bounded checkpoint summaries and controller-approved focus facts.

- [ ] **Step 4: Keep `human_focus` model-authored from bounded facts**

Keep `human_focus` in the handoff-summarizer contract, but exclude checkpoint `proposed_next_actions`. Supply bounded checkpoint summaries plus controller focus facts derived from prepared verification notes, material terminal unknowns/degradations, typed access requests, unresolved material obligations, and open/blocked controller leads. Use controller prose as the fallback when the model returns an empty focus. Keep it concise; do not publish resolved/dropped leads.

- [ ] **Step 5: Aggregate tool activity and external policy in the artifact**

Merge per-session advertised tools and counters by tool name. Add:

```json
"tool_activity": {
  "tools": [{
    "tool": "read_file",
    "advertised_sessions": 3,
    "calls": 8,
    "successful": 7,
    "rejected": 1,
    "deferred": 0,
    "errored": 0,
    "evidence_retained": 7
  }],
  "external_access": {
    "web_search_enabled": true,
    "web_fetch_enabled": true,
    "repository_api_enabled": false,
    "approved_sources": ["docs.github.com/en/actions"],
    "approved_repositories": [],
    "access_request_count": 1
  }
}
```

Derive approved sources/repositories from the already-normalized runtime policy/configuration projection and cap each displayed list at 20 entries. Store no endpoint or tool argument.

- [ ] **Step 6: Render the job-summary tables**

In `_write_outputs`, append:

```markdown
## AI specialist tools

| Tool | Advertised sessions | Calls | Successful | Rejected | Deferred | Errors | Evidence retained |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
```

Then render `<details><summary>External access policy</summary>` with enabled flags, bounded allowlisted sources/repositories, and access-request count. Pass every cell through `_summary_cell` and never render raw event payloads.

- [ ] **Step 7: Update operator migration guidance**

Document that checkpoint todos no longer schedule work, specialists can report evidence-backed cross-scope leads, the handoff model phrases focus from bounded summaries and controller-approved focus facts, and the Actions summary now shows tool/external-access diagnostics. No repository configuration change is required.

- [ ] **Step 8: Run focused suites**

Run:

```powershell
pytest tests/test_specialist_runtime_session.py tests/test_specialist_runtime_negotiation.py tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_cli.py -q
bash tests/test_step_summary.sh
```

Expected: all selected Python and shell tests pass.

- [ ] **Step 9: Commit**

```powershell
git add pr_reviewer/specialist_runtime/session.py pr_reviewer/specialist_runtime/controller.py pr_reviewer/specialist_runtime/cli.py docs/migrations/specialist-session-runtime.md tests/test_specialist_runtime_session.py tests/test_specialist_runtime_controller.py tests/test_specialist_runtime_cli.py tests/test_step_summary.sh
git commit -m "Report specialist tool activity and authoritative handoff focus"
```

### Task 5: Verify the integrated review runtime

**Files:**
- Modify only if a test exposes a defect in the preceding tasks.

**Interfaces:**
- Consumes: all interfaces produced by Tasks 1-4.
- Produces: a verified implementation with no unintended action/workflow schema changes.

- [ ] **Step 1: Run specialist-runtime regression tests**

Run:

```powershell
pytest tests/test_specialist_runtime_*.py tests/test_specialists.py tests/test_github_review_notes.py tests/test_ai_pr_review_workflow.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run action and summary validation**

Run:

```powershell
python -c "import yaml; yaml.safe_load(open('action.yml', encoding='utf-8'))"
bash tests/test_step_summary.sh
git diff --check
```

Expected: YAML parses, the shell summary test passes, and `git diff --check` prints nothing.

- [ ] **Step 3: Inspect the final diff**

Run:

```powershell
git status --short
git diff --stat HEAD~4..HEAD
git diff HEAD~4..HEAD -- pr_reviewer/specialist_runtime docs/migrations tests
```

Confirm the diff contains no new action input, dependency, dynamic obligation creation, PR-comment tool table, or raw tool arguments.

- [ ] **Step 4: Record verification evidence**

Add the exact passing commands and counts to the final handoff. If a platform-only shell test cannot run on Windows, record the exact failure and run its Python/unit equivalent; do not call the suite green without evidence.
