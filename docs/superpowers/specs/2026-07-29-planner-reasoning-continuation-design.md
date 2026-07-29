# Planner Reasoning Continuation Design

## Goal

Prevent a structured planner call from degrading to deterministic fallback when
an OpenAI-compatible reasoning model consumes its entire completion allowance
in `reasoning_content` before emitting the required JSON plan.

## Design

The planner role keeps one temporary `Conversation` for its bounded
completion-producing model calls. Its configured
`specialist_planner_max_tokens` value is the actual
per-request output allowance rather than being reduced by the generic
specialist-session output cap.

When the first planner response ends with `finish_reason: length` and has no
parseable JSON content, the adapter retains the returned intermediate
reasoning as assistant text in that same conversation and requests one
continuation with reasoning enabled. If that response still does not provide
parseable JSON, the adapter retains any additional intermediate text and makes
one final request with `reasoning_effort: none`, tools disabled, and an
ephemeral instruction to return only the required JSON object.

The sequence is capped at three completion-producing model calls: initial
reasoning, reasoning continuation, and forced JSON finalization. An HTTP
request rejected before inference because a provider does not support
`response_format`, followed by the existing compatibility retry, remains one
model call because it produces at most one completion. Successful structured
output returns immediately. Transport errors, non-truncation malformed output,
deadline exhaustion, or failure of the final request continue to use the
controller's existing deterministic planner fallback.

## Boundaries

- Apply continuation only to the planner role.
- Do not publish or persist reasoning in review artifacts.
- Do not copy reasoning into specialist sessions or other controller roles.
- Do not relax assignment validation or planner context admission limits.
- Every completion-producing call uses the same immutable compact planner
  context and the existing absolute planning-phase deadline.

## Verification

Tests reproduce a response whose entire completion is `reasoning_content` with
`finish_reason: length`, assert that reasoning is present in the next request,
assert that the final request disables reasoning, and assert that valid JSON
returns without deterministic fallback. Separate coverage verifies the
planner-specific token allowance is 8192 even when the generic session output
limit is 4096, plus immediate return for a valid first response.
