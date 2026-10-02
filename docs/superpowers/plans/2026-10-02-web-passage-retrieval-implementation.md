# Bounded Web Passage Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Retrieve relevant, accurately citable passages and real navigation links from large documentation pages without increasing specialist tool-response limits.

**Architecture:** Extend SecureFetcher and the existing direct/result-ID routes. Keep document structure and selection in one pure helper module; retain selection metadata alongside evidence so normal and delegated readers share the same source boundaries. Reuse existing authorization, transport, redaction and deadline machinery.

**Tech Stack:** Python, stdlib HTMLParser/dataclasses/hashlib, existing pytest suite. No new dependency.

**Spec:** `docs/superpowers/specs/2026-10-02-bounded-web-passage-retrieval-design.md` (read completely before execution).

## Global Constraints

- Continue in the existing checkout unless isolation is actually needed. Never fetch, pull or push. Check `.venv` first; use `.venv/Scripts/python.exe`.
- No crawler, browser, regex search, additional model, cache or workflow setting.
- Explicit `search_terms`: 1-8 nonempty strings, maximum 128 characters each; literal case-insensitive OR. Omitted is distinct from empty. Validate before I/O.
- Selector downloads: at most 8 MiB plus the existing one-byte truncation probe. No selector preserves existing download behavior.
- Keep at most 256 hit locations; count and disclose omitted matches. At most eight navigation links. Existing response budgets/deadlines remain authoritative.
- Apply selection even to small documents. Expand parents only under the approved recursive all-child-branches rule, never because spare space exists.
- A selector never widens source/repository permissions. Repository-routed result IDs reject selectors without changing route.
- Quotes address retained text and map to normalized redacted document lines; never HTML/repository line numbers. No quote may cross a gap or quote synthetic metadata.
- Use local synthetic fixtures, not copied full websites. Network checks are a separate, optional evaluation.
- Keep `.tmp-stuff`, evaluation reports and old untracked draft plans out of commits. No pin or rebase in this implementation unless separately requested.

## Review Focus

1. Malformed HTML, skipped heading levels and headings in code must not silently swallow content or invent section ancestry (Task 1).
2. Added context must not become a new match and cascade promotion into unrelated chapters; one oversized early hit must not starve later hits (Task 2).
3. Redirect fragments, relative links and hidden URL reflections must not bypass source checks or expose opaque targets (Task 3).
4. JSON escaping, metadata and a second retention/redaction pass must not invalidate byte bounds, hashes or passage coordinates (Tasks 3-4).
5. A useful result with no search engine, or a partial download containing matches, must remain retrievable and explicitly incomplete (Tasks 3-5).

## File map

- Create `pr_reviewer/specialist_runtime/web_passages.py`: normalization/block hierarchy, deterministic passage selection and source-range mapping; no network/policy logic.
- Modify `pr_reviewer/specialist_runtime/web_evidence.py`: selector-aware SecureFetcher, FetchedEvidence metadata, shared routing of discovered navigation links.
- Modify `pr_reviewer/tool_executors.py`: selector validation/forwarding and complete payload budgeting through both existing fetch routes.
- Modify `pr_reviewer/conversation.py`: tool selector schemas and navigation-ID availability without search.
- Modify `pr_reviewer/specialist_runtime/evidence.py`: retained selection metadata and safe coordinate preservation.
- Modify `pr_reviewer/specialist_runtime/session.py`: visible selection limits, delegated source metadata and quote-range enforcement.
- Inspect `pr_reviewer/specialist_runtime/cli.py` and `scripts/run_tool_harness.py`; change only if their existing shared-executor wiring needs adjustment.
- Create `tests/test_web_passages.py`, `tests/fixtures/web_passages/` structural fixtures and README; extend existing web/search/evidence/session/wiring tests.
- Update the existing tool documentation in `README.md`; do not add another user guide.

## Task 1 — Normalize documents into source blocks and sections

**Files:** new `web_passages.py`, `tests/test_web_passages.py`, `tests/fixtures/web_passages/{README.md,bash-like.html,fixtures-like.html,timeouts-like.html,permissions-like.md,announcement-like.html}`.

**Interfaces:** Add `normalize_document(text: str, mime_type: str, *, check_deadline: Callable[[], None]) -> NormalizedDocument`. The immutable result holds normalized redacted `text`, source blocks, heading ancestry, explicit anchors and observed links. All block/section ranges use inclusive one-based lines in that text. Internal block types stay private to this module. Redaction uses existing shared helpers; no policy decisions here.

