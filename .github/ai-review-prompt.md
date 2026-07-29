# Repository review priorities

Apply the bundled specialist protocol together with `.github/ai-review-rules.md`.
Concentrate on observable regressions in this GitHub Action:

- Trace changed inputs and defaults through `action.yml`, shell environment
  wiring, Python validation, documented examples, and focused tests.
- Preserve OpenAI-compatible and Anthropic-compatible request semantics,
  including streamed tool calls, strict verdict turns, retries, and cancellation.
- Treat tokens, model-controlled paths and arguments, repository API scope,
  source allowlists, publishing permissions, and current-branch policy changes as
  trust boundaries.
- For specialist-runtime changes, verify lifetime budget accounting, durable
  session continuity, bounded recovery, complete coverage accounting, and safe
  degraded or unresolved outcomes.
- For publishing changes, keep the human handoff brief and useful. Put detailed
  claims, evidence, unknowns, and web-access requests in resolvable review notes.

Report practical defects with concrete evidence and a plausible execution path.
Do not elevate stylistic preferences, purely hypothetical edge cases, or missing
tests without a meaningful regression risk. This workflow is non-approving:
recommend changes when warranted, but never claim that an approval was published.
