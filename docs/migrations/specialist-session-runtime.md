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
- Near the provider context limit, the runtime requests a compact working-memory
  checkpoint and then resumes the same specialist. Tool access is disabled for
  checkpoint and repair turns. It is explicitly re-enabled for exploration.
  Model checkpoints carry working memory and candidate deltas; controller-owned
  coverage and evidence metadata remain authoritative and are not repeated by
  the model.
- Specialists retain concrete defects immediately with `report_candidate` and
  receive short session-local handles (`C1`, `C2`, ...). If later evidence
  disproves a candidate, they use `withdraw_candidate` with the handle and a
  reason. Submission runs a cheap proof preflight; malformed proof is rejected
  immediately with bounded `repair_hints` and a retained lead so the specialist
  can retry while the evidence is still in context. Do not ask repository
  prompts to invent candidate IDs or repeat full candidate objects in every
  checkpoint.
- Every obligation-resolution proposal also carries a small defect assessment:
  `none_observed`, up to three independently validated `candidate_drafts`, or a
  concrete `needs_followup` lead. This keeps defect recognition next to the
  evidence instead of relying on a late whole-session recall pass. Surviving
  leads are retained through compaction and receive at most one bounded,
  tools-disabled synthesis turn during finalization.
- Deterministic assignments are balanced across the configured session
  capacity (targeting roughly six ordinary obligations per specialist when
  capacity permits). Assignment orientation ranks directly scoped code and
  seed paths ahead of broad documentation scopes; dedicated and independent
  recipe assignments remain isolated.
- The current-head version-2 policy derives deterministic coverage obligations.
  Recipes record whether work is `coverage`, `dedicated`, or `independent`, so
  the runtime can account for every selected and omitted obligation.
- Web access is controlled by the validated policy's `sources` rules. A changed
  policy or allowlist is not trusted until validation succeeds; an invalid
  policy produces a constrained/degraded result rather than broader access.
- The published handoff is deliberately sparse: it gives a human the verdict,
  a behavioral change overview, what the AI reviewed, and useful human focus.
  Detailed evidence, access requests, and findings
  remain in resolvable notes instead of duplicating every finding in the sticky
  summary.
- Direct budgets bound the whole run, per-session turns, read-only tool calls,
  recovery, concurrency, and the absolute deadline. The artifact records the
  budget and event accounting, policy result, head SHA, and publication state.
- CI test results can be supplied as a repository-local, immutable-head-bound
  JSON manifest with `specialist_test_results_file`. The controller seeds each
  case as typed `test-result` evidence; specialists query it with
  `read_test_results` using a name substring or regular expression. The tool
  never executes tests or downloads arbitrary artifacts.

## Migration inputs

The table is intentionally machine-readable: its defaults must match
[`action.yml`](../../action.yml). “Large multilingual” means a repository with
multiple languages/components and a provider that has been tested at the stated
context window. Keep the recommended sequential baseline until deterministic
replay and provider capacity have been demonstrated.

