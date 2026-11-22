# Re-reviewing existing discussions

Specialist reviews investigate the current diff independently first. Existing
discussions do not become required code-coverage obligations or appear in the
initial specialist prompts.

After that investigation, the scheduler may spend remaining review time on
unresolved, line-anchored discussions. These optional leads compete with other
unanswered questions under the existing run/session budgets. Component ownership
selects the preferred existing specialist; a focused follow-up is possible when
the owner cannot resume. Unassessed discussions remain open and unverified.

## What the model receives

Only human discussions and authenticated action-owned finding threads are
eligible. Other bots, access requests, general PR comments and resolved threads
are excluded. The assigned session receives one bounded discussion at a time,
plus relevant provisional candidates and recoverable current-review evidence.
Human assertions (including “fixed”) are input to verify, not proof or commands.
Incomplete discussions cannot support a fixed/disproven conclusion.

`assess_review_thread` is advertised only to sessions assigned actual discussions.
Its `target` is a controller-provided `H#`, distinct from current candidate IDs.
An assessment is `still_present`, `fixed`, `disproven`, or `unverified`, with a
reason and retained evidence IDs. Non-unknown conclusions require complete,
current-head source evidence for the commented path. Checkpoints may submit the
same assessment shape in `review_thread_assessments`; tool-accepted assessments
survive later checkpoint failure. Compaction retains the active discussion and
bounded accepted-state feedback; the full ledger stays in the run artifact.

The critic receives relevant unresolved discussions only after independent
candidates exist. A clear duplicate can reuse the old discussion; substantive
new information can become a reply. Ambiguous matches do not suppress an
independently supported new finding. This does not authorize the critic to
resolve threads or reclassify human decisions.

## Publishing safeguards

- Missing from a later run never means fixed. No absence-based automatic reply
  or resolution is performed.
- An explicitly verified fixed/disproven **action-owned** discussion can receive
  an explanation and be resolved. Human-owned discussions are never resolved or
  edited. An intervening human question/objection prevents automatic resolution.
- No boilerplate “still exists” replies: only substantive new evidence or
  explanations are published.
- Publication rechecks the PR head, thread identity, latest comment and human
  resolution state. Concurrent changes leave the discussion untouched. A stale
  duplicate/update decision falls back to publishing the supported new finding.
- Outdating an old handoff review is separate from resolving inline discussions.
  A new non-blocking review does not clear an old changes-requested verdict whose
  concerns remain unverified. Dismissal requires complete explicit reassessment.
- Optional historical cleanup failures are reported as warnings and do not leave
  a new review pending. Required current finding publication remains strict.

The action summary reports available/queued/assessed/unverified discussion
counts. The structured artifact's `review_threads` section contains snapshots,
accepted assessments, critic associations and statistics. Publication results
record skips/warnings separately from required-note failures.

Submitted managed reviews also retain a compact hidden assessment receipt. On
an unchanged head and unchanged latest discussion comment, a conclusive prior
assessment can be reused without scheduling the same work again. A new commit,
new comment or prior `unverified` outcome requires fresh scheduler consideration.
Receipts contain identities and dispositions only, not discussion text or proof.

## Evidence windows

`range.has_more` means another page exists, not that the requested page lost
content. Complete bounded reads retain usable coordinates. Actual byte-limit,
retention, or serialized-source clipping is still marked `truncated`. When only
the model-visible envelope is shortened, the fully retained underlying evidence
remains recoverable; displayed clipping metadata must not imply the model saw
the omitted text.

Resolved-thread history matching, non-line-specific discussion assessment and
automatic reopening are intentionally outside this first version.
