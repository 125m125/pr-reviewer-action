# Component-owned review with bounded delegation

Status: proposal for review; not implemented.

### Confirmed design decisions

- Use group-level coverage assessments with explicit omissions, not mandatory
  per-file disposition records.
- Require explicit policy migration; do not retain an unmigrated legacy execution
  path or silently reinterpret old recipe settings.
- Specialists propose delegation; the controller checks feasibility without an
  additional LLM approval call.
- Cross-component evidence gaps become targeted leads for the negotiator, not
  automatic specialist launches.
- Delegate only a subset of remaining work; the parent retains substantive work.
- Assign unmatched changed files to one repository-remainder assignment and
  highlight missing ownership configuration in the action summary.
- Require an explicit policy file and provide a minimal single-component quick start.
- Remove initial LLM assignment planning; retain change summarization and follow-up negotiation.
- Reuse lead infrastructure for delegation with explicit transfer semantics and
  risk-based scheduling, without a fixed scheduling-round cap.
- Start boundary checks with all configured participants; permit lightweight,
  scoped not-affected assessments supported by a relevant check.
- Reuse existing evaluator model/context/output and structured-recovery settings.

## 1. Goal and boundaries

Replace an expanding graph of independently accountable file, evidence-category,
and inferred-interaction obligations with component-owned investigations.
Specialists may delegate coherent subsets after limited orientation. The
controller remains authoritative for ownership, budgets, evidence, and coverage.

The goal is less repeated investigation and checkpoint bookkeeping, not a lower
standard for findings or a claim that a large change needs little inspection.
There is no target finding count and no fixed target obligation count.

We do not introduce language-specific dependency parsers, infer functional
architecture from arbitrary directory depth, or ask an additional model to read
the whole PR before specialists begin. On a single local model, delegation is
primarily context isolation, not parallel inference.

## 2. Why change the current method?

The inspected movieHRdb run 35055456916 recorded 139 coverage entries:

| Source | Count |
| --- | ---: |
| Cross-component interactions | 47 |
| Changed implementation-file checks | 42 |
| Other topology checks | 22 |
| Recipe evidence requirements | 20 |
| General risk flags | 3 |
| Failed-test triage | 1 |
| Not-applicable recipe accounting | 4 |

The 47 interaction entries each carried all 87 changed paths as scope. Some
relationships came from coarse roles, not an established dependency. Several
requirements described different evidence for the same investigation.

The plan contained 12 assignments, while five sessions ran. Reducing prompt size
alone does not address this mismatch between planned and feasible work.

### Alternatives considered

- Keep the existing obligations and increase specialists/budgets: distributes
  overlapping work without addressing its source.
- Configure every functional subsystem or infer a dependency graph: requires
  substantial project maintenance or language-aware analysis.
- **Recommended:** deterministic coarse ownership, recipe guidance, and bounded
  model-proposed delegation when actual separable work becomes apparent.

## 3. Separate scope, requirements, and hints

| Concept | Meaning | Completion semantics |
| --- | --- | --- |
| Changed-file inventory | Authoritative changed paths/hunks to account for | Read alone does not mean reviewed; unreviewed scope stays visible |
| Review requirement | A substantive correctness question or explicit policy invariant | Can be supported, partially assessed, unresolved, or inapplicable with a reason |
| Exploration hint | Possible related code, tests, contract consumers, or risk | No independent closure requirement |
| Assignment | One specialist's owned scope and relevant requirements | May finish its lease with incomplete coverage |

Examples of requirements: changed behavior preserves authorization; changed
message producer/consumer semantics agree; a migration preserves required data.
Examples of hints: a related component, a generic dependency-change flag, possible
generated output, and likely test locations.

Evidence categories such as producer, consumer, schema, and tests normally guide
how to answer a question. They do not each create a separate investigation.
Explicit project requirements for particular evidence or independent verification
remain requirements; this redesign must not silently weaken them.

## 4. Minimal repository knowledge

Use existing path-based components, responsibilities, invariants, and recipes.
Add explicit distinctions where needed between review-owning components and
shared contract sources. Names below describe semantics, not a finalized JSON
schema. Explicit migration to the new policy format is required. Reject
unmigrated configurations before model calls with a clear migration error; there
is no fallback to the old review method. Finalize the version discriminator and
field names during implementation planning.

