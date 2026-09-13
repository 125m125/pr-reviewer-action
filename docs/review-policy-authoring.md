# Authoring an AI review policy

This is the permanent guide for creating `.github/ai-review-policy.json`, selected
by the action's `review_policy_file` input. It applies to version 2 specialist
reviews. For converting older configurations, see the
[migration guide](migrations/specialist-session-runtime.md).
The executable schema is [policy.py](../pr_reviewer/specialist_runtime/policy.py).

## Start with the repository, not a catalog of technologies

1. Identify changed-code boundaries: entry points, consumers, persistence,
   generated contracts, build/deployment, and relevant tests.
2. Define a small set of components with real repository paths. Describe actual
   responsibilities and contracts, not everything a component might ever do.
3. Add recipes for recurring, consequential review questions. Begin with
   `execution: "coverage"`; isolate work only when there is a concrete reason.
4. Add narrowly triggered coverage rules only for investigations that must not
   be omitted. Explain why each is mandatory.
5. Add conditional evidence requirements and only the source access needed for
   those investigations. Test applicability before spending model time.

Repository rules/prompt files should supply domain conventions and priorities,
not duplicate the runtime's checkpoint, candidate, or tool schemas.
Model names, budgets, credentials, repository API permissions, and CI artifact
settings belong in the workflow/action inputs, not invented policy fields.

## Schema reference and defaults

Use JSON with `"version": 2` and at least one configuration section. Omitted
collection sections are empty. Unknown keys are rejected at the top level and
inside the structured sections below; they are not comments or extension points.
Use unique, stable lowercase-hyphenated IDs: identifiers are slug-normalized.
String arrays must contain nonempty strings. Repository paths must be relative,
without drive letters, leading slashes, or `..` segments.

| Section | Supported fields and behavior |
| --- | --- |
| `components[]` | Required `id`; `paths`, `responsibilities`, `related_components`, `contracts`, `invariants` default to empty arrays. Prefer non-overlapping paths: component lookup uses the first matching configured component. Relationships orient review; they are not a request to audit entire dependent components. |
| `recipes[]` | Required `id`; `title` defaults to the ID; `objective` defaults to a generic correctness review. Set both explicitly. `execution` defaults to `coverage`; `match` defaults to an empty object; `lenses`, `seed_paths`, `related_paths`, `invariants`, `expected_evidence`, `evidence_requirements` default to empty arrays. `priority`: `critical`, `high`, `normal` (default), or `low`; unknown priorities currently fall back to `normal`. Legacy `source` is accepted but not needed. |
| `coverage_rules[]` | Required `id` and nonempty `required_recipe_ids` referencing existing recipes. Put matching filters directly on the rule, not inside `match`. `risk_tier`: `critical`, `high` (default), `normal`, `low`. `unresolved_policy`: `block_when_unresolved` (default) or `record_unknown`. |
| `recipes[].evidence_requirements[]` | Required `id` (unique within recipe) and `category`; optional `when`, `seed_paths`, `related_paths`. `mode`: `required` (default), `optional`, or `one_of:<group>` with lowercase alphanumeric/hyphen group name. See examples below. |
| `sources[]` | Required concrete lowercase DNS `host`. `schemes` must be `["https"]` (default). `include_subdomains` must remain `false` (default; true is currently unsupported). `path_prefixes` default to empty, allowing the whole host: specify narrow absolute URL paths. `classification` defaults to `reference`. Optional `max_age_hours` must be a positive integer. |
| `generated_artifacts[]` | Required `id`; `source_of_truth`, `generator_config`, `output_paths` are repository-path arrays. Describe how outputs are produced; listing an output does not make an absent build artifact available to tools. |
| `verdict_policy` | `blocker_requires_request_changes` and `require_evidence_for_findings` default to true and cannot be disabled. `blocking_severities` defaults to `["blocker", "major"]` and must be a nonempty subset of those values. `request_changes_severities` is a fallback alias; use the canonical field. `high_risk_tiers` defaults to `["critical", "high"]` and must be a nonempty subset of those values. |
| `publishing` | `allowed_modes` is a nonempty array of `comment`, `review_comment`, or `review_verdict`; parser default is `["review_verdict"]`. Set it explicitly to match the workflow. `allow_approve` defaults to false; policy permission alone does not override other approval safeguards. |
| `exclude` | `paths`, `components`, `lenses`, `recipes` default to empty arrays. Exclusions are deliberate coverage suppression, not a remedy for an overly broad trigger. |

An incomplete high-risk investigation is a coverage limitation, not proof of a
defect. Do not equate `block_when_unresolved` with an automatic finding or a
published changes-requested review.

## Matching and scope

Supported filters are `paths_any`, `component_ids_any`, `risk_flags_any`, and
`file_roles_any`. Values within a group are alternatives (OR); all supplied
groups must match (AND). An explicit empty array cannot match. An empty recipe
`match` matches all reviews, whereas a coverage rule without any filter does not
match. Avoid both when you mean a narrow trigger.

Matching uses aggregate review topology and changed paths: a path filter and a
role filter need not match the same file. Component IDs refer to configured or
discovered components. File roles are fixed path heuristics, documented in the
[file-role reference](file-roles.md); arbitrary role strings are not new roles.
Risk flags come from [the deterministic classifier](../pr_reviewer/classifier.py);
check real classification output rather than inventing flag names.

Policy path patterns use Python `fnmatchcase`, not gitignore rules:
case-sensitive, `*` can match slashes, `?` matches one character, and brackets
define character classes. There is no special zero-directory `**/` behavior.
For example, `**/pom.xml` does not match root `pom.xml`; list both when needed.
Negated gitignore patterns do not work; use explicit exclusions.

