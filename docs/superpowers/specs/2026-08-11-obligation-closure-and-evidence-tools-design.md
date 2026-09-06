# Obligation Closure and Evidence Tools Design

## Purpose

Specialist review obligations must guide bounded defect investigation without
turning absent or inapplicable evidence into an open-ended search. Specialists
need immediate, structured interaction with obligation and evidence state while
the controller remains authoritative. Checkpoints remain compact durable memory,
not the primary coverage-accounting protocol.

## Goals

- Let a specialist conclude that an obligation is covered, not applicable,
  exhausted, blocked, or still unresolved.
- Prevent repeated follow-up when no concrete, novel investigation remains.
- Activate only the evidence requirements relevant to the actual change.
- Separate neutral evidence collection from semantic coverage conclusions.
- Keep obligation bookkeeping out of checkpoint payloads whenever it was already
  recorded through controller tools.
- Preserve conservative handling of high-risk coverage without converting a lack
  of coverage into a code defect or an unbounded investigation.

## Non-goals

- The model will not directly mutate the authoritative coverage ledger.
- The change will not grant broader repository, command, or web access.
- Unresolved coverage alone will not become an accepted finding.
- The implementation will not require models to reproduce internal obligation
  IDs, evidence metadata, or complete controller state in every checkpoint.

## Architecture

The runtime uses three distinct layers:

1. **Deterministic applicability** creates only evidence requirements whose
   configured conditions match the immutable changed-file and topology state.
2. **Interactive assessment** lets a specialist inspect an obligation, collect
   neutral evidence for a short target handle, and propose a disposition. The
   controller validates each proposal and records the accepted assessment in a
   durable controller-owned ledger.
3. **Checkpoint memory** preserves the specialist's working summary, completed
   steps, hypotheses, candidates, unknowns, and proposed next actions across
   compaction. It does not repeat accepted evidence mappings, obligation status,
   or attempt history.

The existing long obligation IDs remain internal and stable. Each specialist
receives short session-local handles such as `O1`, `O2`, and `O3`.

## Obligation Lifecycle

Every assigned mandatory obligation has one of these dispositions:

- `pending`: no accepted conclusion yet.
- `covered`: retained evidence supports a concise conclusion about the changed
  behavior.
- `not_applicable`: the broad recipe matched, but the particular requirement does
  not apply to the actual change.
- `exhausted`: plausible bounded sources were checked and no further concrete
  investigation is available.
- `blocked`: the required evidence source is known but unavailable under the
  current trust boundary, permissions, or tool policy.
- `unresolved`: investigation remains incomplete and names at least one concrete
  next action.

`not_applicable`, `exhausted`, and `blocked` are explicit closure outcomes rather
than invitations to resume automatically. They retain their reason and evidence
limits for the final handoff. High-risk policy may require stricter validation of
`not_applicable`, but does not create unlimited retries.

## Conditional Evidence Requirements

Recipes may retain the existing `expected_evidence` form for compatibility. The
new preferred form is `evidence_requirements`, where each entry has:

- a stable `id`;
- an evidence `category`;
- optional deterministic `when` match conditions using the same immutable path,
  component, file-role, and risk inputs as recipe matching;
- optional requirement-local `seed_paths` and `related_paths`;
- a requirement mode of `required`, `optional`, or a named `one_of` group.

Only matched requirements create mandatory obligations. An unmatched requirement
is accounted as not applicable and is not assigned to a specialist. A coverage
rule may force a recipe to run at a higher risk tier, but it does not bypass the
individual evidence requirement's `when` condition.

Legacy `expected_evidence` entries continue to mean unconditional required
requirements. Migration documentation recommends replacing broad legacy lists
with conditional entries.

## Interactive Obligation Tools

The specialist tool catalogue gains controller-local tools that do not consume
the repository/web tool-call budget:

### `explain_obligation`

Input: a short target handle.

Output includes the objective, activation reason, accepted evidence requirements,
already-inspected sources, attempt history, current disposition, permitted closure
outcomes, and any concrete next actions still available.

### Targeted repository reads

Existing read-only repository tools accept optional short `targets`. Successful
results remain neutral evidence records and report:

- the retained evidence ID;
- whether the source is changed or unchanged;
- the targets for which the collection is eligible;
- that eligibility alone is not semantic coverage.

Targeting limits association candidates; it does not stamp a record with every
required evidence category or mark an obligation covered.

### `propose_obligation_resolution`

Input contains the short target, proposed disposition, concise reason, retained
evidence IDs, and concrete next actions when the disposition is `unresolved`.

