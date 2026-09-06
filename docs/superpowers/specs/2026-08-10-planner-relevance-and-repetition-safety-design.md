# Planner Relevance and Repetition Safety Design

## Context

The optional specialist planner improves a controller-owned deterministic assignment plan. It does not review code, own coverage, or execute repository tools. A movieHRdb review exposed three coupled problems:

1. The planner inherited general specialist instructions about exploring files with tools even though planner requests advertise no tools. Qwen responded with thousands of textual `<tool_call>` markers.
2. Planner topology projection included `available_role_paths`, a repository-wide sample of concrete paths grouped by role. The sample was duplicated across roles and grew independently of the pull request.
3. Every configured `related_components` edge from a changed component became an interaction obligation, even when the changed file did not affect that relationship. A change to the AI-review workflow therefore produced application deployment obligations for database, identity, backend, and clients.

The planner request in the observed run contained only three changed paths, but its 59 KB payload included about 18 KB of sampled repository paths, 21 obligations, and eight assignments. The malformed response consumed the full output allowance before it could be rejected.

## Goals

- Give the planner enough repository shape to judge assignment cohesion, ordering, splitting, merging, and focus improvements.
- Keep planner input proportional to the changed surface and active obligations rather than total repository size.
- Distinguish repository topology used for orientation from relationships that require review coverage.
- Select useful concrete evidence seeds deterministically before planning.
- Interrupt planner repetition and recover without retaining a large malformed response.
- Preserve the deterministic base plan whenever optional planning fails.

## Non-goals

- Giving the planner repository tools or authority to invent coverage.
- Letting planner output expand permitted paths, alter immutable revisions, remove obligations, or weaken risk policy.
- Replacing specialist repository exploration with planner guesses.
- Requiring every configured component relationship to be reviewed for every change.

## Architecture

The review pipeline will keep four distinct representations.

### 1. Repository index

The controller may retain a complete tracked-path index internally. It supplies deterministic file-role counts, component membership, proximity information, and evidence-path selection. This index is never serialized wholesale into a planner request.

### 2. Coverage topology

Configured components and `related_components` describe repository orientation. A static edge means that two components can interact; it does not by itself create an obligation.

An interaction becomes active when at least one controller-verifiable condition holds:

- both endpoint components contain changed files;
- an active recipe's `seed_paths` or `related_paths` intersect the configured paths of the other component, including recipes required by a matching coverage rule; or
- deterministic changed-file facts identify a cross-boundary contract and the policy maps that contract to the consumer component.

The first implementation will support the first two conditions. Existing recipes and coverage rules remain the explicit mechanism for one-sided contract, persistence, messaging, deployment, and security propagation. Static edges remain available as orientation hints without becoming unresolved coverage.

### 3. Authoritative assignment plan

The deterministic planner base retains exact assignment IDs, obligation IDs, priorities, objectives, lenses, and controller-selected scope or seed paths. This remains the authority if the optional model planner fails or returns no useful transformation.

### 4. Planner projection

The model planner receives only:

- changed paths and bounded change facts;
- the deterministic base assignments;
- compact obligation facts: ID, subject, risk, required evidence, component, explanation, and controller-selected scope or seed paths;
- changed components with configured path globs, responsibilities, contracts, and active relationship edges;
- repository capability summaries such as file-role counts and the components in which those roles occur;
- active recipe identifiers and their configured seed or related globs;
- the model-produced change overview when valid.

It does not receive `available_role_paths`, arbitrary tracked-file samples, inactive relationship expansions, or unchanged generated-artifact inventories. Exact unchanged paths appear only when they are already owned by an obligation's scope or seed hints.

Example capability summary:

```json
{
  "role_availability": {
    "test": {
      "count": 842,
      "component_ids": ["java-backend", "web-client", "mobile-client"]
    },
    "migration": {
      "count": 37,
      "component_ids": ["database"]
    }
  }
}
```

Configured component and recipe globs convey repository shape more reliably than arbitrary concrete filenames.

## Relevant evidence seed selection

`available_role_paths` will no longer mean "the first 25 paths for each role." The controller will compute role counts across the repository and select concrete test seeds by deterministic relevance:

