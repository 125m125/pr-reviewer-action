# Large Review Workload and Evidence Design

## Context

The small movieHRdb review completed in about eleven minutes with complete
coverage, no degraded controller roles, and one correctly consolidated access
request. The large pr-reviewer-action dogfood review failed safely but required
346 specialist requests, 223 tool calls, and about 11.7 million prompt tokens.
It retained 195 evidence records while covering only 16 of 92 obligations. Two
specialists degraded, 74 obligations remained unresolved, and both deliberately
changed dogfood defects were missed.

The large run exposed five coupled causes:

1. Generic per-file implementation obligations duplicate test, documentation,
   recipe, and component coverage. Round-robin assignment then placed 23 and 42
   unrelated obligations into individual specialists.
2. Large planner projection omitted all changed paths and capability roles, so
   the optional planner recognized overloaded assignments but could not safely
   rebalance them.
3. Specialists read one file per model round and repeatedly compacted without
   making controller-visible progress. One specialist issued twenty checkpoint
   requests. Thirty-one compacted-evidence reads included twelve reads of the
   same evidence ID.
4. Structurally useful checkpoints were discarded when they added harmless
   controller-owned or legacy fields outside the strict schema.
5. Source redaction rewrote `api_token={api_token}` as `api_[REDACTED]`. The
   specialist consequently concluded that vulnerable source code masked its
   token. Rendering-time redaction must never masquerade as source behavior.

The smaller run also confirmed two publishing requirements: disabling automatic
approval must not turn a clean review into a request-changes recommendation, and
the handoff needs a factual `What the AI reviewed` section even when the model
summary is rejected. The latter overlaps the large-run handoff correction and is
one shared implementation track.

## Goals

- Preserve atomic controller-owned obligation accounting while presenting
  specialists with smaller coherent review families.
- Adapt from two to twelve focused specialists without multiplying the global
  review budget by the specialist count.
- Keep large-PR planner context useful and proportional to the changed surface.
- Reduce one-file-per-turn exploration through bounded multi-path diff reads.
- Make checkpoint and compacted-evidence recovery converge instead of loop.
- Preserve the valid core of near-miss checkpoints without trusting model-owned
  coverage metadata.
- Redact actual secrets without changing the observable semantics of source code.
- Publish accurate clean/degraded recommendations and a useful deterministic
  human handoff.

## Non-goals

- Atomic obligation IDs, immutable revisions, trust boundaries, or evidence
  provenance will not become model-controlled.
- Aggregation will not allow one conclusion to cover unrelated components,
  incompatible risk policies, or independent recipes.
- A larger specialist ceiling will not grant every specialist a full independent
  64-turn lease.
- Compacted-evidence retrieval will not become a general arbitrary evidence-read
  interface.
- Coverage incompleteness will not be converted into a code finding.

## 1. Atomic obligations and static review families

Atomic obligations remain the authoritative audit and policy units. Before
assignment, the controller deterministically projects compatible atomic
obligations into session-local review families. Specialists reason and interact
through short family handles while the controller retains every member ID.

A family key contains:

- risk tier and unresolved policy;
- component or active component boundary;
- recipe identity when present;
- evidence category;
- overlapping changed scope and evidence plan.

The controller never combines:

- independent or dedicated recipes;
- distinct critical risk rules;
- obligations with incompatible unresolved policies;
- unrelated components or component boundaries;
- producer and consumer responsibilities that require separate proof.

Generic topology generation stops creating a per-file implementation obligation
for paths already classified primarily as tests, documentation, plans, fixtures,
generated output, migrations, build manifests, or deployment configuration.
Those paths remain in the changed-file inventory and attach to their relevant
behavior/test/contract family. Ordinary production implementation obligations
within one component may form a bounded family.

Family size is limited by both semantic members and diff workload. Default
targets are no more than ten atomic obligations, eight changed paths, or roughly
1,500 changed lines / the equivalent configured diff-byte allowance. Exceeding
one threshold creates another deterministically ordered family. Line count is a
secondary cost signal; risk, component boundaries, and behavioral cohesion take
precedence.

