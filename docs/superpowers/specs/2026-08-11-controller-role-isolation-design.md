# Controller Role Isolation Design

## Problem

The specialist runtime currently appends every controller role instruction to
the repository's complete code-review system prompt. That prompt correctly
encourages specialists to inspect files and use tools, but it conflicts with
tool-free controller roles such as the planner and handoff summarizer. The
handoff request also receives the complete review policy although it is only
authorized to summarize a small set of validated facts. When a tool-free role
emits tool markup, the structured repair retains the malformed assistant turn,
which can steer the retry into invented repository analysis.

Planner transformation rules are expressed only as prose. The controller
knows that independent recipe assignments cannot be merged or split, but the
model has to infer what "compatible ordinary assignments" means. Finally,
repository-access requests are consolidated into one publication note while
publication readiness still counts every underlying request.

## Goals

- Keep repository review guidance and tools available to durable specialists.
- Give controller roles small, non-conflicting prompts and only the state they
  are authorized to transform.
- Make planner transformation permissions explicit and machine-readable.
- Preserve useful partial-JSON repair while discarding mode-violating output.
- Count deliberately consolidated publication notes consistently.

This change does not alter specialist assignments, obligation derivation,
evidence authority, source allowlists, verdict policy, or publication content.

## Considered Approaches

### 1. Append stronger negations to the existing shared prompt

This is the smallest textual change, but it leaves contradictory instructions
in the same system message. The failed run already had a role-specific suffix
that disabled tools, and the model still followed the earlier review prompt.
This approach is rejected.

### 2. Isolate controller roles and expose admissible operations

Controller roles receive a short trust/structured-output preamble followed by
their role contract. Durable specialists alone receive the repository's review
prompt. Planner inputs include controller-derived transformation permissions,
and the handoff input contains only facts it can safely summarize. This is the
recommended approach.

### 3. Remove optional model roles and use deterministic projections only

This would eliminate schema failures but also remove useful model-authored
change summaries, plan refinements, critic decisions, and human-oriented
handoffs. Deterministic fallbacks remain necessary, but replacing every role
is outside the requested scope.

## Design

### Prompt boundaries

`_role_prompt` will no longer concatenate the repository system prompt for
`change_summarizer`, `planner`, `negotiator`, `critic`, or
`handoff_summarizer`. These adapters receive a controller-role preamble that:

- treats supplied state as immutable and untrusted;
- states that repository tools and exploration are unavailable;
- requires exactly the role's structured result; and
- forbids review verdicts or new evidence.

The configured repository system prompt continues to apply to specialist
sessions unchanged. Repository policy remains controller-owned structured
state and is projected only where a role genuinely needs a bounded subset.

### Planner permissions

The compact planner context will add a permission object for every assignment.
It identifies permitted operations and valid merge peers. Independent recipe
assignments expose reorder/improve only; they are never merge targets, merge
sources, or split candidates. Ordinary assignments expose merge peers only
when the deterministic controller compatibility rules permit the merge.

The planner prompt will say that an operation absent from these permissions is
invalid. Existing controller validation remains authoritative and continues to
reject malformed or stale transformations.

### Handoff projection

The handoff summarizer receives only:

- the validated one-sentence change overview as orientation;
- `successful_review_facts`;
- bounded prepared-note themes/counts;
- degraded stages; and
- the exact reference IDs and paths it may repeat.

It will not receive the complete review policy, raw coverage ledger, raw
unknowns, PR body, or candidate details. Its prompt explicitly says that facts
are final for this presentation step, tools are unavailable, and it must not
inspect files or recreate "What changed." Existing controller validation and
deterministic fallback remain unchanged.

### Structured repair

Repair behavior depends on the failure class:

- A length-limited or syntactically partial JSON response retains the partial
  assistant content/reasoning and continues it with tools disabled.
- Tool calls, textual tool markup, ordinary code-review prose, or another
  clear role-mode violation trigger a clean retry from the original system and
  user context. The malformed assistant turn is not retained.
- The clean retry states the violated role contract and again requires exactly
  one JSON object.

Retry count and output budgets remain bounded.

### Publication accounting

Publication readiness will count source-access notes after the same
consolidation used by `build_source_access_request_notes`, rather than counting
raw requests. Other note categories retain their existing defensive checks.
Two requests for the same repository/revision/endpoint therefore require one
prepared note and no longer block an otherwise complete review.

## Error Handling and Diagnostics

Invalid planner transformations remain non-fatal and retain the deterministic
base plan. Controller-role failures continue to emit bounded diagnostics and
use deterministic fallbacks. Diagnostics will distinguish partial-JSON repair
from a clean mode-violation retry so future logs explain whether malformed
history was retained.

## Testing

Tests will first reproduce each production failure:

1. Controller roles do not contain repository exploration/tool instructions,
   while specialist prompts still do.
2. An isolated recipe assignment advertises no merge or split permission, and
   ordinary compatible assignments advertise only valid peers.
3. Handoff requests omit the full policy and raw review state.
4. Tool markup causes a clean structured retry; partial JSON still continues
   with retained reasoning.
5. Two consolidatable repository-access requests produce one required note and
   publication remains ready.

Focused CLI/controller/gateway/adjudication tests will run before the complete
test suite. The dogfood workflow will be repinned in a separate commit to the
verified runtime commit.