<!-- specialist-runtime-input-table -->
| Input | Lifecycle | Default | Large multilingual recommendation | Why |
|---|---|---|---|---|
| `review_strategy` | added | `single` | Begin with `specialists_evaluate`, then use `specialists` with `publish_review_comment: "true"` | Evaluate without publishing before enabling the handoff; the strategy alone never publishes. |
| `review_policy_file` | added | `.github/ai-review-policy.json` | Keep the default and commit a version-2 policy | The validated current-head policy selects work and limits sources/publishing. |
| `review_diff_priority_file` | added | `.github/ai-review-diff-priorities.json` | Keep the default; add the file only when project-specific ordering or quotas improve large-diff orientation | Rules only reorder or quota paths already present in the immutable changed-file manifest. Missing or invalid files safely use built-in priorities. |
| `specialist_test_results_file` | added |  | Set to a CI-produced path such as `ci/test-results.json` when available | Makes actual test outcomes searchable by every specialist and provides admissible evidence for behavioral-test claims without allowing arbitrary test execution. |
| `ai_max_tokens` | retained | `8192` | `8192` for the tested local Qwen baseline | Leaves enough room for reasoning-heavy structured roles and checkpoint repair. |
| `specialist_review_deadline_sec` | added | `7200` | `7200` initially; raise only after measured runs need it | This is the absolute run deadline, including finalization and artifact production. |
| `specialist_phase_shares` | added | `{"planning":10,"initial":60,"followup":20,"finalization":10}` | Keep the default before tuning from artifacts | The four percentages must total 100 and prevent one phase consuming the run. |
| `specialist_concurrency` | added | `1` | `1` | Sequential execution is the reproducible baseline; raise only after confirming provider capacity and deterministic replay behavior. |
| `specialist_max_sessions` | added | `8` | `8` | Bounds initial specialist assignments. |
| `specialist_max_followup_sessions` | added | `2` | `2` | Bounds reassignment/critic follow-up. |
| `specialist_max_model_turns_per_session` | added | `64` | `64` | Caps lifetime turns for each logical session, including recovery. |
| `specialist_max_tool_calls_per_session` | added | `128` | `128` | Allows multi-call evidence turns without exhausting tools before the 64-turn lifetime bound; actual calls remain controller-accounted. |
| `specialist_max_total_model_turns` | added | `320` | `320` | Bounds total provider turns across all admitted specialists so raising the session cap does not multiply review cost. |
| `specialist_max_total_tool_calls` | added | `640` | `640` | Bounds total repository calls while preserving the recommended two-tool-calls-per-model-turn ratio. |
| `specialist_max_recoveries_per_session` | added | `1` | `1` | Allows one bounded reconstruction without endless retrying. |
| `specialist_config_file` | deprecated | `.github/ai-review-specialists.json` | Retain only while translating version-1 recipes | One-release compatibility alias; `review_policy_file` is the version-2 authority. |
| `specialist_max_initial_passes` | deprecated | `6` | Replace with `specialist_max_sessions: "8"` | Legacy alias, not the version-2 session limit. |
| `specialist_max_followup_passes` | deprecated | `2` | Replace with `specialist_max_followup_sessions: "2"` | Legacy alias, not the version-2 follow-up limit. |
| `specialist_max_tool_calls_per_pass` | deprecated | `128` | Replace with `specialist_max_tool_calls_per_session: "128"` | Legacy alias; the new limit is lifetime-per-session. |
| `specialist_tool_mode` | retained | `native_loop` | `native_loop` | Uses durable read-only specialist sessions; `packet` is deprecated. |
| `specialist_planner_max_tool_calls` | deprecated | `2` | Remove it; use `specialist_max_tool_calls_per_session` for evidence gathering | The planner role does not expose tools, so this compatibility input is a no-op and warns when customized. |
| `specialist_planner_max_tokens` | retained | `2048` | `8192` for reasoning-heavy local models | The smaller default is suitable for concise hosted models; Qwen may otherwise spend the response entirely on reasoning before emitting JSON. |
| `specialist_planner_model` | retained |  | Leave blank to inherit `ai_model` initially | A separate planner model is an optional capacity/quality tuning point. |
| `specialist_model` | retained |  | Leave blank to inherit `ai_model` initially | A separate worker model is optional after the baseline is stable. |
| `specialist_critic_model` | retained |  | Leave blank to inherit `specialist_model`, then `ai_model` | Avoids introducing a second provider variable during migration. |
| `specialist_aggregator_model` | retained |  | Leave blank to inherit `ai_model` | Candidate ranking is bounded; tune only from artifacts. |
| `specialist_pass_timeout_sec` | retained | `600` | `600` | Bounds an individual model request within the global deadline. |
| `specialist_max_tokens` | retained | `4096` | `8192` for the tested local Qwen baseline | Provides room for bounded structured checkpoints and repairs without changing the lifetime turn limit. |
| `specialist_recovery_max_tokens` | retained | `2048` | `2048` | Bounds the first reconstructed specialist model turn after a recovery. |
| `specialist_max_conversation_tokens` | retained | `96000` | `60000` for a 75000-token local window | Keeps ordinary transcript pressure below the provider window while preserving checkpoint/output headroom. |
| `specialist_temperature` | retained | `0.0` | `0.0` | Keeps exploration deterministic while replay behavior is established. |
| `model_context_tokens` | retained |  | Set the provider's actual served window; use `75000` for the tested local Qwen configuration | Derives corpus/diff and admission budgets from the real context window. Never copy a model's advertised maximum when the server is configured lower. |
| `specialist_structured_chat_template_kwargs` | added |  | `{"enable_thinking":false}` for llama.cpp-compatible Qwen servers; otherwise leave blank | Applies provider-specific chat-template options only to no-tool structured roles so exploration can retain reasoning while checkpoints spend their output on JSON. Providers that reject unknown request fields must leave it empty. |
| `system_prompt_file` | retained |  | `.github/ai-review-prompt.md` | Stores repository conventions alongside the code being reviewed. |
| `system_prompt_mode` | changed | `replace` | `append` | Preserves the action-owned specialist protocol and appends repository conventions. |
| `specialist_stream_watchdog` | retained | `true` | `true` | Stops repeated streamed blocks and permits one compact recovery. |
| `specialist_max_truncation_continuations` | deprecated | `2` | Remove it | Durable sessions do not issue truncation-continuation turns; they checkpoint and preserve bounded unknowns instead. |
| `specialist_planner_max_context_bytes` | retained | `60000` | `60000` | Limits context given to the planning scout before tool exploration. |
| `specialist_packet_max_bytes` | deprecated | `90000` | Remove it | Packet mode has been removed; durable sessions ignore this compatibility input and warn when it is customized. |
| `publish_review_comment` | retained | `false` | `"true"` when publishing | Enables managed publication for the selected publish mode. |
| `publish_mode` | changed |  | `review_comment` | The empty default is an omission sentinel: `single` resolves to `comment`; specialist strategies resolve to `review_comment`. An explicit `comment` remains authoritative and stays a sticky comment. |
| `inline_findings` | retained | `false` | Keep `false` until line-anchor noise is acceptable | Detailed notes remain the primary evidence surface. |
<!-- /specialist-runtime-input-table -->

