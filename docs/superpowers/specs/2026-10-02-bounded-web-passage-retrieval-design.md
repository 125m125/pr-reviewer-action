# Bounded web passage retrieval

Status: refined design for approval. Search-result routing is committed in
`025b5fa`; passage retrieval is not implemented yet.

## Intended outcome

A specialist with a narrow question can retrieve relevant passages from a large
documentation page without receiving its entire contents or guessing a smaller
chapter URL. Direct calls, result-ID retrieval and delegated summaries use the
same source selection and authorization rules. This is deterministic extraction,
not another model-generated summary.

This standalone design refines the previously recorded idea and settles its
remaining implementation-facing details.

## Approach and non-goals

Extend the existing fetch pipeline with optional literal `search_terms`. Prefer
this over a separate grep tool: it avoids downloading an already-truncated prefix
before requesting the useful part. Preserve actual linked chapter destinations
instead of guessing URLs. Do not introduce a crawler, regex execution, semantic
ranking model, browser rendering, automatic retries across links, or a new cache.

No terms means the existing bounded fetch behavior. Explicit selection enables
a larger bounded download but does not increase the model-facing response cap.
Repository-file retrieval is unchanged; selectors on repository-routed result
IDs return an actionable unsupported-selector error, not a different route.

## Tool contract

- Add optional `search_terms` to `web_fetch` and `web_fetch_search_result`.
  Accept one to eight nonempty strings, at most 128 characters each; match literal
  case-insensitive substrings with OR semantics. Reject malformed or excessive
  input before network activity. An omitted field differs from an empty list.
- The fields are also available when those tools run under
  `delegate_tool_summary`; no second tool schema or separate research loop.
- Terms select content only. They cannot alter the registered URL, host, path,
  repository, revision, credentials or source permissions.
- Same-page fragment selectors are local selectors, never HTTP partial-download
  instructions. Fetch and authorize the fragment-free resource, then select an
  explicitly present HTML heading/anchor. Do not invent Markdown anchor slugs.
  If both a fragment and terms are supplied, search within that selected section.
  An unknown/unsupported anchor returns a clear selection limitation.
- Direct and result-ID fetches use this same fragment handling. Do not globally
  weaken `SourcePolicy` fragment or encoded-delimiter validation.

## Download, time and output limits

For selection requests, allow at most **8 MiB** of raw response bytes, independent
of the output cap. Reuse the transport's bounded read plus one-byte truncation
probe. An incomplete response may still yield useful passages; never label it a
complete document. This is an internal ceiling, not a model-controlled parameter.
The existing remote-file download ceiling is also 8 MiB, but the implementations
and permissions remain independent.

Without a selector, retain existing download behavior. With a selector, retain
the existing request/session deadline, redirect count, public-IP/DNS restrictions,
credential-free transport, MIME checks and redaction. Parsing and selection must
also finish within the operation deadline. No implicit extension of a lease.

The entire returned tool payload must fit its normal response budget, including
selection metadata, links and evidence wrapper. Delegate calls can use their
existing larger source budget, not an unbounded special case. Budget pressure
removes optional context/links or entire passages with explicit omission counts;
it must not leave malformed JSON or silently cut a quoted source line.

## Passage selection

1. Decode the bounded downloaded body. For HTML, preserve heading levels,
   paragraph/preformatted boundaries and real link/anchor associations while
   excluding scripts, styles and other currently ignored content. Markdown and
   plain text retain their source line structure. Preserve preformatted spacing.
2. Redact source text before it becomes selectable evidence. Line references are
   to the normalized, redacted document, **not HTML source lines or repository
   lines**. Hash that normalized downloaded document to identify the coordinate
   space across repeated retrievals.
3. Find matching lines/blocks; count matching lines across the downloaded text,
   but keep at most 256 candidate hit locations for bounded selection work.
   Report the omitted count when this ceiling is reached.
