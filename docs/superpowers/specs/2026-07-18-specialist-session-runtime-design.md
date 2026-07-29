# Specialist Session Runtime Design

**Date:** 2026-07-18  
**Status:** Approved design  
**Scope:** Replacement of the existing specialist-review orchestration core

## 1. Purpose

Replace the evolved specialist runner with a first-principles session runtime for
large, multilingual repositories. The replacement keeps the project's hardened
transport, tool, evidence-safety, parsing, enforcement, and GitHub publishing
components, but gives specialist reviews an explicit lifecycle, durable state,
global budget accounting, deterministic coverage requirements, and controlled
model-led planning.

The design optimizes primarily for evidence-backed review quality. Runtime and
request counts remain bounded by a configurable review-level deadline, which is
120 minutes by default.

## 2. Product boundary

The new orchestration core supports:

- GitHub pull requests.
- OpenAI-compatible model endpoints, with LM Studio as the reference runtime.
- Optional model overrides for planner, specialist, negotiator, critic, and
  finalizer roles.
- Configurable sequential or concurrent specialist execution.
- Configurable GitHub publishing modes: sticky comment, review with inline
  comments, and guarded native approve/request-changes verdicts.

Anthropic-specific orchestration and Forgejo-specific orchestration are outside
the acceptance boundary for the new core. Existing generic adapters may remain
where they do not constrain the new design, but the new runtime is not required
to preserve those behaviors.

The implementation will replace the current specialist path directly on a
development branch. It does not need to operate a production dual path or shadow
mode. Recorded traces, fixtures, and PR replays remain comparison oracles for
merge readiness.

## 3. Design principles

1. **One logical specialist owns one durable session.** Coverage feedback resumes
   the existing session. A new conversation is created only for an explicit
   recovery condition.
2. **Models organize coverage; they do not define completeness.** Mandatory
   obligations are deterministic and auditable.
3. **Budgets belong to runs and sessions, not API invocations.** Retries,
   checkpoints, follow-ups, and recovery never reset accounting.
4. **Evidence is separate from model prose.** Findings and coverage decisions
   resolve to evidence records with provenance.
5. **Recovery removes polluted context without losing state.** The structured
   ledger and latest valid checkpoint can reconstruct a conversation.
6. **Unknown is not defective.** Missing evidence becomes an explicit unknown;
   only configured high-risk incompleteness directly blocks approval.
7. **The current branch defines repository policy.** Manual review triggering is
   the human authorization boundary for changed policy and source access.
8. **Security remains deterministic.** Models cannot widen source access, tool
   access, budgets, verdict policy, or mandatory coverage.
9. **Publishing separates handoff from evidence.** The sticky comment helps a
   human plan the review; resolvable finding threads contain detailed claims and
   evidence.

## 4. Top-level architecture

```text
Repository + immutable PR snapshot
        |
        v
Topology analyzers + current-branch repository policy
        |
        v
Deterministic coverage obligations
        |
        v
Assignment planner model
        |
        v
Deterministic assignment validator
        |
        v
Session scheduler
   +----+-----------+
   |    |           |
   v    v           v
Specialist sessions (configurable concurrency)
   +----+-----------+
        |
        v
Deterministic coverage reconciliation
        |
        v
Negotiator proposals for resumes, consultations, or follow-ups
        |
        v
Validated follow-up sessions
        |
        v
Critic/finalizer model
        |
        v
Deterministic verdict and publishing policy
```

### 4.1 Reused infrastructure

The replacement should reuse, adapting interfaces where necessary:

- OpenAI-compatible request transport and SSE reassembly.
- Conversation wire rendering and native tool-call normalization.
- Read-only file, Git, GitHub API, web, and named-command executors.
- Path, host, repository, response-size, timeout, and secret guards.
- Response parsing and schema validation.
- Finding normalization, completeness enforcement, and verdict guardrails.
- Markdown sanitation, inline-comment anchoring, metadata, and GitHub publishing.

### 4.2 New core components

#### `ReviewController`

Owns phase transitions, the absolute run deadline, cancellation, degradation,
terminal state, and the authoritative run artifact.