Family-level disposition proposals fan out only after the controller validates
the evidence, conclusion, and eligibility for every atomic member. Partial
eligibility produces a partial family result and leaves unmatched members open.
Artifacts continue to expose atomic statuses and additionally record family
membership and family-level investigation history.

## 2. Adaptive specialists under a global lease

The controller derives a desired session count from isolated recipes plus the
static family workload:

- small reviews normally use two to six sessions;
- large cross-component reviews may use up to twelve;
- the user-configured hard session cap remains authoritative.

Changed lines never directly mean “one specialist per thousand lines.” The work
score combines diff bytes, family count, risk weight, active boundaries, changed
production symbols, and discounted test/documentation/generated volume.

All sessions share controller-owned global limits in addition to per-session
limits. New optional inputs `specialist_max_total_model_turns` and
`specialist_max_total_tool_calls` default to 320 and 640 respectively. This
preserves the existing recommended two-tool-calls-per-model-turn ratio: one
native model turn may emit multiple read-only calls, and tool availability is
the primary signal that evidence exploration can continue. The global limits
target a predictable review ceiling rather than
`max_sessions * per_session_limit`. Critical families receive larger leases,
normal families receive smaller leases, and global deadline/finalization reserve
remain hard boundaries. Increasing the session cap therefore improves focus but
does not linearly increase maximum work.

Priority scheduling starts critical and high-risk families first. If the global
lease runs out, untouched normal families become concise coverage limits rather
than partially initialized sessions.

## 3. Planner projection and transformations

Planner serialization reserves space, in this order, for:

1. every base assignment and transformation permission;
2. family load summaries and risk priorities;
3. a bounded complete changed-path index;
4. changed component, file-role, and diff-size summaries;
5. active relationship and recipe summaries;
6. detailed changed facts while space remains.

The planner must never receive `changed_paths=0` when the manifest is non-empty.
When concrete paths exceed their quota, it receives stable component/glob/role
counts plus the highest-priority concrete paths and an explicit omitted count.

The prompt explains that safe balancing may combine compatible small ordinary
assignments to free capacity for splitting an overloaded ordinary assignment.
The deterministic base should already be balanced; planner transformations are
optional refinements rather than the primary overload repair.

## 4. Bounded multi-path diff reads

`read_pr_diff` gains an optional `paths` array while retaining the existing
single `path` form. A call may request at most eight controller-authorized changed
paths. The total returned bytes remain bounded by the existing tool-result limit,
with per-path status and truncation metadata. Paths are canonicalized and checked
against the immutable changed-file manifest before any read.

Specialist guidance recommends batching closely related production and test
paths. A batch consumes one repository tool call but produces separately indexed
neutral evidence slices so later candidates and obligation proposals can cite
the relevant path. The controller may reduce the batch when the combined result
would exceed the response cap; it never silently substitutes unrelated paths.

## 5. Checkpoint convergence and tolerant core parsing

Checkpoint progress is fingerprinted from controller-visible state: candidate
lifecycle, accepted obligation/family assessments, retained evidence catalogue,
and concrete next actions. Rewording a summary does not count as progress.

After one compact-resume checkpoint with no semantic progress, another checkpoint
with the same progress fingerprint pauses the session. Critical policy may allow
one additional distinct action, but not repeated compact-resume cycles with an
unchanged state.

Checkpoint decoding first extracts exactly one JSON object, then canonicalizes
the recognized checkpoint fields. Harmless unknown fields and repeated
controller-owned projections such as `evidence_ids`, `obligation_statuses`, or
`inspected` are ignored and diagnosed by key name/count. They never mutate the
authoritative ledger. Recognized legacy `obligation_updates` are validated through
the existing obligation proposal path. A checkpoint is rejected only when its
required core, candidate objects, durable memory, or disposition-specific fields
remain invalid after canonicalization.

## 6. Compacted-evidence retrieval control

`read_compacted_evidence` remains restricted to evidence omitted by compaction.
It additionally requires a controller-owned obligation, family, or candidate
target handle and one purpose:

- `candidate_support`;
- `obligation_resolution`;
- `contradiction_check`.

