# Migrate to the specialist session runtime

This guide is a self-contained handoff for a repository moving from a single
AI review (or the version-1 specialists file) to the version-2 specialist
session runtime. It applies to OpenAI-compatible endpoints as well as hosted
models. Start with `review_strategy: specialists_evaluate` to inspect the
outputs without publishing; switch to `specialists` once the policy and
provider capacity are understood.

## What changes

- A specialist is now a continuous, bounded session. It retains only the
  review state needed across planning, investigation, bounded follow-up, and
  finalization instead of restarting an unrelated whole-PR review.
- The current-head version-2 policy derives deterministic coverage obligations.
  Recipes record whether work is `coverage`, `dedicated`, or `independent`, so
  the runtime can account for every selected and omitted obligation.
- Web access is controlled by the validated policy's `sources` rules. A changed
  policy or allowlist is not trusted until validation succeeds; an invalid
  policy produces a constrained/degraded result rather than broader access.
- The published handoff is deliberately sparse: it gives a human the verdict,
  topics, and review boundary. Detailed evidence, access requests, and findings
  remain in resolvable notes instead of duplicating every finding in the sticky
  summary.
- Direct budgets bound the whole run, per-session turns, read-only tool calls,
  recovery, concurrency, and the absolute deadline. The artifact records the
  budget and event accounting, policy result, head SHA, and publication state.

## Migration inputs

The table is intentionally machine-readable: its defaults must match
[`action.yml`](../../action.yml). “Large multilingual” means a repository with
multiple languages/components and a provider that has been tested at the stated
context window. Keep the recommended sequential baseline until deterministic
replay and provider capacity have been demonstrated.

<!-- specialist-runtime-input-table -->
| Input | Lifecycle | Default | Large multilingual recommendation | Why |
|---|---|---|---|---|
| `review_strategy` | added | `single` | Begin with `specialists_evaluate`, then use `specialists` | Evaluate without publishing before enabling the handoff. |
| `review_policy_file` | added | `.github/ai-review-policy.json` | Keep the default and commit a version-2 policy | The validated current-head policy selects work and limits sources/publishing. |
| `specialist_review_deadline_sec` | added | `7200` | `7200` initially; raise only after measured runs need it | This is the absolute run deadline, including finalization and artifact production. |
| `specialist_phase_shares` | added | `{"planning":10,"initial":60,"followup":20,"finalization":10}` | Keep the default before tuning from artifacts | The four percentages must total 100 and prevent one phase consuming the run. |
| `specialist_concurrency` | added | `1` | `1` | Sequential execution is the reproducible baseline; raise only after confirming provider capacity and deterministic replay behavior. |
| `specialist_max_sessions` | added | `8` | `8` | Bounds initial specialist assignments. |
| `specialist_max_followup_sessions` | added | `2` | `2` | Bounds reassignment/critic follow-up. |
| `specialist_max_model_turns_per_session` | added | `64` | `64` | Caps lifetime turns for each logical session, including recovery. |
| `specialist_max_tool_calls_per_session` | added | `20` | `20` | Caps read-only evidence gathering per session. |
| `specialist_max_recoveries_per_session` | added | `1` | `1` | Allows one bounded reconstruction without endless retrying. |
| `specialist_config_file` | deprecated | `.github/ai-review-specialists.json` | Retain only while translating version-1 recipes | One-release compatibility alias; `review_policy_file` is the version-2 authority. |
| `specialist_max_initial_passes` | deprecated | `6` | Replace with `specialist_max_sessions: "8"` | Legacy alias, not the version-2 session limit. |
| `specialist_max_followup_passes` | deprecated | `2` | Replace with `specialist_max_followup_sessions: "2"` | Legacy alias, not the version-2 follow-up limit. |
| `specialist_max_tool_calls_per_pass` | deprecated | `20` | Replace with `specialist_max_tool_calls_per_session: "20"` | Legacy alias; the new limit is lifetime-per-session. |
| `specialist_tool_mode` | retained | `native_loop` | `native_loop` | Uses durable read-only specialist sessions; `packet` is deprecated. |
| `specialist_planner_max_tool_calls` | retained | `2` | `2` | Keeps the topology/diff planning scout bounded. |
| `specialist_planner_max_tokens` | retained | `2048` | `2048` | Keeps the planning scout concise before specialist work begins. |
| `specialist_planner_model` | retained |  | Leave blank to inherit `ai_model` initially | A separate planner model is an optional capacity/quality tuning point. |
| `specialist_model` | retained |  | Leave blank to inherit `ai_model` initially | A separate worker model is optional after the baseline is stable. |
| `specialist_critic_model` | retained |  | Leave blank to inherit `specialist_model`, then `ai_model` | Avoids introducing a second provider variable during migration. |
| `specialist_aggregator_model` | retained |  | Leave blank to inherit `ai_model` | Candidate ranking is bounded; tune only from artifacts. |
| `specialist_pass_timeout_sec` | retained | `600` | `600` | Bounds an individual model request within the global deadline. |
| `specialist_max_tokens` | retained | `4096` | `4096` | Bounds specialist, critic, and aggregation output. |
| `specialist_recovery_max_tokens` | retained | `2048` | `2048` | Bounds the compact recovery after a repetition watchdog stop. |
| `specialist_max_conversation_tokens` | retained | `96000` | `96000` | Bounds each session transcript separately from model context. |
| `specialist_temperature` | retained | `0.0` | `0.0` | Keeps exploration deterministic while replay behavior is established. |
| `model_context_tokens` | retained |  | Set the provider's actual window, for example `262144` | Derives corpus/diff budgets from the real context window; blank uses the action's context-limit mode. |
| `system_prompt_file` | retained |  | `.github/ai-review-prompt.md` | Stores repository conventions alongside the code being reviewed. |
| `system_prompt_mode` | changed | `replace` | `append` | Preserves the action-owned specialist protocol and appends repository conventions. |
| `specialist_stream_watchdog` | retained | `true` | `true` | Stops repeated streamed blocks and permits one compact recovery. |
| `specialist_max_truncation_continuations` | retained | `2` | `2` | Bounds continuation requests after an output reaches its cap. |
| `specialist_planner_max_context_bytes` | retained | `60000` | `60000` | Limits context given to the planning scout before tool exploration. |
| `specialist_packet_max_bytes` | retained | `90000` | `90000` | One-release packet migration setting; durable sessions ignore it. |
| `publish_review_comment` | retained | `false` | `"true"` when publishing | Enables managed publication for the selected publish mode. |
| `publish_mode` | changed |  | `review_comment` | The empty default is an omission sentinel: `single` resolves to `comment`; specialist strategies resolve to `review_comment`. An explicit `comment` remains authoritative and stays a sticky comment. |
| `inline_findings` | retained | `false` | Keep `false` until line-anchor noise is acceptable | Detailed notes remain the primary evidence surface. |
<!-- /specialist-runtime-input-table -->