1. changed tests;
2. tests in the same configured component as changed source;
3. tests sharing the nearest module or directory prefix;
4. tests whose normalized name tokens overlap changed source names;
5. tests selected by an active recipe or coverage rule.

Selection is stable under input ordering and bounded. The generic topology test obligation is created only when changed tests or meaningfully relevant test candidates exist; arbitrary repository-wide fallback tests do not create coverage. Recipes may still require test evidence independently.

Specialists retain read-only search tools and may discover better evidence during their own investigation. Planner context is not an exhaustive evidence catalogue.

## Planner role contract

The planner keeps the trusted repository review priorities but receives a final, explicit role override:

- this is assignment planning, not code review;
- repository tools are unavailable;
- do not inspect or request files;
- never emit textual tool-call syntax;
- operate only on the supplied base plan and facts;
- return `{"transformations": [...]}` or an empty transformation list.

The role override explicitly suspends generic exploration/tool instructions for the planner turn. Planner transformations remain advisory and independently validated by the controller.

## Repetition and malformed-output recovery

Planner generation will use streaming when the configured provider stream mode is enabled, allowing the existing watchdog to interrupt a response before it consumes the full output allowance. The watchdog will additionally recognize repeated short textual tool markers and other low-entropy exact token runs; these are not reliably caught by the existing paragraph and multiword-block checks.

A post-response guard provides the same classification when streaming is disabled or a provider buffers responses.

When planner output is repetitive or contains textual tool-call markup:

1. classify the attempt as malformed with a bounded diagnostic;
2. do not append the malformed response body to continuation history;
3. issue at most one small strict retry with reasoning disabled, tools explicitly unavailable, and a reduced output allowance sufficient for transformations;
4. accept only a valid JSON transformation object;
5. otherwise use the unchanged deterministic base plan.

Other incomplete JSON may continue through the existing bounded structured-role recovery, but retained malformed content is capped. Planner failure never degrades specialist coverage or the final verdict by itself.

## Project-policy guidance

movieHRdb should separate repository automation from application delivery because `.github/workflows/**` currently shares the deployment component's six application relationships. A narrow `review-infrastructure` or `repository-automation` component is useful for AI-review and maintenance workflows, while build, publish, and deploy workflows remain in `deployment`.

This configuration refinement prevents false application relationships for maintenance workflows. The runtime changes above are still required: they prevent repository-wide path pollution and unconditional relationship obligations for every other component.

## Diagnostics

Bounded runtime diagnostics will record:

- projected planner bytes;
- changed-path, assignment, obligation, active-relationship, and capability counts;
- omitted topology fields;
- planner attempt mode and output allowance;
- repetition or textual-tool classification and marker count;
- whether malformed content was omitted from retry history;
- retry outcome or deterministic fallback reason.

Prompts, model reasoning, complete responses, secrets, and repository file contents remain out of console logs. Structured artifacts may retain bounded machine-readable diagnostics.

## Testing

Tests will establish these invariants:

- planner projection size does not grow with unrelated tracked files;
- `available_role_paths` and unrelated concrete paths are absent from planner payloads;
- changed paths, obligation-owned scopes/seeds, component globs, role counts, and active relationships remain available;
- a one-sided static component relationship is orientation-only, while both changed endpoints activate it;
- recipe- or coverage-rule-required cross-boundary evidence remains active;
- test seeds prefer changed, same-component, nearby, and name-related tests deterministically;
- repeated `<tool_call>` output triggers the watchdog or post-response guard;
- malformed repeated output is not copied into retry history;
- the strict retry has no tools, reduced output allowance, and an explicit role reminder;
- failed optional planning leaves the deterministic base plan unchanged;
- the movieHRdb-shaped fixture produces bounded relevant assignments rather than repository-wide application interactions for an AI-review workflow change.

## Compatibility

No new required action input is introduced. Existing version-2 policies remain valid. `related_components` becomes orientation metadata unless an activation condition is satisfied; recipes and coverage rules continue to express mandatory one-sided propagation. Artifacts may add bounded counts and relationship activation reasons without removing existing authoritative coverage data.
