# Authoritative Review Orchestration Design

## Objective

Make review coverage controller-owned and complete before model involvement. Models may improve presentation and grouping, but cannot decide whether mandatory work exists, exclude obligations, invent paths, or determine runtime capacity. Improve finding quality, sticky handoff identity, and the usefulness of the human-review summary.

## Assignment architecture

The controller first builds a valid deterministic base plan from immutable obligations, recipe execution modes, risk tiers, topology scopes, configured concurrency, and session limits. Every assignable mandatory obligation has a primary owner; any genuinely impossible work is recorded explicitly with a controller reason.

The planner receives this base plan plus immutable topology and policy. It may return bounded transformations:

- reorder assignment IDs;
- merge compatible ordinary assignments;
- split an ordinary assignment along existing obligation boundaries;
- select authorized seed/boundary paths;
- add concise objectives and lenses.

The planner may not create or remove obligation IDs, change recipe isolation, change risk-derived priority, estimate runtime turns, or select paths outside immutable scopes. Transformations are applied independently. An invalid transformation is ignored without degrading or discarding the valid base plan. Obligations omitted from a proposal retain their base ownership.

Scheduling uses controller-owned session and deadline budgets. A deterministic scheduling weight may order work, but estimated turns from the model are not a validity gate.

## Candidate consequence authorization

Prompts distinguish evidence of a code change from evidence of a defect. A finding must give a plausible reachable path from changed behavior to a concrete consequence.

Deterministic authorization requires consequence-supporting evidence such as:

- a failing or behaviorally relevant test;
- a violated repository invariant or explicit contract;
- a changed producer and affected consumer;
- a concrete input and reachable failure path;
- contradictory retained evidence.

Evidence that merely confirms the changed line or restates the implementation is insufficient. Such candidates are rejected or converted to an unknown according to existing policy; they are not published as findings.

## Sticky handoff identity

Publishing finds the existing sticky comment by the reserved `<!-- ai-pr-review-specialist-handoff -->` marker among issue comments and updates that exact comment ID. It does not depend on comment ordering or the token actor's “last comment.” Multiple legacy managed handoffs are handled conservatively: update the newest marker-bearing comment and avoid creating another duplicate.

## Human-review handoff

The sticky comment stays concise and contains:

1. **What changed** — two to five short, evidence-backed behavioral descriptions tied to changed paths or retained evidence.
2. **What the AI reviewed** — major contracts and behaviors actually inspected, not a generic component inventory.
3. **Human focus** — at most three useful focus areas derived from accepted findings and material incomplete coverage.

Detailed findings, evidence, unknowns, and verification requests remain in review threads. Generic component lists are fallback-only and bounded. The finalizer may propose summaries, but each item must reference authorized paths/evidence. When finalization degrades, deterministic summaries are derived from changed-file roles and topology signals.

## Safety and compatibility

- Generic OpenAI-compatible/local-model transport remains supported.
- Mandatory obligations cannot be silently excluded by model output.
- Existing dedicated and independent recipe isolation remains authoritative.
- Publication remains fail-safe when artifacts are incomplete.
- No new runtime dependency is introduced.

## Verification

Unit tests cover deterministic ownership, partial/invalid transformation handling, capacity behavior, consequence authorization controls, marker-based sticky updating, summary validation, and degraded deterministic handoff fallback. The dogfood workflow pins the complete implementation commit for a fresh PR run.