The `publish_mode` default is intentionally empty, not `comment`. Do not copy an
older static `comment` default into a specialist workflow: omission means
`review_comment` for `specialists` and `specialists_evaluate`; writing
`publish_mode: comment` deliberately requests the sticky-comment behavior.

### CI test-results manifest

The optional file is JSON and must be bound to the reviewed head. It may contain
either a top-level `tests` array or named `reports`:

```json
{
  "repository": "owner/repository",
  "head_sha": "<40-to-64-hex-head-sha>",
  "reports": [{
    "name": "pytest",
    "workflow": "validate",
    "job": "tests",
    "tests": [{
      "name": "tests.test_notes::test_request_changes",
      "status": "failed",
      "file": "tests/test_notes.py",
      "line": 12,
      "message": "expected REQUEST_CHANGES"
    }]
  }]
}
```

Have the validation workflow write this normalized file (or convert its JUnit
output before the review job) and pass its repository-relative path as the
input. A specialist can then call `read_test_results` with `name_contains` or
`name_regex`, optionally filtering by status. Source inspection alone is never
treated as a test execution result.

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

1. `.github/workflows/ai-review.yml`: use a reviewed immutable action pin. The
   tested local baseline below pins
   `125m125/pr-reviewer-action@9091b940f9f64081dcf64070b71f2a3552e36318`.
   Grant `contents: read`
   and `pull-requests: write` for `review_comment`; native modes need the same
   PR-write permission. Check out the PR head with `fetch-depth: 0`.
2. `.github/ai-review-rules.md`: repository-visible standards and constraints.
   It is suitable for conventions, but does not grant web access or replace the
   version-2 policy.
3. `.github/ai-review-specialists.json`: migration-only compatibility input.
   Preserve an existing version-1 file while translating components/recipes,
   then remove it after version-2 output is established. Fresh version-2
   adopters should not create this file.
4. `.github/ai-review-policy.json`: the version-2 current-head policy. It owns
   component/recipe obligations, allowed documentation sources, generated
   artifacts, and any narrowing publishing/risk policy.
5. `.github/ai-review-prompt.md`: concise repository addendum. Set
   `system_prompt_file` to this path and `system_prompt_mode: append`; do not
   copy the bundled specialist protocol into it.
   Do not redefine checkpoint JSON, candidate IDs, or summarizer schemas here;
   those are action-owned protocols. Repository guidance should describe
   project behavior, trust boundaries, and practical review priorities.
