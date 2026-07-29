# Dogfood AI Review Workflow Design

## Goal

Configure this repository to review manually selected, same-repository pull
requests with the new version-2 specialist runtime and a local LM Studio model.

## Runtime and trigger

- Run only for a non-draft pull request when a maintainer adds the `ai-review`
  label.
- Keep the existing self-hosted Linux `lm-studio` runner and the minimal public
  review container. The container provides useful dependency and process
  isolation; it must not receive the Docker socket or privileged mode.
- Retain the preparatory runner job because its UID/GID and action-cache path
  are required by the job container.
- Pin the public container by OCI index digest because it receives both the
  GitHub review token and LM Studio credential.
- Remove ineffective `workflow_dispatch` and unrelated pull-request event
  types. A dispatch without a PR-number input has no pull-request payload.
- Keep full-history checkout, no persisted credentials, and full review scope.

## Model

Use `qwen/qwen3.6-35b-a3b`. On the target RTX 5070 Ti it generates about
55 tokens/second despite partial CPU offload, which is fast enough for bounded
sequential specialist sessions. Its repository-level coding and tool-use
strength is more valuable here than Gemma 4 12B QAT's approximately 80
tokens/second latency advantage.

Use the native 262,144-token context, streamed responses, reasoning during
investigation, and no reasoning for strict verdict serialization.

## Specialist runtime

- Use `.github/ai-review-policy.json` as the authoritative version-2 policy.
- Replace deprecated pass and packet inputs with direct lifetime limits:
  8 initial sessions, 2 follow-up sessions, 64 model turns per session,
  20 tool calls per session, and 1 recovery.
- Use the 7,200-second review deadline and default 10/60/20/10 phase shares.
- Give the enclosing job a 150-minute timeout so checkout, corpus construction,
  artifact production, and publishing have headroom around the runtime deadline.
- Keep concurrency at 1 because LM Studio serves one local model.
- Preserve the larger local-model token, transcript, timeout, and read-only
  tool-response budgets where they remain operative.
- Append `.github/ai-review-prompt.md` to the bundled specialist protocol.

## Repository policy

The version-2 policy describes the action's real architectural seams:
orchestration/shell, specialist runtime, model transport/conversations,
publishing, and tests/docs. It schedules focused coverage for security,
backward-compatible action inputs, model API compatibility, specialist
session behavior, and publishing hygiene.

Web access is limited to official documentation hosts relevant to this action.
The optional `AI_REVIEW_SEARCH_URL` repository variable supplies the trusted
SearXNG-compatible discovery endpoint; an empty value disables search while
direct allowlisted fetching remains available. Publishing is restricted to
`review_comment`, and approval remains disabled.

The prompt addendum stays concise and repository-specific. Detailed protocol
and output formatting remain owned by the action's bundled prompt.

## Publishing

Publish a short sticky human handoff plus line-anchored detailed findings using
`review_comment`. Keep `publish_review_comment=true`, inline findings enabled,
and `allow_approve=false`.

## Verification

- Parse the workflow YAML and both JSON/Markdown configuration files.
- Validate the version-2 policy with the repository policy loader.
- Run focused action-input, migration, policy, and workflow tests.
- Inspect the final diff for imported MovieHRdb-specific assumptions.

## Operational caveat

Updating the review container is an explicit operation: inspect the new public
image, resolve its OCI index digest, update the workflow pin, and review that
change before applying the `ai-review` label.