A missing policy file also fails before model calls, with setup guidance. Provide
a validated quick start declaring one repository-wide component; intentional setup
is required, but detailed component/boundary configuration is not. The remainder
assignment handles unmatched files under a valid policy, not an absent policy.

Configuration needs to express:

1. Which components own implementation or operational behavior.
2. Which paths are shared contracts and which owners participate in them.
3. A primary investigation owner for contract-only changes. Cross-owner
   reconciliation belongs to the bounded evaluator described in section 6.
4. Optional endpoint path patterns that activate a known boundary without a
   contract-file change. These are conventions, not semantic dependency analysis.

movieHRdb example:

| Shared source | Participants | Contract-only investigation owner |
| --- | --- | --- |
| Backend/frontend OpenAPI | Java backend, Angular frontend | Java backend |
| Backend/mobile OpenAPI, where applicable | Java backend, Flutter app | Java backend |
| Backend/worker AsyncAPI and Protobuf | Java backend, Python worker | Java backend |
| Database schema | Java backend, Python worker | Designated persistence owner |

These are proposed mappings to validate against the actual generator layout.
They do not assert that every operation reaches every participant.

A database component can be both a contract source and an owner of migrations or
database-specific behavior. Deployment, identity, and analytics also need explicit
ownership when changed; they must not disappear behind four application owners.

For overlapping component globs, require explicit ownership precedence rather
than assigning a file to several default owners. Unmatched paths enter one
visible repository-remainder assignment rather than failing the entire review.
The action summary highlights those unmatched paths as missing ownership
configuration. This fallback does not excuse invalid explicit boundary mappings:
declared contracts must reference valid participants and an investigation owner.

## 5. Initial deterministic assignment generation

1. Build the immutable changed-file inventory and match existing components.
2. Create one initial assignment per affected review owner, subject to scheduling
   capacity. Queued assignments still count as unreviewed until actually assessed.
3. Attach applicable recipe objectives and invariants as a concise checklist.
   Do not create a requirement for each recipe evidence category.
4. Attach relevant shared contracts and configured relationships as context.
5. Activate boundary checks for changed contract paths or explicit endpoint
   patterns, not merely because both related components changed.
6. Give contract-only changes to their designated primary owner, which may trace
   unchanged consumers. A shared source does not automatically create a specialist.
7. Integrate ordinary recipes into component guidance. Preserve explicitly
   independent checks as separate review requirements, with intentional overlap
   recorded. Migration must explicitly translate old `coverage` and `dedicated`
   recipes into component guidance, or declare independent review where that is
   actually required; old execution settings are not silently reinterpreted.

There is no initial LLM assignment-transformation call. Component specialists can
propose informed subset delegation after orientation; the change summarizer and
follow-up negotiator remain.

For an implementation-only API change not recognized by path patterns, the owner
still reviews the behavior and follows relevant contracts when discovered. We
cannot promise exhaustive deterministic boundary activation without configuration.

### movieHRdb example

- Backend: changed Java behavior; applicable persistence, API, and delivery checks.
- Worker: changed Python behavior; applicable persistence and delivery checks.
- Frontend: changed Angular behavior and affected OpenAPI consumption.
- Deployment: changed service/configuration behavior, if material changes exist.
- Mobile: only when its implementation changes or concrete contract work requires it.

This is not an automatic four-specialist ceiling. Nor does every listed component
run merely because a neighboring component changed.

## 6. Bounded cross-component evaluation

Each implementation owner checks its side of an activated boundary and records
a concise assessment of actual behavior, limitations, and retained evidence.
A fresh, tools-disabled LLM evaluator compares the relevant results. The backend
specialist does not become a long-lived boundary coordinator and does not need
to recover its earlier reasoning to reconcile a later worker result.

The evaluator is a bounded comparison call, not an API specialist performing a
second full investigation. It is distinct from the critic: it assesses boundary
compatibility, while the critic evaluates submitted candidate findings.

### Retained input and information flow

Local assessment updates carry the substantive observation, not just a covered
status. The controller retains these with their evidence references independently
of conversation compaction. At a scheduling/reconciliation point it assembles:

- The activated boundary question and relevant authoritative contract excerpts.
- Relevant local assessments and their limitations for each required participant.
- Actual retained implementation excerpts, with paths, line information where
  available, and evidence IDs; not only summaries or opaque IDs.