- [ ] Write tests `test_normalized_blocks_preserve_source_structure`, `test_heading_hierarchy_skips_levels_without_parsing_code_headings`, `test_malformed_html_keeps_text_and_checks_deadline`: assert preformatted indentation survives; nested list items stay associated; table headers/rows retain separate ranges; scripts/navigation prose are excluded; real navigation links survive; H2 to H4 attaches correctly; fenced `#` is not a heading; a deadline exception stops work.
- [ ] Add small synthetic fixtures with original prose and representative structures. README records inspiration URLs and explicitly says these are not website snapshots. Generate large filler in tests, not giant fixture files.
- [ ] Run `.venv/Scripts/python.exe -m pytest tests/test_web_passages.py -q`; verify failures reflect missing behavior.
- [ ] Implement with stdlib HTMLParser and a small Markdown block scanner (ATX/setext headings, fenced code, ordinary paragraphs/lists/tables). Do not attempt full Markdown rendering or infer arbitrary CSS semantics. Ignore scripts/styles; recognize explicit warning/note containers and preserve their text labels. Normalize then redact before final range assignment. Check the deadline during bounded processing, not only at the end.
- [ ] Rerun that suite; require PASS. Commit only Task 1 files: `Add structured document normalization for web passages`.

## Task 2 — Select passages with bounded context and recursive parent promotion

**Files:** `web_passages.py`, `tests/test_web_passages.py`.

**Interfaces:** Add `select_passages(document: NormalizedDocument, *, search_terms: tuple[str, ...] | None, fragment: str | None, max_bytes: int, check_deadline: Callable[[], None]) -> PassageSelection`. Result supplies `content`, `selection` metadata and bounded observed navigation candidates. Metadata uses `document_hash`, `excerpted`, `matched_lines`, `returned_matches`, `omitted_matches`, `limitations`, `passages`. Each passage maps `start_line/end_line` in returned content to `source_start_line/source_end_line` in the normalized document. Breadcrumbs are separate metadata. `max_bytes` bounds the serialized selection result, not just source characters.

- [ ] Write tests `test_both_matching_siblings_promote_parent_if_it_fits`, `test_partial_other_branch_prevents_grandparent_promotion`, `test_small_document_still_excludes_unmatched_sibling`, `test_expansion_does_not_create_matches`, `test_large_first_hit_does_not_starve_late_match`. Assert exact included/excluded fixture markers and actual source ranges, not just output length.
- [ ] Add parameterized tests for table header plus nonadjacent matching row, list/nested item, admonition label, huge code block, oversized matching line, no headings, unknown anchor, terms restricted by a known anchor, more than 256 hit locations and Unicode/JSON-escaped text. Assert `len(json.dumps(payload, ensure_ascii=False).encode('utf-8')) <= max_bytes`, omitted counts, complete lines and no fabricated closing fence.
- [ ] Run the selection tests and confirm expected failures.
- [ ] Implement fair initial match allocation, overlapping-range merging, then neighboring blocks and recursive parent promotion. A leaf branch needs an actual retained hit; a non-leaf requires each child branch recursively represented. A parent-introduction hit does not represent its children. Promote only if the complete parent fits. Include nearest ancestor introductions opportunistically without crossing unmatched subsections. Finish in source order.
- [ ] Keep at most eight candidate navigation links relevant to selected context or matching chapter labels; do not resolve authorization here. Remove optional context/links before losing represented matches. If minimum metadata cannot fit, return a bounded explicit size error rather than malformed output. No-match is literal only, never semantic absence.
- [ ] Run `tests/test_web_passages.py`; require PASS. Commit: `Select bounded heading-aware web passages`.

## Task 3 — Wire secure retrieval and real navigation IDs

**Files:** `web_evidence.py`, `tool_executors.py`, `conversation.py`, existing web/search/wiring tests; CLI/native harness only if required.

**Interfaces:** Add optional keyword `search_terms: tuple[str, ...] | None = None` to `SecureFetcher.fetch` and forward it through the existing `web_fetch` helper. URL fragments remain part of the existing URL/registered target, not a new target override. Add optional `selection` and `navigation` fields to FetchedEvidence/as_dict. Keep true destination URLs internal until authorization and opaque rendering are complete. Reuse SearchResultRegistry and existing route authorization; factor a shared routing helper from `discover` only if necessary, not a second policy implementation.