The session records evidence ID, target, purpose, epoch, and subsequent state
change. The first justified recovery is allowed. A repeated recovery of the same
evidence/target/purpose is allowed only after relevant candidate or assessment
state changed. Otherwise the tool returns a bounded already-recovered result and
increments the no-progress signal without replaying the content.

The newest recovered evidence slices remain pinned through the following
compaction epoch under a small fixed byte/count quota. This prevents immediate
retrieve/compact/retrieve cycles while keeping total context bounded. Recovered
evidence is for exact support; checkpoints preserve conclusions and IDs rather
than copying raw results.

## 7. Source-aware secret redaction

Repository source and diff rendering use syntax-preserving redaction. Dynamic
references such as `{api_token}`, `$TOKEN`, `${TOKEN}`, `%TOKEN%`, and recognized
identifier expressions are not secret values and remain visible. Literal
high-entropy credentials and configured secret patterns are still masked.

When a value is redacted, the surrounding key, operator, and syntax remain
visible, for example `api_token="[REDACTED_VALUE]"`. Tool metadata explicitly
states that the replacement was applied by the controller and is not source
behavior. This prevents the reviewer from interpreting `[REDACTED]` as an
application-side security control.

Console output and published artifacts retain existing secret safety. Tests use
the dogfood diagnostic function and real-looking literal fixtures to prove both
semantic preservation and literal masking.

## 8. Verdict and handoff semantics

Publishing separates model/policy findings from GitHub event authorization:

- accepted blocking findings may recommend/request changes;
- concrete verification notes may require human action without becoming findings;
- incomplete high-risk coverage produces `Human review required` / notice;
- complete coverage with no actionable findings produces `No blocking findings
  identified`, even when automated approval is disabled;
- `allow_approve=false` selects a comment event, not a negative recommendation.

If the handoff model's `ai_reviewed_summary` is absent or rejected, the controller
builds a short factual fallback from covered families, accepted assessment
conclusions, retained changed evidence paths, and degraded stages. It describes
scope (“examined checkpoint and compaction behavior”) and never claims alignment,
correctness, complete coverage, or merge safety. Empty or purely generic model
human-focus text similarly falls back to the most material uncovered family or
degraded stage.

## Diagnostics

The structured artifact adds:

- atomic-to-family membership and work scores;
- desired/admitted session counts and global lease consumption;
- planner retained/omitted path and capability counts;
- multi-path read sizes and per-path truncation;
- checkpoint dropped-key diagnostics and progress fingerprints;
- compacted-evidence recovery/rejection counts by evidence and purpose;
- source-redaction type and count without secret values;
- handoff model-versus-deterministic source.

Workflow logs remain bounded and report only counts, lifecycle reasons, and
failure classes.

## Compatibility and migration

- Existing policies, atomic IDs, single-path `read_pr_diff`, and old checkpoints
  remain accepted.
- Existing `specialist_max_sessions` remains a hard cap. Projects wanting the
  large-review recommendation set it to twelve and configure the new global
  turn/tool leases; its existing default of eight and lower configured values
  remain valid. The dogfood workflow uses twelve.
- Migration guidance documents the recommended large-review cap, global leases,
  source-redaction semantics, and new compacted-evidence arguments.

## Testing

Tests cover:

- deterministic family stability, isolation, partial fan-out, and atomic artifact
  accounting;
- exclusion of tests/docs/fixtures from duplicate generic implementation
  obligations;
- balanced assignments for the observed 92-obligation dogfood shape;
- adaptive session count and strict global lease enforcement;
- non-empty large planner path/component summaries;
- authorized, bounded, per-path multi-diff behavior;
- identical no-progress checkpoint termination;
- tolerant checkpoint core extraction without controller-state mutation;
- single justified compacted-evidence recovery and rejection of repeated recovery
  without progress;
- preservation of source variable expressions and masking of literal secrets;
- clean, coverage-limited, concrete-verification, and finding-backed verdicts;
- deterministic `What the AI reviewed` fallback;
- focused integration replay proving the dogfood canaries remain visible to the
  model-facing evidence surface.
