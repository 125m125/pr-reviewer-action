# GitHub Repository Access Requests Design

## Purpose

When a specialist requests a safe read-only `gh_api` endpoint for a repository
outside the current allowlist, the denial must reach the human reviewer as a
typed access request. Today only unapproved web-search results take that path;
GitHub API denials remain failed tool evidence and disappear from the handoff.

## Security contract

- Creating a request never authorizes or fetches repository content.
- Repository access remains controlled by current-branch configuration and a
  human-triggered review.
- Wildcard access is never inferred or recommended by the runtime.
- The controller derives the trustworthy purpose from the requested endpoint,
  exact revision when present, specialist assignment, and obligation targets.
- A model may add a short optional purpose, but it is masked, bounded, treated
  as untrusted context, and never used as authorization evidence.
- Existing GitHub API endpoint-prefix, path-segment, token, response-size,
  deadline, and tool-budget restrictions remain unchanged.

## Typed request

Introduce a repository-specific request rather than pretending that an API
repository denial is an ordinary website-discovery result. Its durable fields
are:

- repository (`owner/repo`);
- canonical GitHub API endpoint;
- exact revision when deterministically extractable from a commit endpoint;
- related obligation IDs;
- controller-derived purpose;
- optional bounded model purpose;
- authority reason, such as `repository is not in the GitHub API allowlist`.

The identity/fingerprint uses the repository, endpoint, purpose, and related
obligations. No response body or rejected model prose becomes evidence.

## Tool interface and data flow

The `gh_api` schema accepts an optional `purpose` string. The session strips it
before invoking the external executor, so it cannot affect endpoint validation
or request identity. When execution returns the specific repository-allowlist
denial, the session parses the already validated `owner/repo` from the endpoint,
derives the request, associates it only with explicitly targeted obligations (or
the assignment's current gaps when untargeted), and retains it in controller
state.

Other `gh_api` failures—invalid endpoints, denied path segments, missing tokens,
HTTP failures, and unsupported forge endpoints—remain ordinary failed tool
results. They must not be mislabeled as requests a human can solve by changing
the repository allowlist.

Existing website requests continue to carry their controller-generated purpose.
`web_search` and `web_fetch` may accept the same optional bounded purpose, but
the controller retains the obligation/query/URL-derived baseline and never
trusts model prose alone.

## Publishing

The sticky handoff remains sparse and shows only the total number of open source
and repository access requests, with the existing optional link. In
`review_comment` and `review_verdict` modes, each valid repository request
becomes a resolvable general review note containing:

- repository and exact endpoint/revision;
- why the model attempted the lookup;
- the related review obligation;
- why human authorization is required;
- an explicit statement that no content was retrieved.

The structured artifact keeps the full bounded request. `comment` mode retains
the request in the artifact and handoff count even though it cannot publish a
separate resolvable note.

## Bounded action-pin verification

For endpoints such as
`repos/125m125/pr-reviewer-action/commits/<sha>`, the derived purpose is narrowly
phrased as verifying existence, provenance, metadata, and bounded changed-file
information for the exact pinned action revision. Granting the repository does
not preload its history or diff: normal endpoint allowlists, the 12 KB response
cap (or configured replacement), model context management, and tool-call budget
still apply.

## Tests

Tests prove that:

- a repository-allowlist denial creates one typed request with repository,
  endpoint, revision, obligations, and derived purpose;
- optional model purpose is bounded/masked and never reaches the executor;
- invalid endpoint, path-policy, token, and HTTP failures create no request;
- duplicate denials deduplicate deterministically;
- the controller artifact, handoff count, and detail note retain the request;
- existing website access requests and fork/tool security behavior remain
  unchanged.
