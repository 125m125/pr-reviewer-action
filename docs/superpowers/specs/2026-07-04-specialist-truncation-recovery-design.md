# Specialist Truncation Recovery Design

## Problem

Specialist orchestration currently treats token-limited model turns too much like
ordinary completed turns. A planner can reach `finish_reason=length` after producing
partial or recoverable output, while scheduling still combines planner, recipe, and
deterministic focuses up to the configured pass cap. A specialist can similarly spend
its entire turn reasoning, end immediately before emitting JSON, and then be invoked
again without its prior reasoning.

The observed result is excessive fan-out for small changes and duplicated specialist
work that loses useful context.

## Desired behavior

- A length-limited turn is never considered a clean completion, even if its text
  happens to contain parseable JSON.
- Planner and specialist work already performed is preserved across recovery.
- Recovery is bounded to one compact synthesis attempt; orchestration must not loop
  indefinitely or silently increase token budgets.
- A valid recovered planner controls the dynamic focus list. Deterministic focuses
  are failure fallback, not an unconditional source of extra passes.
- Repository-configured recipes remain authoritative and may supplement a recovered
  planner result.
- The initial-pass setting remains a ceiling, not a target.
- If specialist recovery still cannot produce a valid report, the pass is recorded
  as incomplete or failed without restarting the investigation cold.

## Design

### Truncation classification

Introduce a shared predicate for model stop reasons that recognizes OpenAI-compatible
`length`, Anthropic-compatible `max_tokens`, and the tool-loop's `truncated-turn`.
Diagnostics will explicitly record whether recovery was attempted and whether it
succeeded.

### Planner recovery

When the planner's exploration turn ends because of truncation, the runner performs
one compact, non-tool synthesis request. Its prompt contains:

- the planner's latest reasoning/text, bounded from the tail;
- the bounded planning context;
- executed tool evidence;
- an instruction to return only the planner JSON schema.

The original partial output is not accepted as a clean plan solely because JSON can
be extracted from it. If compact recovery produces a valid plan, that plan is used.
If recovery fails, the planner is marked degraded and deterministic focuses become
the fallback.

### Specialist recovery

When a specialist turn reaches the token limit before returning a valid report, the
existing terminal-synthesis request must carry forward the latest reasoning and all
bounded executed evidence. No second `run_focus` investigation is started merely to
obtain JSON.

If a length-limited turn contains valid report JSON, it still receives one compact
normalization/synthesis turn so that an abruptly cut response is not mistaken for a
complete report. If that recovery fails, the partial report may preserve supported
findings, but the pass remains incomplete and diagnostics identify truncation.

Coverage-gap continuation remains separate: it may run only after a valid specialist
report exists, and it must receive the prior report. Truncation recovery itself does
not consume another specialist investigation pass.

### Focus scheduling

Initial scheduling uses these sources:

1. valid planner focuses;
2. matching repository-configured recipes;
3. deterministic focuses only when the planner is degraded or produces no valid
   focus.

The scheduler stops before the pass cap when the best remaining candidate adds no
positive marginal coverage. The artifact records this as an omission reason, making
the reduced fan-out observable rather than mysterious.

### Failure behavior

- Planner recovery failure: mark planner degraded, schedule recipes plus deterministic
  fallback focuses, and continue.
- Specialist recovery failure: preserve diagnostics and any structurally valid,
  supported partial findings; mark the pass incomplete rather than rerunning cold.
- Complete absence of valid specialist reports retains the existing whole-PR fallback
  behavior.

## Testing

Regression tests will cover:

- a planner ending with `finish_reason=length` and prose/partial output, followed by
  successful compact JSON recovery;
- a truncated planner whose recovery fails, proving deterministic fallback is used;
- a successful planner proving deterministic focuses are not appended unconditionally;
- scheduling stopping below its maximum when candidates add no positive coverage;
- a specialist ending after reasoning such as `I will generate the JSON.` and proving
  compact synthesis receives that reasoning plus prior tool evidence;
- a truncated but parseable specialist response still being marked/recovered rather
  than accepted as a clean completion;
- failed specialist recovery not triggering a cold second investigation.

Existing specialist, tool-loop, and full Python test suites must remain green.