`seed_paths` suggest where to begin and `related_paths` identify useful supporting
code. Neither independently triggers a recipe or grants external access.
Avoid huge seed lists: point to the contract, relevant implementation/caller,
and focused tests.

## Recipes, required coverage, and cost

| Execution | Meaning | When to choose |
| --- | --- | --- |
| `coverage` | Recipe obligations can be grouped with other compatible review work. | Default for most contracts, state transitions, and tests. |
| `dedicated` | Preserves a recipe-specific assignment instead of folding it into ordinary combined work. | A genuinely separate investigation benefits from focused context. |
| `independent` | Requires independent verification and preserves an independent assignment boundary. | A narrow critical boundary warrants additional independent scrutiny; expect overlap and extra cost. |

These are scheduling constraints, not unlimited budgets or guarantees of
completion. Many dedicated/independent recipes can consume capacity before
ordinary changed-code investigation.

Final obligation accounting is tools-disabled and bounded: at most four targets
per request and forty per exploration period, subject to the existing turn,
context, and deadline budgets. It reuses retained evidence without repeating
scope/seed-path catalogs. A failed checkpoint takes precedence over optional
accounting; unprocessed targets remain pending/unresolved, never assumed covered.
Failed tests do not automatically raise an assignment's scheduling priority.

A matching coverage rule can **force** its required recipe even if the recipe's
own `match` would not select it. Keep their triggers consistent. For example,
forcing a delivery recipe for every `backend/**` edit also forces it for backend
documentation. Use known messaging paths or a justified additional
`file_roles_any: ["messaging"]` filter instead.

Write invariants that can be checked for the matched change: “retry cannot repeat
the same persistent side effect,” not “the whole backend is correct.” Do not
require generated output when only generator inputs are available. Prefer
conditional requirements over long unconditional `expected_evidence` lists.
Use `record_unknown` where incomplete coverage should be recorded without imposing
blocking coverage requirements; do not lower a real security requirement merely
to make a run green.

## Validate before a model run

From a checkout of pr-reviewer-action, use its Python environment to parse the
target JSON through the production parser (no model or network needed):

```python
import json
from pathlib import Path
from pr_reviewer.specialist_runtime.policy import parse_review_policy

path = Path("/absolute/path/to/target/.github/ai-review-policy.json")
policy = parse_review_policy(json.loads(path.read_text(encoding="utf-8")))
print(f"Valid v{policy.version}: {len(policy.recipes)} recipes")
```

Use `parse_review_policy` rather than assuming a missing file will fail:
`load_review_policy` intentionally returns a minimal policy for a missing file.
Successful parsing does not prove glob applicability, available evidence, or
sensible workload.

Prepare a small applicability table for representative changes: documentation
only, one implementation change, one contract change, one workflow change, and
one unrelated component. Record expected matching recipes, forced recipes, and
required evidence. Check positive **and negative** cases. For exact automated
checks, use the existing [coverage tests](../tests/test_specialist_runtime_coverage.py)
as examples. Then run `specialists_evaluate` on a small representative PR and
inspect the artifact/Action summary for assignments, obligations, suppression,
test reports, and tool availability. That mode still consumes model calls but
does not publish.

## Brief for a configuration-generating agent

Copy this with the repository path and action revision you intend to use:

> Inspect this repository and generate or update its version-2 AI review policy.
> Read the pinned review action's policy-authoring guide and file-role reference;
> do not infer schema fields or role meanings from their names.
> Preserve existing user changes. Identify actual components, contracts, test
> locations, generators, and build/deployment boundaries. Use a small number of
> targeted recipes, coverage mode by default, and conditional evidence.
> Explain why every mandatory coverage rule and dedicated/independent recipe is
> needed. Check that forcing rules do not broaden a recipe unintentionally.
> Do not require unavailable generated output or assume every test can run.
> Propose narrow official-source permissions separately; never grant broad
> website/repository access silently. Keep workflow inputs and repository
> guidance separate from policy and do not reproduce runtime model schemas.
> Validate the JSON with the action's production parser and check representative
> positive/negative applicability cases. Deliver the policy changes, necessary
> workflow/rules changes, rationale, unresolved assumptions, and validation
> results. Do not commit or push unless requested.

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
it does not authorize GitHub repository tools. `gh_api` defaults to metadata for
the repository under review. If a changed workflow pins an action or other
dependency from another repository, explicitly list only the reviewed remote
repositories:

```yaml
tool_allowed_gh_api_repos: "125m125/pr-reviewer-action"
```

Do not use `*` unless unrestricted repository metadata access is an intentional
trust decision. A specifically named repository entry permits safe read-only
metadata through `gh_api` and UTF-8 source text through `read_remote_file`;
the wildcard never grants source-text access. The latter requires an exact
immutable commit SHA, rejects the repository currently under review, and rejects
binary content. Generic `gh_api` rejects repository-content and Git-blob
endpoints so base64 payloads never enter the model as accidental source text.
Remote text retrieval uses raw GitHub content after checking metadata size;
files over 8 MiB are rejected before content download, with a transfer cap as
a second guard. This limit is separate from model-context/excerpt limits and
cannot be bypassed with pagination. There is no base64 fallback.
Use `read_file` or `read_pr_diff` for the current repository. Response byte caps,
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
      "when": {"file_roles_any": ["generated"]},
      "mode": "optional"
    }
  ]
}
```

Every populated `when` group must match; values within one group use `any`
semantics. The permanent [file-role reference](file-roles.md) documents all
`file_roles_any` values, exact detection rules, and limitations.
A coverage rule may force the recipe and raise its risk tier, but it
does not bypass a requirement's `when`. Modes are `required`, `optional`, and
`one_of:<group>`; one matching evidence category satisfies a `one_of` group.
