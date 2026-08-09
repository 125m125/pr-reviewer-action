# Checkpoint Epoch Compaction Design

## Problem

Specialist sessions preserve provider `reasoning_content` and complete tool
results across exploration turns. This improves local-model continuity, but the
current admission estimate omits material wire costs such as tool schemas,
message wrappers, and provider chat-template overhead. A session can therefore
send a request that the controller estimates at less than the configured
context window while LM Studio rejects the rendered request as too large.

Current compaction is also mechanical. It may shorten or collapse exploration
history without first requiring the specialist to externalize its working
model. Read-heavy specialists can accumulate many turns whose reasoning only
says which file will be read next, leaving conclusions and planned comparisons
implicit when old reasoning is removed.

## Goals

- Detect context pressure from a conservative rendered-request estimate.
- Make every validated checkpoint a durable epoch boundary.
- Require a cumulative working summary, completed steps, and next actions in
  addition to existing evidence, hypothesis, invariant, candidate, and unknown
  state.
- Compact only after a valid checkpoint and only when the checkpoint reason or
  a later continuation requires it.
- Preserve recent investigative structure by retaining old tool calls with
  compact result placeholders using their original call IDs.
- Permit exact, bounded retrieval only for evidence explicitly compacted by the
  controller.
- Degrade safely when neither checkpoint attempt succeeds.

## Non-goals

- Do not checkpoint periodically merely because a fixed number of tools ran.
- Do not publish checkpoint working memory as review evidence or findings.
- Do not copy private reasoning into ordinary assistant content.
- Do not reset lifetime model-turn, tool-call, recovery, or deadline budgets.
- Do not create synthetic orphan tool results or change immutable assignment
  boundaries.

## Checkpoint Contract

All checkpoint reasons use one cumulative schema. Add these bounded fields:

- `working_summary`: one string of at most 2,000 characters describing the
  current understanding, important relationships, and material conclusions.
- `completed_steps`: at most 12 strings of at most 500 characters describing
  what was checked and the conclusion, including ruled-out concerns where
  useful.
- `proposed_next_actions`: retain the existing field, at most 12 strings of at
  most 500 characters.

Existing `hypotheses`, `invariants_evaluated`, candidates, evidence IDs,
unknowns, and obligation statuses remain authoritative in their current roles.
Working summary and completed steps are non-authoritative continuation memory;
they never establish evidence or a publishable defect.

The controller merges cumulative working state into `SessionCheckpoint`.
Candidate updates remain model-friendly deltas, but the controller must be able
to materialize a self-contained snapshot containing all active candidate
definitions before older epochs are removed.

## Checkpoint Reasons and Dispositions

The checkpoint prompt always states:

- the concrete checkpoint reason;
- whether compaction follows immediately;
- whether the session will resume, pause for controller evaluation, or finalize;
- that the checkpoint must be self-contained because older history may later be
  removed.

Checkpoint creation and compaction are separate operations:

- Context pressure: validate, compact immediately, and resume when the lease
  permits.
- Repetitive, polluted, or no-progress transcript: validate and compact before
  a resumed investigation.
- Provider-history recovery: validate and rebuild immediately.
- Normal model completion: validate and pause/finalize without immediate
  compaction.
- Tool lease, phase boundary, or controller request: retain full history while
  it fits; evaluate compaction when continuation is admitted.
- Final checkpoint: finalize without compaction because no later model request
  consumes the transcript.

If a paused session is resumed, the controller first estimates its rendered
continuation. It reuses the existing valid checkpoint as the compaction boundary
when pressure exists; it does not request a redundant checkpoint.

## Admission and Reserved Repair

Admission uses a conservative estimate of the exact request mode:

- rendered messages, including tool-result trust-boundary wrappers;
- assistant tool calls and IDs;
- tool schemas when tools are enabled;
- response-format/schema payload where applicable;
- a configurable or deterministic provider/chat-template safety margin.

Before context compaction, the controller projects a no-tools checkpoint turn.
The pressure threshold reserves the worst-case second request:

```
checkpoint_input
+ first_checkpoint_output_limit
+ repair_instruction
+ repair_output_limit
+ wire_safety_margin
>= model_context_limit
```

Checkpoint and repair output limits are bounded independently of the normal
exploration output limit. The implementation may choose values within existing
configuration, but must reserve both attempts before the first checkpoint
request. Context-length provider errors are classified as recoverable context
pressure rather than generic specialist failures.

## Repair and Failure

The first checkpoint request sees the complete pre-compaction transcript. If its
ordinary content is invalid, append the provider response using native content
and reasoning fields, then make one strict repair request with tools and
reasoning disabled. Do not copy reasoning into ordinary content.

No compaction occurs until a checkpoint validates. If repair fails:

- retain the previous valid checkpoint when present;
- record candidate-retention uncertainty when material candidate-shaped output
  cannot be reconciled;
- do not continue as though new working state was safely retained;
- terminate or pause the specialist in a degraded state when no safe checkpoint
  exists.

## Epoch Retention

Each validated checkpoint closes one exploration epoch.

### Current epoch

Retain full assistant reasoning, tool calls, and tool results.

### Previous epoch

After compaction:

- remove historical reasoning;
- retain assistant tool calls and their original call IDs and arguments;
- replace each matching tool result body with a small deterministic compacted
  payload using the same call ID;
- include the exact evidence ID, source identity, original byte count, and
  compacted status;
- retain the latest one or two complete tool exchanges unchanged;
- remove assistant messages left empty after reasoning removal.

The original evidence remains in `EvidenceStore`. `read_compacted_evidence`
continues to accept only evidence IDs explicitly registered during compaction.

### Older epochs

When a second validated checkpoint exists, non-checkpoint messages older than
the previous checkpoint may be removed completely. Retain structurally valid
checkpoint request/response pairs. Before removing a checkpoint needed for
candidate or working-state history, materialize the latest cumulative controller
snapshot.

If ordinary compaction still cannot admit the next request, emergency
reconstruction retains only:

- system prompt and immutable assignment;
- latest cumulative checkpoint snapshot;
- bounded compacted-evidence ledger;
- newest complete exchanges that fit;
- one continuation instruction.

## Continuation

The post-compaction continuation message explains exactly what was removed,
lists the evidence IDs eligible for bounded retrieval, and directs the model to
continue from `proposed_next_actions`. It contains no ephemeral budget counter.
No separate generic “continue” message is added.

## Diagnostics

Artifact and bounded console events record:

- checkpoint reason and declared post-checkpoint disposition;
- rendered admission estimate by messages, tool schemas, response reserve, and
  safety margin;
- checkpoint and repair parse outcomes;
- compaction level and epoch boundary;
- counts of removed reasoning messages, placeholder-replaced results, removed
  old exchanges, and retained full exchanges;
- before/after rendered estimates;
- provider context-limit recovery outcome.

Logs never include prompts, raw responses, evidence bodies, secrets, or private
reasoning.

## Testing

Tests must prove:

- rendered admission includes tool schemas and triggers before the provider
  limit observed in the dogfood run;
- checkpoint prompts state reason and disposition;
- cumulative working fields survive parsing, snapshotting, and reconstruction;
- malformed first checkpoints retain native response state for one repair;
- compaction never occurs before a valid checkpoint;
- compact result placeholders retain original call IDs and wire validity;
- recent exchanges remain complete while older epochs can be removed;
- emergency reconstruction contains the cumulative active candidate register;
- normal completion checkpoints do not compact unnecessarily;
- context-length provider failures enter bounded recovery instead of immediate
  generic degradation;
- lifetime budgets and evidence retrieval restrictions remain unchanged.