- [ ] Write tests for invalid selectors before transport, late manual match after the old prefix limit, selector/no-selector download caps, complete versus interrupted downloads, and deadline expiry during parsing. Assert cap is `8 * 1024 * 1024`, with truncation probe retained; final truncation is `download_truncated or excerpted`.
- [ ] Write routing tests for direct/result-ID equivalence, rejected repository selectors, no-search-engine navigation IDs, relative links resolved against final redirect URL, denied/opaque navigation, and no destination requests during discovery. Assert denied links never receive approved IDs and opaque URL payloads never appear in labels/content/metadata.
- [ ] Run affected tests; confirm missing behavior failures.
- [ ] Validate tool arguments, then split and safely validate a web fragment before authorizing/fetching the fragment-free URL. Resolve only real anchors; handle redirect fragments with the same validation, never by weakening SourcePolicy globally. Preserve existing repository anchors/routes. Keep unsupported anchors explicit rather than silently returning a different section.
- [ ] Separate raw download ceiling from output ceiling in SecureFetcher. Feed the new normalizer/selector only for selection requests; keep ordinary behavior intact. Reuse `_remaining`, transport/DNS/redirect/MIME protections and existing opaque reflection masking.
- [ ] Budget the complete serialized result with provenance, link metadata and any executor wrapper; measure actual UTF-8 JSON bytes. Finalize source ranges/hash before evidence insertion. Never use a trailing generic string clip on this structured result. Reserve space for navigation or omit it; do not mutate retained source after storing it. Test both direct SecureFetcher evidence insertion and executor/session insertion.
- [ ] Add `search_terms` to both tool schemas. Describe literal filtering, incomplete excerpts and the unchanged response cap. Expose `web_fetch_search_result` when web fetching is enabled even without search; preserve fork/repository-only gating. Registered link IDs use the same dispatch as search IDs.
- [ ] Run `.venv/Scripts/python.exe -m pytest tests/test_web_passages.py tests/test_specialist_runtime_web.py tests/test_web_search.py tests/test_search_retrieval.py tests/test_run_native_loop_wiring.py tests/test_specialist_runtime_cli.py -q`; require PASS. Commit: `Expose secure passage retrieval and navigation links`.

## Task 4 — Preserve excerpt evidence and enforce quote boundaries

**Files:** `evidence.py`, `session.py`, evidence/session regression tests.

**Interfaces:** Add optional `selection` metadata to EvidenceRecord using a default so ordinary records remain unchanged. Preserve it through snapshots/artifact serialization/import and direct/session feedback. It is controller-derived; models cannot supply evidence authority. Passage maps follow Task 2. Existing delegated quote requests still use retained-input line numbers, not a new model-facing schema.

- [ ] Write `test_web_selection_survives_evidence_round_trip_and_compaction`, `test_delegated_quotes_cannot_cross_passage_gap`, `test_selected_table_header_and_row_have_distinct_source_ranges`, `test_retention_limit_cannot_leave_stale_selection_ranges`. Assert both actual quote text and source mappings; reject markers/breadcrumbs/gap-spanning ranges while accepting genuine contiguous passages.
- [ ] Add test for two delegated inputs where only one is excerpted. Assert source identity remains per-input, helper receives selection limitations, and visible truncation cannot be downgraded by model output. Include a redaction expansion/multiline replacement fixture to detect coordinate drift.
- [ ] Run targeted tests; confirm failures.
- [ ] Propagate trusted selection metadata through EvidenceStore and session feedback. If a later existing storage cap changes content, adjust retained ranges to actual complete source lines or omit invalid ranges with an explicit limitation; never preserve a false mapping. Ensure evidence identity distinguishes differing document-coordinate maps even if excerpt text is identical.
- [ ] Update `_validated_delegated_summary` and its multi-source handling: validate each quoted interval inside one real passage after translating packed-input offsets; obtain quote text from retained source as today. Include resulting normalized-source coordinates alongside retained coordinates. Do not authorize quoting synthetic content.
- [ ] Run `.venv/Scripts/python.exe -m pytest tests/test_specialist_runtime_evidence.py tests/test_specialist_runtime_session.py tests/test_specialist_runtime_web.py -q`; require PASS. Commit: `Retain web passage provenance through delegated quoting`.

## Task 5 — End-to-end verification and user documentation

**Files:** README tool documentation and existing integration tests; amend affected implementation files only for verified regressions.

- [ ] Add an end-to-end fake-transport test: search/direct page -> late matching section -> authorized chapter ID -> chapter fetch -> delegate quote. Assert no automatic chapter fetch; normal and delegated caps differ only by caller budget; all final payloads are valid bounded JSON with truthful truncation and coordinates.
- [ ] Run that test and prove its assertions catch deliberately removed range/truncation metadata before finalizing it.
- [ ] Document a one-element delegated `tool_requests` example using `web_fetch` with `search_terms`, optional anchors, literal no-match caveat and normalized source-line meaning. Explain parent promotion and that unused budget does not imply more unrelated content.
- [ ] Run `.venv/Scripts/python.exe -m pytest tests/ -q --tb=short` and `git diff --check`. Compare any failures by exact test name with `.tmp-stuff/2026-10-02-hard-fixes-pytest.log` (21 known Windows-only failures); report all new failures and fix them before completion. Do not silently deselect new failures.
- [ ] If network authorization is available, manually check the GNU/Playwright/GitHub examples with approved-source policy, bounded fetch and no model call. Record MIME, selected headings, bytes and truncation; moved/blocked pages are evaluation limitations, not automatic code changes. Offline fixtures remain the required gate.
- [ ] Perform final code review under the normal execution skill, particularly source-policy boundaries, whole-payload size and quote integrity. Commit verified docs/integration changes: `Document and verify bounded web passage retrieval`.

## Execution handoff

Recommend native execution: these five tasks share one extraction/evidence contract and benefit from one implementer holding that contract, followed by an independent final review. No product code has changed while preparing this plan. Review this plan and choose native or subagent-driven execution before implementation.