- The immutable repository revisions and assessment versions being evaluated.

The packet is focused and subject to the ordinary model context/output limits.
Missing or truncated material is explicit. If the needed evidence cannot fit, the
comparison stays incomplete and may produce a targeted investigation lead; it
must not accept compatibility from summaries alone.

### movieHRdb example

For a backend-to-worker message, Java may populate `showId` with a database ID
while Python uses it in a database primary-key lookup. Both owners retain the
code establishing those behaviors. The evaluator compares the source excerpts,
the applicable schema, destination, and relevant stated limitations.

If Java instead sends an external provider ID, both local descriptions could be
correct and both payloads schema-valid, while the integration is incompatible.
The evaluator must compare filling and reading semantics, not merely agreement
with the schema or matching covered statuses. It evaluates only the stated
question, not every possible property of the whole API.

Start with all participants configured for the activated boundary. A participant
can be marked not affected for the specific change using a brief reason and a
relevant retained search, lookup, or source inspection. Require a genuine check,
not an exhaustive call graph or exact proof phrasing. Failed, blocked, or materially
truncated searches alone do not suffice, and known contradictory evidence prevents
acceptance. Report this as no affected usage found in the checks performed, not
proven absence. The evaluator receives these scoped assessments and supporting
evidence; it does not re-demand full implementation proof from excluded participants.
New contradictory evidence can reopen their applicability.

### Outcomes and scheduling

| Outcome | Controller treatment |
| --- | --- |
| Supported | Record the specific combined requirement as supported by the cited retained evidence |
| Insufficient evidence | Preserve local assessments; record the missing fact and, when actionable, a targeted lead |
| Potential contradiction | Preserve local assessments; record the conflicting evidence and a targeted investigation lead |

Rejecting a combined compatibility conclusion does not invalidate correct local
assessments. Neither a contradiction nor a missing fact automatically becomes a
published finding. The negotiator may route the lead to an existing capable
specialist or a bounded cross-component specialist with authorized access to
both sides. Findings still use normal candidate admission, critic, and adjudication.

The negotiator weighs concrete gaps against risk, other pending work, and remaining
budget. It may leave a gap explicitly unresolved. Inconclusive evaluation never
automatically launches a specialist. A concrete suspected mismatch has stronger
follow-up priority than a generic evidence gap, but neither grants extra budget.
Supported means the supplied evidence supports the stated compatibility question,
not that the whole integration has been proven correct.

Run the evaluator when usable assessments for the required sides are available,
normally after their local checks are covered. Do not require entire components
to finish. A concrete contradiction can trigger investigation earlier, without
waiting for both sides to claim coverage. If a side never supplies sufficient
evidence, the combined requirement remains incomplete.

Do not call the evaluator after every update. Evaluate once per ready packet at
a scheduling point and reconsider only after material new evidence or changed
relevant assessments. Metadata-only changes do not trigger another comparison.
If new evidence undermines an earlier supported result, mark it for reevaluation
rather than retaining a stale completion. Repeated missing-evidence results must
not spawn duplicate leads or unlimited evaluation loops.

For contract-only changes, the configured investigation owner may gather both
sides, including unchanged consumers, or delegate bounded work. The evaluator
needs evidence for the required sides, not necessarily two different specialists.
It also supports boundaries with more than two required participants; unrelated
configured participants do not automatically create all-pairs comparisons.

Shared schema reads are acceptable. Two full producer-to-consumer investigations
are not the default. Retained assessments are not unquestionable truth, and source
content remains untrusted. Cross-component reads remain subject to existing
authorization; investigation ownership does not grant additional access.

## 7. Bounded specialist delegation

After limited orientation, a component owner can propose delegation of an
independent question or changed-path subset. Delegation is optional, not a required
planning phase and not an excuse to postpone ordinary investigation.

Only a proper subset of remaining review work may be delegated. The parent must
retain a substantive investigation, not just coordination or final reporting.
Requests that transfer the whole remainder are rejected; existing compaction and
recovery handle context problems instead. The prompt asks for substantial,
separable work, not tiny questions the parent could answer with a few reads.

The proposal contains:

- A concrete review question and why separate investigation helps.
- The changed paths or existing requirement being delegated.
- Necessary reference paths and selected retained evidence IDs.
- Relevant observations and a concise expected result.

The controller supplies canonical handles and derives the available lease. The
model does not need to reproduce the whole obligation graph or calculate budgets.

