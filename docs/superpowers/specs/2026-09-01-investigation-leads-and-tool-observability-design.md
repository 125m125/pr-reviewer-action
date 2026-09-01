# Investigation Leads and Tool Observability Design

## Goal

Keep checkpoint continuation memory private to its specialist, retain concrete
cross-assignment concerns as controller-owned work, and make tool availability
and usage visible in the GitHub Actions job summary without leaking sensitive
arguments.

## Non-goals

- Do not turn free-form checkpoint todos into scheduler authority.
- Do not automatically create coverage obligations from investigation leads.
- Do not publish raw leads, checkpoint todos, tool arguments, search queries, or
  private endpoints in the PR handoff.
- Do not replace the existing candidate, obligation-assessment, evidence, or
  access-request lifecycles.

## Checkpoint continuation memory

`SessionCheckpoint.proposed_next_actions` remains an informal todo list for the
same model session. It is preserved only when a compaction checkpoint is followed
immediately by continuation of that same durable session. It is not included in
negotiation targets and is not projected into the handoff summarizer.

When a checkpoint ends exploration because the session stopped, completed,
paused for controller negotiation, or entered finalization, its proposed actions
expire operationally. Emergency checkpoints follow the same rule: preserve the
actions only for an immediate same-session resume. The structured artifact may
retain the original strings for diagnostics, together with an event describing
whether they were preserved or expired, but they cannot authorize later work.

Fresh controller feedback on a later resume contains the accepted obligation
state, active candidates and leads, and the controller-selected reason for the
resume. It does not replay an expired informal todo list.

## Investigation lead lifecycle

An investigation lead represents a concrete, evidence-backed concern that needs
more work before it can become a finding. It is separate from a coverage
obligation and from a rejected candidate draft.

The specialist tool catalogue adds `report_investigation_lead` with this compact
input contract:

```json
{
  "summary": "upload-artifact may require a permission not granted here",
  "affected_paths": [".github/workflows/ai-pr-review.yaml"],
  "evidence_ids": ["evidence:..."],
  "next_action": "Check the documented permission requirements for actions/upload-artifact",
  "required_capability": "web"
}
```

`summary`, at least one retained evidence ID, and one concrete `next_action` are
required. `affected_paths` is bounded to repository paths visible to the review.
`required_capability` is a small controller-defined enum such as `repository`,
`tests`, `web`, or `none`; it is a routing hint, not permission to enable a tool.
If evidence already supports a concrete defect, the prompt directs the specialist
to use `report_candidate` instead. A rejected candidate continues through the
existing focused-repair path rather than being duplicated as a lead.

The controller derives a stable canonical lead ID, exposes a bounded `L#` handle
to the negotiator and assigned specialist, deduplicates materially equivalent
leads, records their origin session and evidence, and maintains one of these
states:

- `open`: accepted and awaiting routing;
- `scheduled`: assigned to an existing or new specialist;
- `resolved_candidate`: produced an admitted candidate;
- `resolved_no_issue`: investigation disproved or safely exhausted the concern;
- `blocked`: an access or external-state boundary prevents investigation;
- `dropped`: duplicate, invalid, immaterial, or outside the immutable review
  boundary.

Leads exist only for the current immutable review run. They are preserved in the
artifact for audit but are not carried into a later PR review.

A session explicitly assigned an `L#` target also receives the narrow
`resolve_investigation_lead` tool. It can mark that target `resolved_no_issue` or
`blocked` with a concrete reason and retained evidence IDs. An admitted candidate
from a one-lead assignment marks the target `resolved_candidate`. Silence never
resolves a lead.

## Routing and negotiation

The controller, not the model negotiator, determines which routes are valid. It
first checks whether a lead is already covered by an admitted candidate, accepted
obligation assessment, or equivalent active lead. For a remaining lead it offers
bounded actions in this order:

1. resume the originating specialist when its boundary, capabilities, and budget
   fit;
2. consult another existing specialist whose assignment and tools fit;
3. start one new specialist only when the scope is distinct and run/session caps
   allow it;
4. convert an unavailable repository or web capability into the existing typed
   access-request path;
5. mark the lead blocked or resolved without further model work.

The compact negotiation catalogue contains both unresolved obligation targets
(`U#`) and accepted lead targets (`L#`). Obligation targets derive next actions
only from controller-accepted `ObligationAssessment.next_actions`; checkpoint
`proposed_next_actions` are never merged into them. The negotiator still chooses
one action from the controller-supplied allowlist, and the controller revalidates
the choice before applying it.

Scheduling a lead does not create a new coverage obligation. A lead assignment
has a narrow investigation objective and may end in a candidate, a disproven
lead, or a blocked/exhausted lead. This avoids inflating coverage accounting for
opportunistic discoveries.

## Handoff boundary

The handoff summarizer may use bounded checkpoint working summaries and completed
steps to describe what the AI reviewed, but it does not receive checkpoint
`proposed_next_actions`. `human_focus` is assembled from authoritative final
state only: published verification notes, retained material unknowns, degraded
stages, unresolved material obligations, typed access requests, and unresolved
material investigation leads.

An open lead is not automatically a PR comment. It may contribute a concise
human-focus sentence only when the controller retained it as material and no
model session could resolve it. Raw hypotheses and dropped or disproved leads
never reach the PR handoff.

## Tool observability

The specialist artifact records tool lifecycle events using the existing session
journal and tool-result status rather than adding a second execution log. The
controller aggregates, per tool:

- number of sessions in which it was advertised;
- calls attempted;
- successful, rejected, deferred, and errored results;
- retained evidence records produced.

`specialist-review-summary.md`, which is already appended to
`GITHUB_STEP_SUMMARY`, gains an **AI specialist tools** table with those values.
This covers repository, diff, test-result, web, access, evidence-recovery,
candidate, withdrawal, obligation, and lead tools. A separate collapsible
**External access policy** section reports whether search/fetch/repository access
was enabled, the bounded approved hosts/path prefixes or repositories, and typed
access-request counts. It never prints credentials, endpoint URLs, model-supplied
purposes, queries, or full tool arguments.

The structured artifact also exposes the aggregate and lead-transition reasons
so failures can be diagnosed without parsing raw model logs.

## Failure handling and bounds

- Invalid leads return a focused tool error and do not consume follow-up capacity.
- Duplicate leads return the existing `L#` handle and record a deduplication
  event; they do not count as progress repeatedly.
- A run-wide lead cap prevents model-generated lead floods. Beyond the cap,
  submissions are rejected with a deterministic reason.
- A lead cannot expand repository paths, immutable revisions, tool allowlists, or
  web/repository permissions.
- Negotiation may not schedule a lead without a concrete next action and a route
  that fits remaining time, turn, tool-call, and session budgets.

## Verification

Focused tests cover:

1. checkpoint actions survive immediate same-session compaction only and are
   absent from negotiation and handoff projections;
2. lead admission, evidence/path validation, deduplication, run cap, and terminal
   transitions;
3. controller-generated lead route allowlists and negotiator validation for `L#`
   targets;
4. lead assignments producing a candidate, no-issue resolution, or typed access
   request without creating an obligation;
5. authoritative handoff filtering for expired todos and unresolved material
   leads;
6. tool-summary aggregation across advertised, successful, rejected, deferred,
   errored, and evidence-producing calls, including redaction of sensitive data;
7. structured artifact serialization and replay for the new lead and telemetry
   fields.

Existing session, negotiation, controller, publishing, workflow-summary, replay,
and specialist-runtime suites remain green.