#### `CoverageEngine`

Derives mandatory coverage obligations from topology, changed behavior, risk,
repository policy, and PR intent. Evaluates obligation satisfaction against the
evidence and coverage ledgers.

#### `AssignmentPlanner`

Groups the immutable obligation list into coherent specialist focuses. It proposes
ownership, distinct lenses, scope, evidence expectations, effort, and justified
redundancy.

#### `AssignmentValidator`

Rejects omitted obligations, vague duplicate focuses, unjustified overlap,
infeasible schedules, scope violations, and missing required redundancy. It can
mechanically merge exact overlap or request one bounded planner repair.

#### `SessionScheduler`

Runs specialist waves with configurable concurrency, priority ordering, immutable
wave-start ledger snapshots, deterministic result merging, leases, and deadline
aware cancellation.

#### `SpecialistSession`

Owns one compactable transcript, structured checkpoint, hypotheses, candidate
findings, evidence references, assigned obligations, unknowns, and lifetime budget
ledger.

#### `CoverageReconciler`

Classifies obligations as covered, attempted but unresolved, never covered, or
contradicted. It relies on satisfaction predicates and evidence, not a model's
declared completion status.

#### `Negotiator`

Proposes the least costly useful next action for uncovered obligations: resume an
owner, request a consultation, create a narrow follow-up, or record an externally
unverifiable unknown. The controller validates every proposal.

#### `CriticFinalizer`

Adjudicates evidence-backed candidate findings, resolves semantic duplication and
contradictions, and renders a concise human review from accepted candidates,
coverage, unknowns, policy, and PR intent.

#### `VerdictPolicy`

Applies deterministic severity, incomplete-high-risk-coverage, approval, native
review, and publishing guardrails after model judgment.

## 5. Core state and invariants

### 5.1 `ReviewRun`

The run contains:

- Repository, PR number, base SHA, and head SHA.
- Immutable PR metadata, diff, file list, and collected local context.
- Validated runtime configuration and current-branch policy snapshot.
- Policy and configuration hashes.
- Absolute run deadline and phase deadlines.
- Topology, obligations, assignments, session registry, and scheduler state.
- Global evidence store and coverage ledger.
- Candidate findings, critic dispositions, unknowns, and access requests.
- Global budget totals and final publishing result.

The run is append-only at the event level. Current projections may be updated, but
every material transition remains reconstructable from the artifact.

### 5.2 `BudgetLedger`

Budgets are direct lifetime quantities rather than the legacy `2 x rounds`
mapping. At minimum, the run and each session track:

- Model turns attempted and completed.
- Executed, rejected, malformed, failed, replayed, and duplicate tool calls.
- Input and output tokens when the provider reports them.
- Wall-clock time.
- Consecutive no-progress events.
- Checkpoint repairs.
- Conversation reconstructions.
- Remaining run-deadline share.

Retries, follow-ups, reconstruction, and reassignment continue against the same
ledger. No helper may instantiate a fresh effective budget for an existing logical
session.

### 5.3 `EvidenceStore`

Evidence is content-addressed using a canonical request identity, source
provenance, and bounded content hash. A record includes:

- Evidence ID and evidence category.
- Collector session and model identity.
- Tool and canonical arguments.
- Source location, host, URL, repository path, or API endpoint.
- Retrieval time and freshness policy.
- Result status, bounded content, content hash, MIME type where applicable,
  truncation status, and redaction status.
- Imported-by session IDs.
- Superseded or contradictory evidence links.

Successful duplicate requests reuse the existing record unless freshness policy
requires revalidation. Reuse does not create a second independent verification.

### 5.4 `CoverageLedger`

The ledger connects obligations to supporting and contradicting evidence,
responsible sessions, checkpoint claims, satisfaction decisions, unresolved
reasons, and policy consequences. Completion order must not affect the resulting
projection.

## 6. Repository topology and policy

### 6.1 Generic analyzers

Built-in analyzers derive components, relationships, file roles, changed
behaviors, and risk signals from, where applicable:

- Language and package structure.
- Build, dependency, workspace, and module files.
- API and messaging schemas.
- Database schemas and migrations.
- Deployment manifests, workflows, and infrastructure configuration.
- Generated-artifact relationships.
- Tests, callers, consumers, and repository history.

Analyzers produce explainable facts and candidate obligations. They do not silently
assert repository-specific conventions.

### 6.2 Current-branch repository policy

A schema-versioned policy file on the reviewed head/current branch may:

- Define or refine component boundaries and relationships.
- Declare generated artifacts and authoritative inputs.
- Add, override, or suppress coverage rules with a recorded reason.
- Configure risk tiers and unresolved-coverage verdict consequences.
- Require independent verification for selected obligations.
- Define source-access rules.
- Configure approval and publishing policy.

Existing version-1 `components` remain authoritative topology knowledge. Matching
version-1 `recipes` are migrated into named mandatory coverage obligations rather
than treated as advisory candidate specialists. A recipe's identity and terminal
coverage status remain visible even when the assignment planner groups its work
into a more specialized model-created session.

Each recipe may declare an execution policy:

- `coverage` (default): its obligations are mandatory, but the planner may group
  them into a coherent specialist assignment.
- `dedicated`: its obligations require a distinct named specialist session.
- `independent`: its obligations require a separate assessment whose initial
  checkpoint is not exposed to another specialist's conclusions.

The manual trigger is the human authorization boundary. A changed policy is
effective for that same review. The run must prominently disclose a policy change,
record its diff or a bounded summary, and record the exact head SHA and policy hash
used.

Invalid security-sensitive rules fail closed. Optional invalid topology hints may
be ignored only when ignoring them cannot broaden access or weaken a mandatory
policy, and the degradation is recorded.

## 7. Coverage obligations

A coverage obligation has:

- Stable ID.
- Origin: analyzer, risk rule, classifier, PR requirement, or repository policy.
- Subject: behavior, component, boundary, contract, or artifact.
- Required evidence categories.
- Deterministic satisfaction predicates.
- Risk tier.
- Policy for unresolved status.
- Redundancy requirement.
- Scope and seed hints.
- Explanation suitable for the artifact.
- Repository recipe ID when the obligation originates from a configured recipe.

Example obligations include:

- Trace a changed API field through one producer and one consumer.
- Inspect a relevant test for modified behavior.
- Verify error identity and propagation across a changed boundary.
- Verify a configured model capability against an approved primary source.
- Confirm deployment consumes the artifact produced by the changed revision.

Models cannot remove, weaken, or mark mandatory obligations satisfied. A model may
challenge applicability with evidence; the deterministic engine applies the
configured applicability predicate and records the decision.

Every configured recipe receives an explicit lifecycle status; `assigned` is an
intermediate state and all other applicable states are terminal:

- `not_applicable`
- `assigned`
- `covered`
- `partially_covered`
- `unresolved`
- `suppressed_by_policy`

The artifact records the specialist assignment that owned each applicable recipe,
including cases where a model-created focus was more specialized than the recipe.

## 8. Assignment planning and overlap

The planner receives the complete immutable obligation list, topology, risk, PR
intent, configuration, and available budget. It outputs a minimal coherent roster
whose assignments declare:

- Primary obligation ownership.
- Independent-verification ownership where required.
- Objective and distinct analytical lens.
- Seed paths and permitted boundary areas.
- Expected evidence categories.
- Estimated effort and priority.
- Justification for overlapping files, paths, components, or obligations.

Overlap is risk-tiered:

- Every obligation has one accountable primary owner.
- Multiple specialists may inspect the same code for genuinely orthogonal
  obligations.
- High-risk policy may require two separately collected assessments.
- Independent assessments begin without seeing each other's conclusions.
- The artifact discloses model identity; two runs of the same model are treated as
  correlated rather than fully independent.
- Unjustified broad duplication is rejected.

The validator ensures set coverage, required redundancy, distinct focus, feasible
effort, and ownership. A deterministic fallback groups obligations by component and
boundary if planning fails.

## 9. Specialist session lifecycle

