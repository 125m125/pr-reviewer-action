# Task 12 implementation report

## Outcome

Implemented deterministic specialist review-note publication for GitHub. The
new publisher consumes only the typed runtime policy result, sparse
`ReviewHandoff`, typed `ReviewNote` values, and the current PR diff/files
snapshot. It does not accept transcripts, raw model responses, evidence-store
objects, or tool output. Task 13 controller/action wiring remains out of scope.

## RED / GREEN record

1. **RED:** `tests/test_github_review_notes.py` failed collection with
   `ModuleNotFoundError: pr_reviewer.github_review_notes`. **GREEN:** pure
   RIGHT-side LINE / changed FILE selection, unanchored finding downgrade,
   exact GraphQL variables, all three mode lifecycles, general-comment fallback,
   human-resolution preservation, native approval guards, and separate error
   state passed 10 tests.
2. **RED:** the production client raised `reply_thread requires repository
   context`. **GREEN:** the managed-state query binds validated repo/PR context
   and replies use a file-backed REST request; all note text remains outside
   argv.
3. **RED:** importing `scripts.publish_specialist_review` failed. **GREEN:** the
   CLI strictly deserializes only final handoff/notes/policy/files/diff/artifact
   inputs into the typed publisher API.
4. **RED:** shared legacy primitives and the shell compatibility entry point did
   not exist. **GREEN:** `build_review_comments.py` delegates legacy diff
   positions, `resolve_finding_threads.py` delegates marker parsing, and
   `publish_helpers.sh` exposes the specialist publisher wrapper.
5. **RED:** a current changed file absent from a truncated diff could not receive
   a FILE anchor, and an unanchored finding retained defect-like prose.
   **GREEN:** current PR files independently authorize FILE anchors while LINE
   still requires an added RIGHT-side diff line; downgraded findings are visibly
   labeled non-actionable verification requests.
6. **RED:** the sparse sticky handoff had no locally generated managed marker.
   **GREEN:** every handoff starts with the specialist handoff marker, without
   copying note detail.
7. **RED:** a generator of current PR files was exhausted after normalizing the
   first note. **GREEN:** the publisher materializes the controller-owned files
   snapshot once and applies it to every note.

Final Task 12 suite: **16 passed**.

## Lifecycle self-review

- `comment` performs exactly one sticky-handoff update. It includes only the
  sparse handoff and validated retained-artifact links; detailed note markdown
  is not read into the sticky body.
- `review_comment` performs sticky update, managed-state query, same-fingerprint
  status replies and absent-fingerprint resolution replies/mutations, managed
  general-comment updates, one pending review, new LINE/FILE thread additions,
  `COMMENT` submission, then atomic state persistence.
- `review_verdict` uses the same flow and derives only `APPROVE` or
  `REQUEST_CHANGES` from `RuntimeVerdictPolicyResult` plus the existing
  `allow_approve`, fork, incremental-scope, and clean-baseline guards. Handoff
  recommendation/model text cannot select the native event.
- Same open fingerprints receive current status/evidence replies. Missing prior
  fingerprints receive fixed/answered replies and a resolve mutation. A resolved
  same-fingerprint thread is recorded and never silently reopened. Changed
  evidence with the same stable fingerprint is an explicit reply; a new
  fingerprint creates a new thread.
- Unanchored verification/source requests and downgraded findings receive a
  dedicated marker-bearing general PR comment. Its text and persisted state both
  disclose that GitHub cannot resolve it. Answered general requests receive a
  separate follow-up and preserve the original historical comment.
- State records sticky/review/thread/comment IDs and URLs, fingerprint, anchor
  type, resolution, human-resolution, non-resolvable limitation, and publication
  errors. Publication calls retry at most three times and never rerun analysis.

## Anchor and normalization self-review

- A LINE anchor requires a safe repository-relative note path authorized by the
  current files snapshot and an actually added new-side line in the current
  unified diff. Context lines, deleted-side lines, booleans, non-positive lines,
  absolute paths, backslashes, and dot segments cannot become LINE anchors.
- A safe current changed file without a defensible added line becomes FILE,
  including when the diff was truncated. When no files snapshot is supplied,
  the new-side paths in the current diff are the fallback authority.
- An off-change or unlocated finding is downgraded to a non-actionable
  verification request. No note-supplied path/line is trusted on its own.
- LINE variables contain exactly `subjectType: LINE`, `path`, `line`, and
  `side: RIGHT`; FILE variables contain `subjectType: FILE` and `path`, with no
  line or side.

## Security self-review

- All GitHub calls use argv lists through the Python platform seam (`gh_argv`),
  while sticky publication invokes the existing shell platform helper with a
  static command and a body file.
- GraphQL query text, bounded variables, REST bodies, note markdown, and comment
  bodies use temporary `--input` files. The files are created privately and
  explicitly chmodded `0600` on POSIX, then removed in `finally`. Model-derived
  text is never interpolated into a shell command or placed in argv.
- Publication error text is secret-redacted and bounded. Markdown reuses reserved
  marker stripping, reference/mention sanitation, and secret masking before
  locally generated managed markers are appended. Invalid fingerprints are
  deterministically hashed before marker use.
- The production API surface has no transcript/evidence-store parameter, and the
  CLI has no option for either. Only the final policy projection can affect a
  native verdict.

## Compatibility-wrapper self-review

- `scripts/build_review_comments.py` retains its existing public functions and
  CLI while delegating diff-position parsing to the new module. Its historical
  context-line behavior is intentionally isolated as a compatibility primitive;
  the specialist publisher's stricter LINE policy does not inherit it.
- `scripts/resolve_finding_threads.py` retains its existing public functions and
  CLI while delegating bounded managed-marker parsing.
- `scripts/publish_helpers.sh` retains all legacy functions and adds only the
  specialist CLI adapter. Existing action arms remain unchanged for Task 13/17.

## Verification

- Task 12 suite: **16 passed**.
- Exact focused command from the brief: **77 passed, 9 pre-existing
  Windows-host failures** (eight extensionless fake-bash launch cases and the
  Windows `chmod(0600)` reporting case). Its host-independent subset is **73
  passed, 13 deselected**.
- Full specialist-runtime suite: **331 passed**.
- Build-comment, sanitation, platform, and Forgejo platform regressions:
  **102 passed**.
- Resolve-thread pure/wiring coverage on native Windows: **18 passed, 12
  deselected** (the deselected end-to-end group launches extensionless bash
  fakes directly through Windows `subprocess`, a documented baseline host
  limitation).
- Publisher/platform/sanitation/API-key group: **109 passed, 1 pre-existing
  Windows mode-bit failure** (`chmod(0600)` is reported as `0666` by Windows;
  POSIX enforcement remains in production).
- Native shell platform seam: **39 passed**.
- Native verdict safety: **15 passed**.
- Native approval guardrails: **24 passed**.
- Python compilation: passed.
- `git diff --check`: clean.

## Concerns and boundaries

- GitHub review threads are a GraphQL-only surface. Forgejo specialist thread
  parity is not fabricated; the existing platform seam fails loudly there.
- The managed-state query is deliberately bounded to the first 100 threads and
  comments. State records publication failures, but very large PRs may require a
  future paginated query enhancement.
- Native cleanup remains in the existing publishing helper/action path and is
  preserved for Task 13 wiring. Task 12 does not change the legacy action
  dispatcher.
- Native cleanup behavior scripts could not run fully on this host because `jq`
  is not installed. Their structural checks ran, while the platform/verdict/
  approval shell suites above passed natively.
- The two unrelated untracked July 12 documents were not read, edited, staged,
  or committed.