6. `.github/ai-review-diff-priorities.json`: optional project-specific ordering
   for large diffs. Omit it for a small initial test and use built-in ordering,
   or add narrow glob rules when documentation, contracts, configuration, or
   language-specific entry points should be read before normal source. It never
   expands the changed-file boundary.

Example optional priority file:

```json
{
  "rules": [
    {"glob": "docs/**/*.md", "priority": 5, "max_bytes": 40000},
    {"glob": "src/**/*.py", "priority": 15},
    {"glob": "**/*_generated.*", "priority": 90, "max_bytes": 8000}
  ]
}
```

Lower numbers are selected first. Replace the example source glob with the
project's actual source roots. Do not add lockfiles merely to mention them: the
built-in rules already place common lockfiles after normal files.

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

### After: tested local-Qwen version-2 baseline

```yaml
name: AI PR Review
on:
  pull_request:
    # A maintainer explicitly requests a review with the ai-review label.
    types: [labeled]

permissions:
  contents: read
  pull-requests: write

jobs:
  review:
    if: >-
      github.event.label.name == 'ai-review' &&
      github.event.pull_request.draft == false &&
      github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
          ref: ${{ github.event.pull_request.head.sha }}
      - uses: 125m125/pr-reviewer-action@9091b940f9f64081dcf64070b71f2a3552e36318
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          ai_base_url: ${{ vars.LM_STUDIO_BASE_URL }}
          ai_api_format: openai
          ai_model: qwen/qwen3.6-35b-a3b
          ai_api_key: ${{ secrets.LM_STUDIO_API_KEY }}
          ai_response_format: json_schema
          ai_reasoning_effort: ""
          ai_verdict_reasoning_effort: none
          ai_stream: "true"
          ai_max_tokens: "8192"
          ai_request_timeout_sec: "1200"

          # First run without publishing; switch to specialists after inspecting
          # the structured artifact and model-server logs.
          review_strategy: specialists_evaluate
          review_policy_file: .github/ai-review-policy.json
          review_diff_priority_file: .github/ai-review-diff-priorities.json
          standards_file: .github/ai-review-rules.md
          system_prompt_file: .github/ai-review-prompt.md
          system_prompt_mode: append
          review_scope: full
          model_context_tokens: "75000"
          specialist_review_deadline_sec: "7200"
          specialist_concurrency: "1"
          specialist_max_sessions: "12"
          specialist_max_followup_sessions: "2"
          specialist_max_model_turns_per_session: "64"
          specialist_max_tool_calls_per_session: "128"
          specialist_max_total_model_turns: "320"
          specialist_max_total_tool_calls: "640"
          specialist_max_recoveries_per_session: "1"
          specialist_planner_max_tokens: "8192"
          specialist_max_tokens: "8192"
          specialist_pass_timeout_sec: "1200"
          specialist_recovery_max_tokens: "4096"
          specialist_max_conversation_tokens: "60000"
          specialist_structured_chat_template_kwargs: '{"enable_thinking":false}'
          publish_review_comment: "false"
          publish_mode: review_comment
```

The `75000` context value is the served window of the tested local Qwen setup,
not a universal constant. Replace it with the other provider's real configured
window. After a successful evaluation, change `review_strategy` to `specialists`
and `publish_review_comment` to `"true"`. Raise `specialist_concurrency` only
after confirming provider capacity and deterministic replay behavior.

For large reviews, `specialist_max_sessions: "12"` is a focus ceiling, not a
budget multiplier. The controller groups compatible atomic obligations into
bounded review families and distributes the shared 320-turn/640-tool-call lease
by risk. Keep tool calls above model turns because one native model response can
request multiple related reads. Lower the global pair together when review time
must be reduced; do not reduce only the tool lease or specialists may lose their
main continuation signal.

Specialists can batch up to eight related changed paths with
`read_pr_diff(paths=[...])`; the old single `path` form remains valid. Each path
is retained as separate evidence under one shared response cap. Compacted
evidence recovery now requires an exact controller-provided `target` plus one of
`candidate_support`, `obligation_resolution`, or `contradiction_check`. It is
not a general evidence browsing tool, and repeating the same recovery without a
candidate or obligation state change is rejected as no progress.