```text
CREATED
   |
   v
EXPLORING <-------------------+
   |                          |
   v                          |
CHECKPOINT                    |
   |                          |
   v                          |
COVERAGE_EVALUATION           |
   |-- useful gaps -----------+
   |-- polluted transcript --> RECOVERY --> EXPLORING
   |-- externally blocked ---> RECORDED_UNKNOWN
   `-- sufficiently covered -> FINALIZING --> COMPLETE
```

### 9.1 Exploration

The specialist receives its assignment, immutable context, relevant shared-evidence
index, tool schemas, scope, and current lifetime budgets. It uses native tool calls
autonomously within deterministic path, host, repository, response, repetition,
scope, and budget controls.

### 9.2 Checkpoints

A checkpoint is not a final report. It records:

- Evidence gathered and imported.
- Hypotheses supported, contradicted, or unresolved.
- Candidate findings and evidence IDs.
- Assigned-obligation status.
- Invariants evaluated.
- Unknowns.
- Proposed next actions.

The controller can request a checkpoint when the model stops, reaches a no-progress
guard, crosses an evidence threshold, or approaches a phase deadline. A valid
checkpoint is added to the same transcript and projected into the structured
ledger.

### 9.3 Coverage feedback and resume

The deterministic engine evaluates the checkpoint and evidence. If useful gaps
remain, the controller appends targeted gap feedback to the same conversation and
resumes exploration. The transcript, evidence, deduplication keys, and lifetime
budgets remain intact.

A no-progress stop does not force finalization. It requests a checkpoint and gives
the controller an opportunity to redirect the same session. The no-progress streak
may reset after materially new controller feedback, while lifetime no-progress
telemetry and all other counters remain.

### 9.4 Recovery

Recovery is reserved for:

- Repetitive or polluted reasoning.
- Irreducible context pressure after safe compaction.
- Invalid provider conversation history.
- Transport incompatibility that prevents continuing the transcript.

Recovery creates a clean conversation from the immutable assignment, latest valid
checkpoint, structured ledger, complete bounded evidence set, current gaps, and
remaining lifetime budgets. The abandoned transcript remains available in bounded
diagnostics. Recovery never resets calls, turns, time, deduplication identity, or
evidence state.

### 9.5 Finalization

Finalization occurs once, when required coverage is sufficiently satisfied,
further investigation has no justified expected value, or the exploration deadline
requires termination. The final specialist report is projected from the latest
checkpoint, evidence, and final model response. Invalid final output receives one
bounded schema repair, then degrades to the controller's structured projection.

## 10. Scheduling and deadlines

The configurable review-level deadline defaults to 120 minutes. Default phase
allocations are:

- 10% topology, obligations, and assignment planning.
- 60% initial specialist investigation.
- 20% reconciliation and targeted follow-ups.
- 10% finalization and GitHub publishing.

Unused time flows forward. Exploration cannot consume the finalization reserve.
Phase shares may be configured, but must sum to 100% and must retain a positive
finalization reserve.

Specialist concurrency is configurable and defaults to one. Sessions in a
concurrent wave receive the same immutable wave-start ledger projection. Their
events and evidence merge by stable IDs, and reconciliation occurs after the wave,
so completion order does not affect coverage or findings.

High-risk mandatory work is scheduled before lower-risk or optional investigation.
Every session receives a lease bounded by the absolute run deadline and its phase.
Every outbound request timeout is capped by the remaining phase lease.

At the exploration cutoff:

- Pending tools and sessions are not started.
- In-flight work receives only a bounded grace period.
- Sessions emit or reconstruct their latest checkpoint.
- Unresolved obligations move to risk-based verdict policy.

## 11. Negotiation and follow-ups

After each initial or follow-up wave, the negotiator receives structured
checkpoints, coverage state, evidence provenance, current assignments, and remaining
budgets. It does not receive full coworker reasoning transcripts.

It may propose:

- Resume an existing owner with targeted coverage feedback.
- Ask an existing specialist for a bounded consultation.
- Create a narrow follow-up specialist for uncovered obligations.
- Record an externally unverifiable unknown.

The proposal includes expected new evidence, obligation IDs, estimated effort, and
why existing work is insufficient. The controller rejects proposals that repeat
completed checks, lack expected coverage gain, exceed scope or deadline, modify
policy, or grant new budget. Mandatory obligations cannot be deleted.

If negotiation fails, the deterministic fallback resumes the primary owner for the
highest-risk uncovered obligation when feasible, otherwise records policy-governed
unknowns.

## 12. Evidence sharing and independent verification

Specialists receive a compact index of potentially relevant shared evidence rather
than every coworker transcript. They may import a record, preserving the original
collector and provenance.

For deliberate independent verification, policy declares whether the second
specialist may reuse raw evidence. It never sees the first specialist's hypotheses,
candidate findings, or conclusions before completing its independent checkpoint.

Evidence reuse reduces duplicate I/O but does not count as independently gathered
confirmation. A second fetch or read counts as independent only when policy requires
fresh collection and the executor actually performs it.

## 13. Web discovery and approved external evidence

### 13.1 Search provider

Web search uses a fixed operator-configured `SearchProvider`; SearXNG is the
reference implementation. The model supplies only a bounded, redacted query. It
cannot choose the search endpoint.

The query guard:

- Applies secret redaction and maximum length.
- Rejects detected credentials and unsafe high-entropy payloads.
- Records a redacted query in provenance.
- Never attaches repository credentials or cookies.

### 13.2 Discovery versus evidence

Search is discovery, not evidence. The provider may scan a bounded candidate count,
for example 20-30, independently of the smaller result count returned to the model.
Candidates are classified against the current-branch source policy before being
returned.

Approved candidates may expose bounded title, URL, host, path, and source
classification. Unapproved candidates expose only minimal non-evidentiary metadata:
host, URL, an optional sanitized title, denial reason, and suppressed-result count.
Search snippets or summaries from unapproved pages are never returned to the model.

No search result supports a published claim until an approved `web_fetch` retrieves
and normalizes the source.

### 13.3 Source-access requests

A specialist may record a structured future-access request containing:

- Host and candidate URL.
- Purpose.
- Related coverage obligation.
- Reason the source appears authoritative.

The request does not modify policy during the run. It is shown in the run artifact
and step summary. The current obligation remains externally unverifiable unless
other approved evidence satisfies it.

### 13.4 Source policy

Source rules live in the current-branch repository policy and support:

- Exact host.
- Explicit subdomain inclusion.
- Optional path prefixes.
- Source classification such as official documentation, release metadata, or
  advisory.
- Optional freshness rules.

Rules cannot use a global wildcard, unsafe scheme, arbitrary port, public-suffix
wildcard, or private-network target.

### 13.5 Secure fetch

`web_fetch` enforces:

- HTTPS and approved ports.
- Host and path policy on the initial URL and every redirect.
- DNS/IP rejection for private, loopback, link-local, multicast, reserved, and
  metadata-service destinations.
- Response timeout, redirect count, body size, and MIME-type limits.
- No cookies, ambient credentials, or repository tokens.
- HTML-to-text normalization, secret redaction, and untrusted-content fencing.
- Final URL, retrieval time, content hash, MIME type, truncation, and policy hash
  in evidence provenance.

## 14. Findings, criticism, and final verdict

A candidate finding contains:

- Candidate ID and root-cause fingerprint.
- Concrete claim and affected location/behavior.
- Causal chain.
- Proposed severity and category.
- Supporting and contradicting evidence IDs.
- Related obligation IDs.
- Collector session and model identity.
- Confidence rationale.

Deterministic fingerprints group exact and near-exact duplicates. The critic handles
semantic decisions: keep, reject, merge consequences under one root cause, request
narrow verification, or downgrade to an unknown. It cannot create a publishable
finding without existing evidence.

The finalizer receives adjudicated candidates, coverage state, material unknowns,
repository policy, and PR intent. It does not receive every raw transcript. It
produces two separate structured products rather than one markdown blob:

- `ReviewHandoff`, optimized for the human reviewer's task planning.
- `ReviewNote[]`, optimized for evidence, discussion, and resolution. Note
  kinds include `finding`, `verification_request`, and `source_access_request`.

The finalizer also recommends a verdict.

Deterministic policy then enforces:

- Supported blocker or major findings affect verdict according to configured
  severity policy.
- Unresolved mandatory obligations block only when their configured high-risk
  policy requires it.
- Lower-risk gaps are explicit unknowns, not invented findings.
- Approval remains disabled unless repository policy permits it.
- Every published factual finding resolves to retained evidence provenance.
- Native approve/request-changes behavior obeys existing GitHub safety guards.

Rejected candidates and critic reasoning remain in the private artifact. Public
output stays concise.

## 15. Publishing

One GitHub publisher interface supports:

- `comment`: managed sticky handoff only, with links to retained run artifacts.
- `review_comment`: managed sticky handoff plus resolvable line/file finding
  threads submitted as a non-verdict review.
- `review_verdict`: the same handoff and finding threads plus guarded native
  approve/request-changes state.

`review_comment` is the default specialist publishing mode because it preserves
the handoff/detail separation without allowing the action to approve or block a
merge. Selecting `comment` is an explicit reduced-detail mode; it does not place
finding evidence back into the sticky handoff.

### 15.1 Sticky human-review handoff

The managed sticky comment is intentionally concise and must not duplicate the
detailed review notes. It contains:

- Recommendation and review status.
- A short change map describing what behavior and components the human should
  expect to have changed.
- What the AI reviewed, including selected specialist focuses, contributed
  repository recipes, and major coverage boundaries.
- An optional single-line open-thread status, such as the number of unresolved
  notes and highest material severity. It never lists every finding.
- An optional aggregate finding theme only when multiple notes genuinely share a
  useful pattern, such as persistence boundaries or authorization. Disparate
  findings do not receive an artificial theme.
- At most a few high-level human-review emphasis areas derived from risk,
  cross-component consequences, contradiction, or weak verification. Detailed
  claims remain in their threads.
- An optional compact coverage warning when degraded sessions or missing evidence
  materially limit confidence, linked to diagnostics rather than expanded inline.
- An optional count/link for open external-source requests; host, purpose, and
  related evidence requirements remain in their dedicated notes.

Sections and fields with no clear orientation value are omitted. The handoff must
not include a finding-by-finding index, unknown-by-unknown list, or detailed
recovery telemetry.

The handoff must explicitly state that focus suggestions do not reduce the human's
responsibility to review the complete change. It should help divide attention, not
claim that unlisted areas are safe.

Human-focus suggestions are derived from structured coverage, risk, evidence, and
session state. They are not a restatement of every finding, unknown, or request and
are not generated from unsupported model confidence.

### 15.2 Detailed review notes

In `review_comment` and `review_verdict`, each accepted finding and each specific
manual-verification or source-access request is published as its own managed GitHub
review note. Finding notes contain:

- Concise claim and severity.
- Exact changed causal file and line when available.
- User-visible consequence and causal chain.
- Supporting and contradicting evidence.
- External source citations where used.
- Suggested manual verification or fix validation.
- Stable root-cause fingerprint and managed metadata.

Verification and source-access notes contain their precise question, related
coverage obligation, evidence already checked, reason human input or new source
access is needed, and stable managed metadata. They do not pretend that an unknown
is a defect.

Anchor selection follows this order:

1. A defensible changed diff line becomes a `LINE` review thread.
2. A note tied to a changed file but not one defensible line becomes a `FILE`
   review thread.
3. A finding with no honest changed causal file is not published as an actionable
   defect; it becomes a verification request instead.
4. A verification or source-access request with no honest changed-file anchor is
   published as a dedicated managed general PR comment. GitHub cannot mark a
   general PR comment resolved, so this fallback and its limitation are recorded.

Both line- and file-level review threads are resolvable on GitHub. Re-review uses
the stable note fingerprint to locate the existing thread or managed comment. A
still-open note receives a reply with current evidence. A fixed finding or answered
request receives a resolution reply and is resolved through GitHub's review-thread
API when permissions allow. Human-resolved threads are not silently reopened;
contradictory new evidence creates an explicit reply or a new finding according to
repository policy.

The sticky handoff links to finding threads but never embeds their detailed
evidence.

### 15.3 Publisher inputs and failure behavior

Publishing consumes only the final policy result, `ReviewHandoff`, and normalized
`ReviewNote[]`. It does not inspect raw model transcripts. Existing sanitation,
reserved-marker stripping, line anchoring, prior-review cleanup, and
finding-thread resolution should be reused and extended for file-level GraphQL
review threads.

Publishing failure is recorded separately from review completion and follows a
bounded retry policy without rerunning analysis.

## 16. Failure and degradation behavior

- **Transient request or SSE failure:** bounded retry of the same logical turn.
- **Malformed native tool arguments:** structured error result in the same session.
- **Duplicate/no-progress behavior:** checkpoint followed by targeted controller
  feedback; recovery only if the transcript becomes polluted.
- **Repetitive transcript:** reconstruction from checkpoint, ledger, evidence, and
  remaining lifetime budgets.
- **Invalid checkpoint:** one schema-repair turn, then deterministic projection of
  known session state.
- **Specialist failure:** reassign only uncovered mandatory obligations when the
  remaining deadline makes useful work feasible.
- **Planner or negotiator failure:** deterministic component/boundary fallback.
- **Denied or unavailable source:** provenance-backed unknown and optional access
  request.
- **Critic failure:** conservative deterministic handling of normalized candidates;
  ambiguous candidates are not published as defects.
- **Finalizer failure:** deterministic minimal review from accepted candidates,
  coverage, and unknowns.
- **Deadline exhaustion:** preserve finalization reserve, stop exploration, apply
  risk policy, publish a controlled result, and record degraded confidence.

Every degradation that materially affects confidence appears in the artifact and
public review. No recovery path broadens permissions or resets budgets.

## 17. Configuration model

### 17.1 Runtime/action configuration

Runtime configuration controls:

- OpenAI-compatible endpoint and credentials.
- Default model and optional role-specific models.
- Streaming and provider request parameters.
- Review deadline and phase shares.
- Specialist concurrency.
- Direct run/session call, turn, token, response, and recovery limits.
- Search provider endpoint.
- Diagnostic retention.
- GitHub publishing mode and credentials.

### 17.2 Repository policy

The current-branch policy controls:

- Topology hints and component relationships.
- Coverage additions, overrides, and documented suppressions.
- Generated-artifact conventions.
- Risk, severity, redundancy, incomplete-coverage, and approval policy.
- External source allowlist.
- Publishing policy restrictions.

Configuration is validated before model execution. The effective normalized
configuration and policy hashes are recorded.

## 18. Artifact and observability

One machine-readable artifact is authoritative for:

- Run identity, PR/head SHA, effective configuration, and hashes.
- Policy change disclosure.
- Phase timeline and deadline allocation.
- Topology and obligations with derivation explanations.
- Planner assignments and validator repairs.
- Session/model identities and lifecycle transitions.
- Lifetime budget consumption.
- Evidence metadata, provenance, freshness, and truncation.
- Coverage decisions.
- Repository recipe applicability, assignment, and terminal coverage status.
- Candidate findings, contradictions, and critic dispositions.
- Unknowns and source-access requests.
- Recovery and degradation events.
- Rendered human-review handoff and review-note IDs/URLs/resolution state.
- Final verdict provenance and publishing result.

Raw secrets are never stored. Large evidence bodies and full transcripts may be
separate bounded diagnostic attachments with configurable retention, referenced by
ID from the primary artifact.

Runtime logs use structured lifecycle events. The step summary reports specialist
outcomes, obligation coverage, budgets, recoveries, denied sources, unknowns,
verdict provenance, and publishing status without requiring operators to reconstruct
state from repeated API request bodies.

## 19. Testing and acceptance

### 19.1 Test layers

1. Pure state-machine, projection, assignment-validation, coverage, and budget
   tests, including property tests for invariants.
2. Recorded OpenAI-compatible and LM Studio streaming, reasoning-channel,
   structured-output, and native-tool fixtures.
3. Security tests for repository paths, commands, redirects, DNS/IP targets,
   source policy, prompt injection, query leakage, response fencing, and redaction.
4. End-to-end scenarios for multilingual topology, concurrency, failure injection,
   recovery, deadline exhaustion, deterministic fallbacks, and all GitHub publishing
   modes.
5. Representative real-PR replay with human-reviewed expected obligations, material
   findings, acceptable unknowns, and runtime budgets.

### 19.2 Required invariants

Automated tests must prove:

- A logical session never resets its lifetime budget.
- Coverage feedback resumes the same conversation unless a recorded recovery reason
  applies.
- Recovery retains checkpoint, evidence, gaps, deduplication identity, and budgets.
- Mandatory obligations cannot disappear between planning and final policy.
- Concurrent completion order cannot change merged evidence, coverage, or accepted
  candidate identity.
- Unapproved pages are never fetched and their snippets are never exposed.
- Redirects cannot escape source policy or reach private network space.
- Published findings resolve to retained evidence.
- Every matching repository recipe contributes accounted obligations and remains
  traceable through assignment and terminal coverage status.
- The sticky comment contains a sparse, conditional human-review handoff without
  per-finding, per-unknown, or detailed-evidence duplication.
- In `review_comment` and `review_verdict`, accepted findings publish as
  individual resolvable line- or file-level threads; specific verification and
  source-access requests publish as separate notes, using managed general comments
  only when no honest changed-file anchor exists.
- Re-review updates and resolves managed finding threads without silently reopening
  human-resolved discussions.
- The finalization reserve cannot be consumed by exploration.
- A failed model role has a bounded deterministic degradation path.

### 19.3 Merge acceptance

The branch is mergeable when:

- All automated test layers pass.
- No unsupported claim is published in the replay set.
- Every mandatory obligation has an accounted terminal status.
- Risk-based incomplete-coverage policy behaves as configured.
- Runs respect the hard deadline and preserve finalization.
- Review quality is judged improved or at least equivalent on representative PRs.
- Security-sensitive behavior has explicit adversarial tests.
- Operators can explain every session restart, budget change, coverage decision,
  source access, finding disposition, and verdict from the artifact.

## 20. Downstream-project migration handoff

Implementation includes a concise agent-facing migration document at
`docs/migrations/specialist-session-runtime.md`. It is a required release
deliverable, not a post-release follow-up.

The handoff contains:

- A changelog of externally visible review-process behavior.
- A table of added, changed, retained, deprecated, and removed action properties,
  including the recommended value for large multilingual repositories and why.
- A before/after workflow example for GitHub and OpenAI-compatible endpoints.
- A repository migration checklist covering `.github/ai-review-rules.md`,
  `.github/ai-review-specialists.json`, the configured `system_prompt_file`, the
  current-branch source allowlist/policy, workflow permissions, action version pin,
  model-role overrides, concurrency, deadline, and publishing mode.
- Guidance for migrating version-1 components and recipes into coverage,
  `dedicated`, or `independent` execution policies.
- Guidance on writing concise repository prompt additions instead of copying the
  bundled system prompt.
- The security consequence of current-branch source rules and the expectation that
  a human inspects policy changes before manually triggering a review.
- Expected sticky-handoff, resolvable-review-note, artifact, and degraded-run
  behavior so project agents can update their own repository rules and tests.
- A troubleshooting section for missing recipes, uncovered obligations, denied web
  sources, non-resolvable notes, model incompatibility, deadline exhaustion, and
  publishing permissions.

README input tables and example workflows must link to this migration document.
The implementation plan and final release verification must check that the handoff
matches the actual input names, defaults, schemas, and behavior delivered by the
code.

## 21. Explicit non-goals

- Building a general-purpose agent workflow framework.
- Supporting arbitrary model-supplied shell commands.
- Automatically modifying source policy during a review.
- Treating search snippets as evidence.
- Requiring multiple model providers for independent verification.
- Preserving Anthropic- or Forgejo-specific specialist behavior in the new core.
- Publishing raw specialist reasoning or full diagnostic transcripts.
- Using the sticky comment as the detailed finding database.
- Inferring defects solely from missing evidence.
