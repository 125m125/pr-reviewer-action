# Defect-First Specialist Review Design

## Goal

Make specialist reviews retain concrete defects as soon as they are found,
while reducing obligation bookkeeping, assignment dilution, checkpoint schema
failures, and summary-role token waste.

## Candidate lifecycle

Exploration advertises two controller-owned tools. `report_candidate` accepts a
candidate without a model-generated identifier and returns a short session-local
handle such as `C1`. `withdraw_candidate` accepts one handle created by the same
session plus a reason and optional retained evidence IDs. Withdrawal removes the
candidate from the active set but retains an audited status entry. Silence never
withdraws a candidate, and changing a claim means withdrawing the old handle and
reporting a replacement.

Checkpoints preserve active candidate state implicitly. Candidate updates remain
a recovery compatibility path, not the normal reporting mechanism. The critic
receives active candidates only.

## Assignments and changed context

The deterministic plan keeps isolated recipe assignments intact and bounds
ordinary assignments to six obligations. It splits oversized ordinary groups
before scheduling and never relies on the optional planner for validity.

Each assignment receives changed context ordered by its own obligation scope and
seed paths. Relevant changed implementation and test paths precede broad shared
documentation. The context reports assignment-local omissions rather than taking
the repository's first twelve changed paths.

## Checkpoints and diagnostics

Checkpoint obligation updates use the same fields as
`propose_obligation_resolution`: `target`, `disposition`, `reason`,
`evidence_ids`, and `next_actions`. The tolerant parser normalizes the safe
near-misses `status` to `disposition` and `notes` or `conclusion` to `reason`,
and supplies empty evidence/next-action arrays. Controller validation remains
authoritative.

Accepted tool decisions are not repeated. Compaction checkpoints still require
non-empty working memory. Every invalid checkpoint diagnostic records the exact
bounded validation reason used for repair.

## Summary roles

The change summarizer returns one overview and at most five behavioral change
groups. Its prompt states the numeric limit. The deterministic fallback uses the
same behavior-oriented bound.

The handoff summarizer returns only `ai_reviewed_summary` and `human_focus`.
Paths, components, and obligation provenance remain controller-owned. A validated
AI-reviewed sentence does not require a path embedded in its prose.

## Verification

Focused tests cover candidate create/withdraw ownership, assignment splitting and
local context order, checkpoint normalization and diagnostics, and both summary
contracts. Existing session, assignment, controller, publishing, migration, and
workflow suites remain green apart from documented Windows-only harness failures.