4. Start with matching paragraphs/code blocks and their heading breadcrumbs.
   Share space across distinct matches instead of letting the first large section
   consume everything. Merge overlapping windows and return passages in source
   order. Expand context outward through neighboring blocks and enclosing
   sections only while the payload budget permits.
5. If a block cannot fit, keep a complete-line window around the match. If the
   matching line itself cannot fit, report that omission rather than emit a
   partial line. Do not add synthetic closing fences to quoted source content.

The extraction is a convenience view, not an exhaustive interpretation. No
matches means only that the literal terms did not match the downloaded text;
even a complete download does not establish semantic absence of a behavior.

## Evidence and quotations

Keep `content` as bounded source excerpts with explicit omission separators.
Store passage ranges mapping each returned excerpt to normalized document lines.
Breadcrumbs and omission markers are controller metadata, not quotable source.

Preserve the existing provenance, source URL secrecy, policy identity, retrieval
time and content hash. Add explicit selection metadata:

- `download_truncated`: raw source exceeded the download ceiling;
- `excerpted`: output omits portions of the downloaded document;
- `matched_lines`, `returned_matches`, `omitted_matches`;
- normalized document hash and each returned source-line range;
- selection limitations, including unknown anchors and oversized matching lines.

Keep the existing conservative `truncated` signal true when either the download
or excerpt selection is incomplete. Do not turn a selected passage into proof
that the full page was inspected.

Both normal specialist tool feedback and delegated-summary metadata must retain
these limits. Delegated quote ranges continue to address the supplied retained
text; validate that a quote remains inside a real passage and cannot include
omission markers, cross a gap, or quote a breadcrumb as source. Keep the mapping
to normalized document lines alongside that retained-text coordinate system.

## Real navigation links

Return at most eight relevant links: links in selected context, plus genuine
table-of-contents/chapter links whose labels match the requested terms or selected
heading. Preserve the observed target; never synthesize a chapter path.

Resolve relative links against the final fetched URL. Authorize each destination
through the shared search-result routing rules, without fetching it. Approved
destinations receive session result IDs; denied destinations remain bounded,
content-free metadata. A link on an approved page is not itself authorized.
Opaque destination URLs remain hidden, including fragments and link labels that
repeat sensitive URL payloads. Same-page anchors use local section selection.

Navigation metadata is not evidence. The model chooses whether to retrieve a
link; the controller does not traverse it automatically. Link metadata shares
the response budget with passages and can be omitted first when necessary.
Advertise `web_fetch_search_result` whenever website retrieval is available,
even without a configured search engine, so returned navigation IDs are usable.
This does not enable `web_search` without its configured endpoint or bypass fork
gating. Repository-only search retains its existing capability conditions.

## Integration boundaries

- `web_evidence.py`: bounded fetch, HTML structure, normalized source selection
  and selection metadata. Put the pure selection helper in a small separate
  module if necessary rather than further coupling URL authorization to parsing.
- `tool_executors.py`: argument validation, shared direct/result-ID routing,
  destination registration and complete response-budget enforcement.
- `conversation.py`: one consistent selector contract and short descriptions.
- `evidence.py` / `session.py`: retain passage metadata and propagate it to direct
  feedback, delegated summaries and quote validation. Never accept model-supplied
  evidence authority.
- Native harness and specialist factory: use the same default ceilings and
  deadlines; no additional workflow setting or dependency in this increment.

## Verification

Cover late-document matches; multiple distant matches and overlapping windows;
oversized blocks/lines; HTML heading hierarchy and preformatted text; Markdown
code fences; mixed Unicode and UTF-8 output budgets; no-match results from full
and partial downloads; source redaction; deadline expiry during processing;
authorized/denied/opaque links; same-page/unknown anchors; redirects; unsupported
repository selectors; retained evidence/quote mapping; and direct, result-ID and
delegated equivalence. Include navigation-ID retrieval without a search engine.
Assert no new destination requests during link discovery.

Run the affected suites and full pytest suite. Record the existing Windows-only
baseline failures separately. A real model review is a subsequent evaluation,
not a prerequisite for the deterministic tests.
