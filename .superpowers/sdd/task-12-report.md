# Task 12 implementation report

## Outcome

Implemented deterministic specialist review-note publication for GitHub. The
publisher consumes only the typed runtime policy result, sparse `ReviewHandoff`,
typed `ReviewNote` values, and explicit PR snapshots. It does not accept
transcripts, raw model responses, evidence-store objects, or tool output. Task
13 controller/action wiring remains out of scope.

The review hardening pass closed the managed-state trust boundary: state is
accepted only after complete GraphQL pagination of review threads, every
thread's comments, issue comments, and reviews, plus complete REST pagination
of changed files. Any query, shape, ownership, cursor, or page-limit failure
fails closed before detailed mutations and preserves prior note state.

## RED / GREEN record

1. **Anchoring and compatibility.** RED cases covered truncated diffs, file 101,
   hunk content beginning with `+++`, unsafe paths, and Linux filenames that
   contain backslashes. GREEN uses the complete files snapshot as FILE-anchor
   authority, requires a parsed added RIGHT-side line for LINE anchors, tracks
   files/diff completeness separately, and preserves the legacy adapter's exact
   path behavior.
2. **Managed-state pagination and ownership.** RED cases covered second-page
   threads, top-level comments, per-thread replies, copied markers, GraphQL
   `errors`, missing `data`, missing cursors, repeated cursors, and changed file
   101. GREEN paginates each connection to completion and accepts only starter
   markers and status markers authored by the authenticated publisher.
3. **Sticky handoff lifecycle.** RED showed `edit-last` could overwrite an
   unrelated comment. GREEN finds the exact publisher-owned handoff marker,
   PATCHes that comment only, creates it only when absent, reconciles ambiguous
   writes by exact final body, and refreshes it after submission with exactly one
   aggregate managed-review link and no finding detail.
4. **Strict mutation responses.** RED accepted truthy, partial, or malformed
   GitHub success objects. GREEN validates typed positive comment IDs, node IDs,
   canonical HTTPS URLs, confirmed resolution state, and review/thread response
   shapes before recording success or starting dependent mutations.
5. **Idempotency and interruption recovery.** RED cases simulated timeouts after
   the server committed pending-review creation, thread creation, status reply,
   resolution, and review submission, plus interruption after thread creation.
   GREEN never blindly retries mutations, safely re-queries by run/fingerprint/
   generation markers, checkpoints every confirmed mutation atomically, resumes
   an owned pending review, and reuses an already-submitted owned review for the
   same head.
6. **Resolution ownership and recurrence.** RED cases covered human-resolved
   recurrence, publisher-resolved recurrence, repeated runs, and non-resolvable
   general comments. GREEN never reopens a human-resolved thread, creates a new
   generation only after a publisher-owned resolution, and deduplicates status,
   resolution, and general-answer replies by publisher-owned markers.
7. **Artifact and direct-input hardening.** RED cases covered credentials,
   query/fragment smuggling, invalid ports, backslashes, control characters,
   Markdown-label injection, secret labels, noncanonical SHAs, invalid repos,
   and truthy non-booleans. GREEN canonicalizes a narrow HTTPS artifact URL
   form, escapes/redacts bounded labels, and validates all direct publisher
   inputs before the first mutation.
8. **Cleanup compatibility.** Submitted specialist reviews start with the
   existing `<!-- ai-pr-reviewer... -->` managed-review prefix for COMMENT,
   APPROVE, and REQUEST_CHANGES cleanup discovery.

## Lifecycle and safety review

- `comment` performs one exact-marker sticky upsert and persists its checkpoint.
- `review_comment` and `review_verdict` publish the sparse sticky first, query
  complete managed state, reconcile existing findings, checkpoint general
  comments and thread mutations, create/resume one marked pending review, add
  only new managed generations, submit, then refresh the sticky aggregate link.
- Native events come only from `RuntimeVerdictPolicyResult` plus the existing
  approval, fork, scope, and clean-baseline guards. Handoff/model prose cannot
  select the native verdict.
- Every mutation is single-attempt. Ambiguous outcomes are reconciled only with
  safe queries and exact publisher-owned markers. Atomic state writes keep an
  ordered operation journal with recovered IDs/URLs/fingerprints.
- The pending and submitted review bodies use a head-specific managed marker.
  This supports timeout reconciliation, repeat-run idempotency, and the existing
  native-review cleanup prefix.
- Unanchored findings become visibly non-actionable verification requests and
  use a marker-bearing, explicitly non-resolvable general PR comment.

## Security review

- GitHub requests use argv lists through the Python platform seam. GraphQL
  documents/variables and REST bodies are passed through private temporary
  `--input` files, removed in `finally`; model-derived text is never interpolated
  into shell commands or argv.
- Managed state is publisher-owned, fully paginated, and shape checked. A human
  copying a marker into a reply, issue comment, or thread starter cannot claim
  ownership.
- Publication errors are secret-redacted and bounded. Markdown is sanitized and
  reserved upstream markers are stripped before locally generated markers are
  appended.
- Artifact URLs allow only canonical HTTPS URLs without credentials, query,
  fragment, whitespace/control characters, backslashes, or invalid ports.
  Labels are bounded, secret-masked, Markdown-escaped, and protocol-neutralized.

## Compatibility wrappers

- `scripts/build_review_comments.py` retains its public functions/CLI while
  delegating legacy diff-position parsing to the new state-aware parser.
- `scripts/resolve_finding_threads.py` retains its public functions/CLI while
  delegating bounded marker extraction.
- `scripts/publish_helpers.sh` retains existing action behavior and exposes only
  the specialist CLI adapter; Task 13 wiring is unchanged.

## Verification

- Specialist publication module: **62 passed**.
- Publisher plus build-comment, sanitation, and resolve-thread host-independent
  coverage: **151 passed, 12 Windows-only end-to-end cases deselected**.
- Full Python suite with UTF-8 mode: **1459 passed, 21 pre-existing Windows-host
  failures**. The failures are the known Windows mode-bit assertion, evidence
  provider subprocess launch behavior, and extensionless fake-bash resolve
  fixtures; no specialist publication test failed.
- Python compilation and `git diff --check`: run in the final verification pass.

## Boundaries

- GitHub review threads are a GraphQL-only surface. Forgejo specialist-thread
  parity is not fabricated; the existing platform seam fails loudly there.
- Task 13 action/controller wiring and Task 17 dispatcher changes remain out of
  scope.
- The two unrelated untracked July 12 documents were not read, edited, staged,
  or committed.