Repository source redaction preserves dynamic expressions such as
`{api_token}`, `$TOKEN`, `${TOKEN}`, and `%TOKEN%`. Literal credentials are
still replaced with `[REDACTED_VALUE]`, and artifact metadata labels that as a
controller-applied source redaction rather than application behavior.

## Downstream adaptation checklist

Change these repository-specific values:

1. Select a runner that can reach the configured LM Studio endpoint, and map
   `LM_STUDIO_BASE_URL` and `LM_STUDIO_API_KEY` to that project's trusted
   repository variables/secrets.
2. Replace `model_context_tokens` only when the served model/window differs.
   Use the server's configured value, not the model card maximum.
3. Rewrite `.github/ai-review-policy.json` components, relationships, recipes,
   generated artifacts, and official-documentation `sources` for the project.
   Unknown authors or domains require human review before allowlisting.
4. Rewrite `.github/ai-review-rules.md` and `.github/ai-review-prompt.md` with
   the project's architecture, generated-code boundaries, tests, and review
   priorities. Keep the prompt short and repository-specific.
5. Either omit `.github/ai-review-diff-priorities.json` for the first small PR,
   or replace its example globs with real documentation, configuration, build,
   schema, test, source, generated, and lockfile paths.

Keep these values initially:

- the immutable action SHA, `system_prompt_mode: append`, `review_scope: full`,
  sequential specialist execution, 64 lifetime turns, 128 lifetime tool calls,
  one recovery, and the 8,192-token local-Qwen output ceilings;
- `specialists_evaluate` plus disabled publication for the first run, followed
  by artifact/log inspection before enabling `specialists` publication;
- the manual `ai-review` label gate, current-branch policy review, read-only
  `contents` permission, narrowly scoped `pull-requests: write`, and fork
  exclusion until the trust boundaries have been reviewed for that project.

Do not copy `.github/ai-review-specialists.json` into a fresh version-2 project,
do not broaden source hosts merely because search returned them, and do not
replace the bundled specialist prompt with the repository addendum.

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

### Authorize external GitHub repositories separately

The review policy `sources` list controls ordinary HTTPS discovery and fetches;
it does not authorize the `gh_api` tool. GitHub API access defaults to the
repository under review. If a changed workflow pins an action or other dependency
from another repository, explicitly list only the reviewed repositories:

```yaml
tool_allowed_gh_api_repos: "125m125/pr-reviewer-action"
```

Do not use `*` unless unrestricted repository discovery is an intentional trust
decision. A repository entry still permits only the action's globally safe,
read-only endpoint prefixes and denied path segments; response byte caps,
deadlines, and session tool-call budgets remain enforced. Granting an entry does
not preload that repository, its history, or its full diff into model context.

When a specialist requests a repository that is not listed, the runtime does not
fetch it. Instead it records a typed repository-access request containing the
repository, exact API endpoint and revision when available, related obligation,
controller-derived purpose, optional bounded specialist context, and the denial
reason. The sticky handoff shows only the number of open requests; the detailed
request lives in the structured artifact and, for review publishing modes, a
resolvable general note. A human can then review the repository/authors and add
the narrow allowlist entry on the current branch before manually rerunning the
review.

## Make evidence requirements conditional

`expected_evidence` remains supported, but every entry is unconditional once its
recipe runs. Use it only when every matched change genuinely requires every
listed category. For broad components or risk rules, prefer
`evidence_requirements`:

```json
{
  "id": "runtime-delivery",
  "title": "Runtime delivery",
  "objective": "Trace changed build and delivery behavior.",
  "execution": "dedicated",
  "match": {"component_ids_any": ["review-infrastructure"]},
  "evidence_requirements": [
    {
      "id": "workflow",
      "category": "workflow or deployment",
      "when": {"paths_any": [".github/workflows/**", "ci/**"]},
      "mode": "required"
    },
    {
      "id": "build-manifest",
      "category": "build manifest",
      "when": {
        "paths_any": [
          "pom.xml", "**/pom.xml", "package.json", "**/package.json",
          "build.gradle", "**/build.gradle", "build.gradle.kts",
          "**/build.gradle.kts"
        ]
      },
      "seed_paths": ["pom.xml", "**/pom.xml", "package.json", "**/package.json"],
      "mode": "required"
    },
    {
      "id": "artifact-proof",
      "category": "generated output",
      "when": {"file_roles_any": ["generated-artifact"]},
      "mode": "optional"
    }
  ]
}
```