The `publish_mode` default is intentionally empty, not `comment`. Do not copy an
older static `comment` default into a specialist workflow: omission means
`review_comment` for `specialists` and `specialists_evaluate`; writing
`publish_mode: comment` deliberately requests the sticky-comment behavior.

## Version-1 to version-2 mapping

Translate the existing `.github/ai-review-specialists.json` into
`.github/ai-review-policy.json` rather than deleting it before the first v2
evaluation. The v1 file remains a compatibility migration input, but the
validated current-head v2 policy is authoritative for obligations, specialist
selection, sources, and publishing.

| Version-1 field | Version-2 field | Translation |
|---|---|---|
| `version: 1` | `version: 2` | Change the version and add v2-only sections as needed. |
| `components` | `components` | Copy each component's `id`, `paths`, responsibilities, relationships, contracts, and invariants. IDs are normalized to slugs; paths must remain repository-relative. |
| `recipes` | `recipes` | Copy recipe IDs, title, objective, `match`, lenses, paths, invariants, expected evidence, and priority. Add `execution`: use `coverage` for normal obligation coverage, `dedicated` for a focused separate examination, or `independent` for a separate corroborating examination. |
| `match` | `match` | Preserve `paths_any`, `component_ids_any`, `risk_flags_any`, and `file_roles_any`. Every populated match group must match; values within a group use `any` semantics. Do not turn separate match groups into alternatives. |
| `exclude` | `exclude` | Copy `paths`, `components`, `lenses`, and `recipes`. Exclusions remain authoritative for scheduling and are disclosed; they do not disable classifier, verdict, or publication guardrails. |
| `generated_artifacts` | `generated_artifacts` | Copy each artifact's `id`, `source_of_truth`, `generator_config`, and `output_paths`. If an output is absent, review the source specification, generator config, handwritten consumers, and tests instead of assuming generated output is evidence. |

Then add the v2-only sections deliberately: `coverage_rules` for deterministic
risk/obligation requirements, `sources` for narrow official-documentation
allowlists, `verdict_policy` for verdict restrictions, and `publishing` for
policy-level narrowing. Recipes are structured review data: they do not grant
commands, arbitrary web hosts, custom models, custom budgets, or full prompt
replacement.

## Repository file checklist

Create or review these files in the consuming repository before enabling
`review_strategy: specialists`:

1. `.github/workflows/ai-review.yml`: use a reviewed action pin (the snippets
   use `misospace/pr-reviewer-action@v1`; production repositories may pin the
   same reviewed release to its immutable commit SHA). Grant `contents: read`
   and `pull-requests: write` for `review_comment`; native modes need the same
   PR-write permission. Check out the PR head with `fetch-depth: 0`.
2. `.github/ai-review-rules.md`: repository-visible standards and constraints.
   It is suitable for conventions, but does not grant web access or replace the
   version-2 policy.
3. `.github/ai-review-specialists.json`: the existing version-1 migration input.
   Preserve it while translating components/recipes, then remove it only after
   version-2 output is established. It is a compatibility fallback, not the
   authoritative version-2 source.
4. `.github/ai-review-policy.json`: the version-2 current-head policy. It owns
   component/recipe obligations, allowed documentation sources, generated
   artifacts, and any narrowing publishing/risk policy.
5. `.github/ai-review-prompt.md`: concise repository addendum. Set
   `system_prompt_file` to this path and `system_prompt_mode: append`; do not
   copy the bundled specialist protocol into it.

### Manual re-review safety

The `ai-review` label triggers a human-initiated review. Before applying it,
inspect the PR's changed `.github/ai-review-policy.json` and its `sources`
allowlist (and any linked rules/prompt changes). Current-head policy is
authoritative only after validation; humans must not use a label to bless an
unreviewed policy or source rule change.

## Workflow conversion

### Before: single OpenAI-compatible review

```yaml
name: AI PR Review
on:
  pull_request:
    types: [opened, reopened, synchronize, ready_for_review]

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          ref: ${{ github.event.pull_request.head.sha }}
      - uses: misospace/pr-reviewer-action@v1
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          ai_base_url: https://api.openai.com/v1
          ai_model: gpt-4.1-mini
          ai_api_key: ${{ secrets.OPENAI_API_KEY }}
          publish_review_comment: "true"
          publish_mode: comment
```

### After: version-2 specialist runtime

```yaml
name: AI PR Review
on:
  pull_request:
    types: [opened, reopened, synchronize, ready_for_review, labeled]

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    if: ${{ !github.event.pull_request.draft }}
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          ref: ${{ github.event.pull_request.head.sha }}
      - uses: misospace/pr-reviewer-action@v1
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          ai_base_url: https://api.openai.com/v1
          ai_model: gpt-4.1-mini
          ai_api_key: ${{ secrets.OPENAI_API_KEY }}
          review_strategy: specialists
          review_policy_file: .github/ai-review-policy.json
          model_context_tokens: "262144"
          specialist_review_deadline_sec: "7200"
          specialist_concurrency: "1"
          system_prompt_file: .github/ai-review-prompt.md
          system_prompt_mode: append
          publish_review_comment: "true"
          publish_mode: review_comment
```

Use `specialists_evaluate` for the first rollout and inspect the artifacts
before changing it to `specialists`. Raise `specialist_concurrency` only after
confirming provider capacity and deterministic replay behavior.

## Complete version-2 policy example

This JSON uses only fields accepted by the version-2 parser. Every populated
recipe `match` group must match; values within a group are alternatives. The
three recipes show the supported execution modes: `coverage`, `dedicated`, and
`independent`.