The controller validates scope, existing ownership, duplicate requests, session
capacity, and budget. It then either transfers work to a queued/running child,
routes a cross-owner lead to its existing owner, or declines with a reason. Matching
is based on known ownership/targets, not a claim to understand semantic duplicates.
These are deterministic feasibility checks, including whether review work remains
with the parent. They do not attempt to score semantic benefit or enforce an
arbitrary minimum file count. No separate model approval is required.

Use the existing lead infrastructure with a distinct delegation kind and ownership
metadata. An ordinary investigation lead never transfers work. Accepted delegations
participate in priority-based scheduling alongside other open points; acceptance
does not guarantee execution. Do not introduce a separate delegation scheduler.

Ownership transfer is explicit and atomic: until acceptance, the parent owns the
work; after acceptance, it must not count that scope as locally completed. The
parent can continue other work. If accepted work cannot run, it stays visibly
pending or is explicitly returned to the parent.

Only one delegation level initially. Children cannot create grandchildren. They
can report leads through the existing mechanism. Shared evidence can still be
read by several specialists; exclusive ownership applies to review work, not files
as read resources.

### Backend-only example

The backend owner starts with all changed Java behavior. It discovers:

- A connected synchronization path spanning scheduling, cursor persistence, and
  vector publication: keeps this together.
- An independent cast/person mapping change: proposes a child for that subset.
- Recommendation fallback that depends on synchronization: retains this rather
  than splitting merely because it lives in another directory.

No controller parser had to recognize these functional groups beforehand. The
model discovered them while doing useful orientation, not after reviewing all files.

## 8. Scheduling and budgets

All primary, child, cross-component evaluation, and follow-up work uses the
existing global budgets and deadline; specialist sessions also obey session
limits. Evaluation calls are charged and scheduled before the review's final
cutoff, not added as free work afterward. Accepting delegation allocates from the
remaining review budget; it does not reset the parent's limits or multiply
allowances. Checkpoint and
finalization reserves remain protected.

Reuse the controller's scheduling and negotiator machinery. Do not add a second
LLM that must approve every delegation. The controller can admit a feasible split
deterministically; the negotiator prioritizes competing work at scheduling points.

There is no fixed number of scheduling rounds. Continue while useful work and
time/turn/tool/session budgets permit, preserving finalization reserves. Queued
delegations never selected remain visibly unreviewed. The evaluator reuses existing
model/context/output configuration and structured-response recovery behavior;
there are no separate evaluator overrides or special one-repair limit initially.

Children receive the focused assignment, relevant diff/context, applicable policy,
and bounded retained source evidence. They do not inherit the full conversation.
Their reads may legitimately overlap for context; delegation must not guarantee
zero duplicate calls.

Results and candidate submissions go directly to central state. The parent gets
a compact result with controller-backed references and coverage limits. It does
not re-submit or paraphrase child candidates to make them count.

## 9. Coverage, checkpoints, and failure handling

Avoid replacing 139 flat obligations with a giant nested checklist the model must
reprint. Checkpoints remain incremental: working summary, completed work, concrete
coverage changes, unresolved questions, and continuation notes when appropriate.

Use group-level assessments with explicit omissions. The aim remains to review
the assigned scope, but no specialist must emit a separate disposition or proof
record for every changed file. The changed-file inventory anchors which paths a
group assessment includes; everything not assessed remains visibly incomplete
by default. Do not recreate per-file obligations inside nested checkpoint arrays.
Identify assessed scope by explicit paths grouped into each assessment, not an
implicit claim about the whole assignment. Both unassessed paths and stated
behavioral gaps within assessed files remain schedulable for risk-based follow-up.

- Tool reads can record that a path was viewed, not that its behavior was verified.
- Specialists may assess a named path group in one update, with retained evidence
  and stated limits. No evidence citation automatically covers the whole component.
- Controller state distinguishes assessed, partial, and unassessed scope. A
  finished session is not synonymous with complete coverage.
- Delegated scope remains incomplete until the child's accepted assessment, and
  is not counted twice in component totals.
- Explicit independent checks keep independent completion requirements.
- Local coverage does not imply cross-component coverage. A combined requirement
  needs an accepted evaluator result tied to the relevant retained evidence and
  assessments. Evaluator failure or budget exhaustion leaves it incomplete and
  preserves local results; a coverage gap alone is not a defect finding.