Every populated `when` group must match; values within one group use `any`
semantics. A coverage rule may force the recipe and raise its risk tier, but it
does not bypass a requirement's `when`. Modes are `required`, `optional`, and
`one_of:<group>`; one matching evidence category satisfies a `one_of` group.

During exploration, specialists receive short handles such as `O1` rather than
internal obligation hashes. The controller-local tools
`explain_obligation`, `get_obligation_status`, and
`propose_obligation_resolution` do not consume repository/web tool-call budget.
They use a separate per-session allowance of 32 bookkeeping calls, so a malformed
or repetitive local-tool loop is still bounded.
Repository reads can be targeted with `targets: ["O1"]`; the result remains
neutral evidence until the controller accepts a semantic conclusion.
Each `propose_obligation_resolution` call must also say whether that fresh
evidence produced no defect indicator, one to three candidate drafts, or a
specific lead that needs follow-up. Candidate drafts are accepted or rejected
independently from both the obligation resolution and their sibling drafts.
Repository prompts should explain domain priorities, not reproduce this schema.

Valid dispositions are `covered`, `not_applicable`, `exhausted`, `blocked`, and
`unresolved`. Only `unresolved` names concrete novel next actions. Once an action
has been attempted, the controller does not offer the same resume again. Tools
are disabled during checkpoint turns, so `obligation_updates` is retained only
as a compact compatibility/emergency fallback; accepted interactive state does
not need to be repeated in checkpoints.

## Reliability corrections for current-runtime adopters

- Candidate IDs are specialist-local handles. Do not assume that a model ID such
  as `c1` is globally unique; the controller scopes collisions before critic
  adjudication and retains the original ID in artifact dispositions.
- Missing mandatory high-risk coverage is reported as an incomplete `notice`,
  not as `request_changes`, when there is no evidence-backed finding. The
  aggregate coverage warning belongs in the handoff; it is not emitted once per
  obligation as a detail note.
- A specialist run with incomplete coverage still publishes its validated
  specialist artifacts and bypasses the generic whole-PR model path. Consumers
  should distinguish `evaluation_status: incomplete` from `degraded` and
  `complete`.
- Checkpoint recovery diagnostics are bounded and structured in the artifact
  event journal. They describe parse/repair status and candidate-retention
  signals; raw model responses are intentionally excluded.
- Context-pressure checkpoints are compact model-owned deltas. The model emits
  working summary, completed steps, candidate updates/new candidates, unknowns,
  and next actions; it must not reproduce the controller's coverage ledger,
  obligation statuses, or evidence metadata. Empty candidate arrays are valid
  and do not imply degradation. A malformed response is repaired once before a
  bounded deterministic projection is used.
- Structurally valid checkpoints are accepted in parts. Durable working memory
  and valid obligation/candidate changes are retained even when another proposed
  change fails controller validation. The controller then requests one small,
  tools-disabled correction containing only the rejected obligation or candidate
  changes; it does not ask the model to regenerate the full checkpoint.
- After that focused correction, the controller appends an authoritative receipt
  with the current pending obligations and active candidate state. Rejected
  obligation resolutions remain unresolved, rejected new candidates remain
  inactive, and rejected withdrawals or supersessions preserve the candidate's
  prior state. The receipt also says whether tools will be re-enabled when the
  durable specialist resumes.
- Coverage is not evidence-seeking at all costs. An unchanged seed file can
  explain a contract but does not automatically cover changed behavior. Closed
  not-applicable/exhausted/blocked obligations remain auditable in the artifact;
  they do not create detail comments or a request-changes verdict without a
  concrete accepted finding.
- A surviving `needs_followup` defect lead is compact controller-owned memory,
  not a verification-request comment. At finalization the runtime performs one
  bounded synthesis over only those leads and their retained evidence. A failed
  or inconclusive synthesis leaves the lead visible in the artifact; it does
  not manufacture a finding or trigger a generic scan of all prior evidence.

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