The controller validates target ownership, evidence existence and eligibility,
independence constraints, disposition-specific requirements, and novelty. It
returns an immediate accepted or rejected result with a bounded explanation. An
accepted proposal updates durable controller state; the model does not need to
repeat it in its next checkpoint.

### `get_obligation_status`

Returns the current controller-owned assessment for one target, including the
last accepted or rejected proposal and remaining permitted actions.

These state tools have a separate bounded bookkeeping allowance so that using the
protocol does not reduce the specialist's repository investigation budget.

## Evidence Semantics

A successful file or diff read creates neutral retained evidence. It does not
cover an obligation merely because its path falls under a seed path. Coverage
requires an accepted specialist proposal containing:

- a concise semantic conclusion;
- eligible retained evidence IDs;
- satisfaction of independent-verification requirements when applicable.

Unchanged files can establish contracts or absence conditions but do not by
themselves demonstrate changed behavior. The controller reports this distinction
in tool results.

## Checkpoints

Normal exploration uses the obligation tools. A checkpoint contains only durable
working memory:

- `working_summary`;
- `completed_steps`;
- hypotheses and invariants evaluated;
- candidate additions and lifecycle updates;
- genuine unknowns;
- concrete proposed next actions.

For compatibility and emergency recovery, checkpoints may contain a small
`obligation_updates` fallback. The controller validates it through the same path
as `propose_obligation_resolution`. Checkpoints never need to repeat accepted
coverage maps, evidence metadata, attempt history, or full obligation objects.

## Bounded Negotiation

Negotiation targets include the last conclusion, inspected-source fingerprint,
attempt count, evidence delta from the last attempt, and concrete proposed next
actions. The controller offers `resume` or `consult` only when a novel feasible
action exists.

An obligation-level attempt ledger records:

- inspected path and tool-call fingerprints;
- retained evidence before and after the attempt;
- accepted and rejected dispositions;
- last conclusion and proposed next actions.

A follow-up that adds no evidence and completes no proposed action closes as
`exhausted` unless a newly available evidence source makes a different action
possible. Repeating an already attempted action is rejected. Default policy
allows one no-progress follow-up per obligation; high-risk policy may allow one
additional distinct attempt, never an unbounded retry loop.

The deterministic negotiator fallback follows the same novelty rule. When no
novel action exists, it records the applicable closure outcome rather than
resuming merely because budget remains.

## Prompt Contract

Specialists are explicitly told:

- coverage is not a request to find supporting evidence at all costs;
- broad recipe activation does not prove every requirement applies;
- unchanged sources may provide context without proving changed behavior;
- accepted obligation state is controller-owned and need not be repeated;
- unresolved work must identify a concrete novel next action;
- `not_applicable`, `exhausted`, and `blocked` are legitimate conclusions;
- accepted findings still require a concrete defect and consequence.

Checkpoint turns continue to state that tools are disabled and that tools will be
re-enabled after a compact-resume checkpoint.

## Verdict and Handoff Behavior

Coverage limitations do not create finding threads and do not request changes in
the absence of an accepted concrete finding. Material blocked or exhausted
high-risk areas appear once, concisely, in the human handoff. `not_applicable`
requirements are retained in the artifact for auditability but normally omitted
from the human summary.

## Compatibility and Migration

- Existing version-2 policy files remain valid.
- Legacy `expected_evidence` remains supported and unconditional.
- Existing checkpoints without `obligation_updates` remain valid.
- Internal obligation IDs and artifact provenance remain stable.
- The migration guide documents conditional requirements, lifecycle outcomes,
  target-aware tools, and the recommendation to avoid broad unconditional
  evidence lists.

## Diagnostics

Bounded runtime diagnostics and the detailed artifact record:

- why each requirement activated;
- every proposed disposition and validation result;
- evidence delta and action fingerprint per attempt;
- why a resume was offered or rejected;
- the final closure reason.

Normal workflow logs contain one concise line per proposal and negotiation action;
full structured details stay in the artifact.

## Testing

Tests cover:

- conditional versus legacy requirement activation;
- short target handle stability and ownership checks;
- neutral evidence not covering an obligation automatically;
- accepted and rejected disposition proposals;
- high-risk `not_applicable` validation;
- checkpoint fallback through the same validation path;
- resume admission only for novel concrete actions;
- no-progress exhaustion and bounded high-risk retries;
- persistence across checkpoint compaction and session resume;
- handoff/verdict behavior for coverage-only limitations;
- policy migration and artifact diagnostics.