- Candidate proof validation remains unchanged; grouped coverage is not proof of
  a defect or a substitute for candidate evidence.

The scheduler/negotiator can prioritize incomplete areas when their risk warrants
more investigation and budget remains. The critic evaluates candidate findings,
not coverage scheduling. Unscheduled gaps remain incomplete; there is no automatic
retry for every omitted area and no requirement to abandon accepted review results.

Timeouts and quarantines retain accepted candidates, leads, evidence, and valid
checkpoints. Unfinished ownership can be returned or reassigned within remaining
budgets. A missing child result never implies success. No automatic unlimited
respawn loop is introduced.

Private continuation notes remain separate from schedulable leads and human
handoff content. Broad incompleteness belongs in coverage reporting, not a flood
of finding-shaped verification requests.

## 10. End-to-end flow

```text
Immutable diff + component/contract policy
                  |
                  v
Deterministic ownership, requirements, hints
                  |
                  v
Controller schedules affected component owners
                  |
          +-------+--------------------+
          |                            |
          v                            v
  Direct investigation       Optional bounded delegation proposal
          |                            |
          |                   Controller validates and allocates
          |                            |
          |                     Focused child investigation
          |                            |
          +------------+---------------+
                       v
       Shared evidence, candidates, coverage, leads
                       |
                       v
        Boundary ready? -- no --> Remains incomplete if required;
                       |          other work can still proceed
                      yes
                       v
        Relevant assessments + retained source excerpts
                       |
                       v
        Bounded cross-component evaluator (LLM)
          |                            |
          v                            v
   Supported boundary         Gap / potential contradiction
          |                            |
          |                     Targeted lead
          +------------+---------------+
                       v
         Negotiator selects useful remaining work
          |                            |
          +--> continue/assign --------+  (within budget;
                       |                  returns to shared state)
                       |
                       v
        Existing critic, adjudication, remediation,
                 and human handoff
```

## 11. Reporting and evaluation

Action artifacts should show initial owners, delegated work, ownership changes,
remaining scope, actual versus planned sessions, and per-investigation cost.
Include evaluator outcomes, input evidence/assessment versions, limitations,
follow-up leads, and cost. Record why an activated boundary was not evaluated.
The human handoff remains a concise behavioral summary, not this internal graph.

Compare against the same PR and model configuration, recording:

- Valid defect recall/precision, including planted regression cases.
- Unassessed changed scope and substantive unresolved requirements.
- Repeated reads and overlapping investigations, checkpoint/repair cost, and time.
- Whether delegation adds value or merely shifts work and duplicates orientation.

Fewer reported obligations alone is not a success metric. Validate component-only,
contract-only, deployment-only, and cross-component changes, including multiple
Java components. Test ownership overlap/unmatched files, rejected delegation,
budget exhaustion, child failure, and incomplete shared-boundary reconciliation.
Include schema-valid but semantically incompatible IDs, missing/truncated source
evidence, compaction before comparison, and material updates after a supported
result. Confirm that local coverage cannot silently complete a combined check and
that repeated evaluation does not duplicate leads.
Verify that unassessed scope stays incomplete without mandatory per-file updates,
that all-remaining-work delegation is rejected, and that unmatched files enter the
remainder assignment. Test actionable migration errors before model calls, explicit
independent-check preservation, and inconclusive evaluation without automatic spawn.

## 12. Delivery boundaries

First implement component ownership and requirement simplification with retained
local assessments and bounded cross-component evaluation, without child
delegation. Validate that scope is preserved, combined checks are not automatically
covered, and policy exceptions remain explicit. Then add one-level delegation
using the same assignment and result lifecycle.

Document the required policy migration with movieHRdb examples and an agent-facing
handoff: old property/meaning, replacement, recommendation, and reason. Explain
ordinary versus independent recipes, component ownership, shared contracts, and
the unmatched-file fallback. Reject unmigrated policy rather than maintaining a
legacy execution path. Do not require functional-package configuration, introduce
new language parsers, or change finding adjudication as part of this work.
Include a complete, parser-tested quick-start policy and clear missing-policy
errors so users can deliberately adopt the action without extensive configuration.

This document requests design approval only. Implementation details and the exact
configuration schema should be finalized against the existing policy loader and
assignment interfaces after that approval.
