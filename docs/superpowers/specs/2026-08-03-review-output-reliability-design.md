# Review Output Reliability Design

## Goal

Make specialist review output useful when coverage is incomplete without hiding real findings or silently losing candidate state.

## Design

1. **Candidate identity**: candidate IDs are local model handles, not globally unique identifiers. The controller will namespace duplicate IDs by collector session before adjudication, preserving each candidate instead of rejecting every collision. Existing explicit IDs remain readable in diagnostics, while accepted IDs receive a stable scoped form.
2. **Coverage versus defects**: unresolved mandatory obligations remain recorded in the artifact and continue to make evaluation status incomplete, but generic obligation gaps are aggregated into the handoff instead of becoming one verification note per obligation. Only concrete findings or evidence-backed candidate unknowns create detail notes.
3. **Verdict semantics**: coverage-only incompleteness is represented separately from a defect verdict. The publishing layer must not request changes solely because a mandatory obligation lacks evidence; it publishes the concise handoff/coverage warning and reserves request-changes for accepted actionable findings or concrete verification requests.
4. **Degraded handoff**: a degraded specialist must not discard a validated behavioral change overview. The controller fallback will render a bounded two-to-three-sentence behavioral summary from structured overview facts, with a single coverage caveat.
5. **Diagnostics**: checkpoint finalization records bounded retention diagnostics (parse status, material candidate signal, repair attempted/result, and bounded reason) in the artifact/event journal. Raw model responses remain out of normal logs.

## Safety and compatibility

- Candidate identity normalization is deterministic and only changes IDs when a collision exists.
- Existing artifact fields remain readable; new diagnostics are additive.
- Strict coverage accounting remains available for policy consumers and tests.
- No new model dependency is introduced.

## Verification

Add regression tests for duplicate candidate preservation, aggregated coverage notes, coverage-only verdict behavior, degraded handoff rendering, and checkpoint diagnostic emission. Run the complete specialist-runtime pytest suite and `git diff --check`.