```json
{
  "version": 2,
  "components": [
    {
      "id": "api",
      "paths": ["services/api/**", "openapi/**"],
      "responsibilities": ["HTTP API and schema"],
      "related_components": ["worker"],
      "contracts": ["OpenAPI request and response compatibility"],
      "invariants": ["authenticated callers cannot cross tenant boundaries"]
    },
    {
      "id": "worker",
      "paths": ["services/worker/**"],
      "responsibilities": ["asynchronous delivery"],
      "related_components": ["api"],
      "contracts": ["durable event payloads"],
      "invariants": ["retries do not create duplicate effects"]
    }
  ],
  "recipes": [
    {
      "id": "api-coverage",
      "title": "API compatibility coverage",
      "objective": "Trace schema, authorization, and consumer compatibility.",
      "execution": "coverage",
      "match": {"component_ids_any": ["api"]},
      "lenses": ["authorization", "backward-compatibility"],
      "seed_paths": ["services/api/**"],
      "related_paths": ["openapi/**", "tests/api/**"],
      "invariants": ["tenant boundary is preserved"],
      "expected_evidence": ["changed endpoint and contract tests"],
      "priority": "high"
    },
    {
      "id": "generated-client",
      "title": "Generated client integrity",
      "objective": "Verify the generator inputs and committed generated output agree.",
      "execution": "dedicated",
      "match": {"paths_any": ["openapi/**", "clients/generated/**"]},
      "lenses": ["generated-artifact"],
      "seed_paths": ["openapi/openapi.yaml"],
      "related_paths": ["clients/generated/**", "scripts/generate-client.sh"],
      "invariants": ["generated client follows the OpenAPI source"],
      "expected_evidence": ["source specification and generated diff"],
      "priority": "normal"
    },
    {
      "id": "worker-delivery",
      "title": "Worker delivery independence",
      "objective": "Independently examine retry and acknowledgement behavior.",
      "execution": "independent",
      "match": {"component_ids_any": ["worker"]},
      "lenses": ["retry", "idempotency"],
      "seed_paths": ["services/worker/**"],
      "related_paths": ["tests/worker/**"],
      "invariants": ["retries do not create duplicate effects"],
      "expected_evidence": ["failure path and worker tests"],
      "priority": "high"
    }
  ],
  "coverage_rules": [
    {"id": "auth-risk", "risk_flags_any": ["auth_changes"], "required_recipe_ids": ["api-coverage"]}
  ],
  "sources": [
    {
      "host": "platform.openai.com",
      "include_subdomains": false,
      "path_prefixes": ["/docs"],
      "classification": "official-documentation",
      "max_age_hours": 720,
      "schemes": ["https"]
    },
    {
      "host": "docs.python.org",
      "include_subdomains": false,
      "path_prefixes": ["/3"],
      "classification": "official-documentation",
      "schemes": ["https"]
    }
  ],
  "generated_artifacts": [
    {
      "id": "openapi-client",
      "source_of_truth": ["openapi/openapi.yaml"],
      "generator_config": ["scripts/generate-client.sh"],
      "output_paths": ["clients/generated/**"]
    }
  ],
  "verdict_policy": {
    "blocker_requires_request_changes": true,
    "require_evidence_for_findings": true
  },
  "publishing": {
    "allowed_modes": ["review_comment"],
    "allow_approve": false
  },
  "exclude": {"paths": ["vendor/**"], "components": [], "lenses": [], "recipes": []}
}
```

The official-documentation rules above are examples, not a broad web permit.
Use concrete lowercase DNS hosts, HTTPS only, and narrow path prefixes. Keep
policy changes in the PR diff so a reviewer can audit them before a manual
re-review label is applied.

## What to expect

| Surface | Expected behavior |
|---|---|
| `review-handoff.md` and PR handoff | A sparse managed comment/native review with the recommendation, change topics, selected coverage boundary, and the outcome—not a repeated list of every finding. `review-handoff.json` is the structured handoff companion. |
| `review-notes.json` | Detailed, resolvable notes including evidence/access requests and normalized findings; use it to investigate or anchor comments. |
| `specialist-review-artifact.json` | Machine-readable schema-versioned run artifact bound to the PR head SHA, with policy validation/result, event/budget accounting, coverage, degradations, and publication readiness. |
| Action outputs | `review_handoff`, `review_notes`, and `specialist_artifact` expose the respective files when specialist mode runs. |
| `specialists_evaluate` | Writes the same artifacts but intentionally does not publish a review. |

## Troubleshooting

| Symptom | Check | Resolution |
|---|---|---|
| No specialist review is published | `review_strategy` and `publish_review_comment` | Use `specialists` and `publish_review_comment: "true"`; `specialists_evaluate` is intentionally non-publishing. |
| A specialist review is a sticky comment | `publish_mode` | Omit it for specialist default `review_comment`, or set `review_comment` explicitly. `comment` is an intentional sticky override. |
| Policy/source access is constrained or degraded | `specialist-review-artifact.json` policy/degradation fields | Validate `.github/ai-review-policy.json`; use only known version-2 keys, concrete lowercase HTTPS hosts, and repository-relative paths. |
| Expected recipe did not run | artifact coverage/omitted obligations and recipe `match` | Check every populated match group and exclusions; add a `coverage`, `dedicated`, or `independent` recipe that matches the changed component/path. |
| Deadline is reached | artifact budget/event accounting | Keep concurrency at `1`, inspect expensive recipes/tool usage, then tune direct budgets or deadline from evidence. |
| Provider overload or nondeterministic results | model logs and repeated artifacts | Keep `specialist_concurrency: "1"`; increase it only after capacity and deterministic replay are confirmed. |
| Native review cannot publish | workflow permissions | Grant `pull-requests: write`; preserve `contents: read` and checkout the PR head. |
| Prompt lost the action protocol | prompt configuration | Use `.github/ai-review-prompt.md` with `system_prompt_mode: append`, not `replace`. |
