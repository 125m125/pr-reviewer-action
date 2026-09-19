# Authoring an AI review policy

This is the permanent guide for creating `.github/ai-review-policy.json`, selected
by the action's `review_policy_file` input. It applies to version 3 specialist
reviews. For converting older configurations, see the
[migration guide](migrations/specialist-session-runtime.md).
The executable schema is [policy.py](../pr_reviewer/specialist_runtime/policy.py).

## Quick start: one owner

Create `.github/ai-review-policy.json` with this complete policy:

```json
{
  "version": 3,
  "components": [
    {
      "id": "repository",
      "paths": ["*"],
      "responsibilities": ["Review changed behavior and its relevant callers and tests"]
    }
  ],
  "publishing": {"allowed_modes": ["review_comment"], "allow_approve": false}
}
```

Keep `review_policy_file: .github/ai-review-policy.json` and begin with
`review_strategy: specialists_evaluate`. Validate locally using the command
below before running the workflow. A small repository needs no boundaries or
recipes initially. Add them when concrete ownership or compatibility questions
justify them, not to enumerate every file.

## Owners and shared boundaries

A component is an investigation owner, not a language. Two Java services can be
two owners; multiple languages can be one coherent owner. Shared contracts do
not need an extra specialist by default. When no component owns a changed
contract path, its configured `contract_change_owner` owns that change.
Unmatched changed files enter one `repository-remainder` owner and remain
visible as missing ownership configuration.

A boundary activates when a changed path matches `contract_paths` or explicit
`endpoint_paths`; configured relationships alone are orientation. Local checks
start for all configured participants, but unchanged participants do not each
spawn a session: the primary investigation owner can inspect them. A relevant
successful lookup and brief reason can support "not affected"; exhaustive
negative proof is not required.

Local boundary scope contains only changed contracts and that participant's
endpoint matches. Other component behavior remains separate. A fresh bounded,
tools-disabled evaluator compares retained local assessments and actual source
excerpts, not only covered labels. It records supported, insufficient evidence,
or a potential contradiction for the stated question. Missing/truncated evidence
leaves the combined requirement incomplete; targeted leads may be scheduled,
but neither an automatic finding nor a new specialist is implied. New material
evidence invalidates stale support; evaluator costs share current limits.

## movieHRdb-style ownership example

This complete starting example demonstrates application owners and shared
contracts without declaring every generated API as a separate owner. Adapt paths
to the actual generator layout. Sources and publishing authority stay explicit.

```json
{
  "version": 3,
  "components": [
    {"id": "backend", "paths": ["movieHRdb-backend/**"], "responsibilities": ["HTTP and message producers, application behavior"]},
    {"id": "worker", "paths": ["movieHRdb-pythonworker/**"], "responsibilities": ["Message consumers and background persistence"]},
    {"id": "frontend", "paths": ["movieHRdb-frontend/**"], "responsibilities": ["HTTP consumption and reactive state"]},
    {"id": "database", "paths": ["movieHRdb-database/**"], "responsibilities": ["Migrations and database behavior"]},
    {"id": "deployment", "paths": [".github/workflows/**", "ci/**", "movieHRdb-ansible/**"], "responsibilities": ["Build and runtime artifact delivery"]}
  ],
  "boundaries": [
    {
      "id": "http-api", "contract_paths": ["movieHRdb-openapi/**"],
      "participants": ["backend", "frontend"], "contract_change_owner": "backend",
      "objective": "Compare changed HTTP request and response meaning in producers and consumers."
    },
    {
      "id": "worker-messages", "contract_paths": ["movieHRdb-asyncapi/**", "movieHRdb-protobuf-backend/**"],
      "participants": ["backend", "worker"], "contract_change_owner": "backend",
      "endpoint_paths": {"backend": ["movieHRdb-backend/**/messaging/**"], "worker": ["movieHRdb-pythonworker/**/messaging/**"]},
      "objective": "Compare message identity, destination, payload, and failure semantics."
    },
    {
      "id": "persistence", "contract_paths": ["movieHRdb-database/src/main/resources/db/changelog/**"],
      "participants": ["database", "backend", "worker"], "contract_change_owner": "database",
      "objective": "Check changed schema and its actual reads and writes preserve data meaning."
    }
  ],
  "recipes": [
    {
      "id": "reactive-state", "execution": "integrated",
      "match": {"component_ids_any": ["frontend"]},
      "objective": "Check loading termination, cancellation, and stale state.",
      "expected_evidence": ["changed state transitions", "relevant tests"]
    }
  ],
  "publishing": {"allowed_modes": ["review_comment"], "allow_approve": false}
}
```

## Start with the repository, not a catalog of technologies

1. Identify changed-code boundaries: entry points, consumers, persistence,
   generated contracts, build/deployment, and relevant tests.
2. Define a small set of components with real repository paths. Describe actual
   responsibilities and contracts, not everything a component might ever do.
3. Add recipes for recurring, consequential review questions. Begin with
   `execution: "integrated"`; isolate work only when there is a concrete reason.
4. Add narrowly triggered coverage rules only for investigations that must not
   be omitted. Explain why each is mandatory.
5. Add conditional evidence requirements and only the source access needed for
   those investigations. Test applicability before spending model time.

Repository rules/prompt files should supply domain conventions and priorities,
not duplicate the runtime's checkpoint, candidate, or tool schemas.
Model names, budgets, credentials, repository API permissions, and CI artifact
settings belong in the workflow/action inputs, not invented policy fields.

## Schema reference and defaults

Use JSON with `"version": 3` and at least one explicit owner in `components`. A missing policy file or version 1/2 fails before model calls; there is no legacy execution fallback. Omitted
collection sections are empty. Unknown keys are rejected at the top level and
inside the structured sections below; they are not comments or extension points.
Use unique, stable lowercase-hyphenated IDs: identifiers are slug-normalized.
String arrays must contain nonempty strings. Repository paths must be relative,
without drive letters, leading slashes, or `..` segments.

| Section | Supported fields and behavior |
| --- | --- |
| `components[]` | Required `id`; `paths`, `responsibilities`, `related_components`, `contracts`, `invariants` default to empty arrays. Use non-overlapping owner paths, or list all matching IDs in `ownership_precedence`; the first listed owner wins. An actual ambiguous match without that ordering is rejected. Relationships orient review; they are not a request to audit entire dependent components. |
| `ownership_precedence` | Ordered component IDs used only when changed paths overlap. Does not grant access or add work. |
| `boundaries[]` | Required `id`, `contract_paths`, `participants`, `contract_change_owner`, `objective`; optional `endpoint_paths` maps participant IDs to path globs. All participants and the contract-change owner must be declared components; the owner must be a participant. |
| `recipes[]` | Required `id`; `title` defaults to the ID; `objective` defaults to a generic correctness review. Set both explicitly. `execution` defaults to `integrated`; `match` defaults to an empty object; `lenses`, `seed_paths`, `related_paths`, `invariants`, `expected_evidence`, `evidence_requirements` default to empty arrays. `priority`: `critical`, `high`, `normal` (default), or `low`; unknown priorities currently fall back to `normal`. Legacy `source` is accepted but not needed. |
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
role filter need not match the same file. Component IDs refer to configured owners or the visible `repository-remainder` fallback. File roles are fixed path heuristics, documented in the
[file-role reference](file-roles.md); arbitrary role strings are not new roles.
Risk flags come from [the deterministic classifier](../pr_reviewer/classifier.py);
check real classification output rather than inventing flag names.
Integrated guidance is then matched against each owner's changed paths and roles;
a match elsewhere in the repository does not require every owner to perform it.

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
| `integrated` | Adds objectives, invariants, and evidence guidance to affected owner work. | Default for contracts, state transitions, and tests. |
| `independent` | Requires independent verification and preserves an independent assignment boundary. | A narrow critical boundary warrants additional independent scrutiny; expect overlap and extra cost. |

These are scheduling constraints, not unlimited budgets or guarantees of
completion. Many independent recipes can consume capacity before
ordinary changed-code investigation.

One initial assignment owns each affected component's changed paths. A group assessment
names assessed paths and remaining gaps; a read or a covered status alone does not
complete the whole inventory. Unassessed paths and unresolved behavior remain
schedulable. `expected_evidence` is guidance; explicit `evidence_requirements`
are checked within the question rather than creating one job per category.
Failed tests do not automatically raise an assignment's scheduling priority.

There is no initial planner model call. Specialists can request one-level subset
delegation using `request_delegation`. The controller admits only feasible owned
work while leaving substantive work with the parent; accepted work is queued
with other leads, not guaranteed immediate execution. Children cannot delegate.
Unscheduled owners/delegations remain incomplete. The negotiator schedules gaps
and useful continuations within the existing shared limits, not a fixed round count.

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

Both `parse_review_policy` and `load_review_policy` validate v3; the loader rejects a missing policy with quick-start guidance.
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

> Inspect this repository and generate or update its version-3 AI review policy.
> Read the pinned review action's policy-authoring guide and file-role reference;
> do not infer schema fields or role meanings from their names.
> Preserve existing user changes. Identify actual components, contracts, test
> locations, generators, and build/deployment boundaries. Use a small number of
> targeted integrated recipes and conditional evidence. Model owners by repository responsibilities, not language; declare shared contract boundaries separately.
> Explain why every mandatory coverage rule and independent recipe is
> needed. Check that forcing rules do not broaden a recipe unintentionally.
> Do not require unavailable generated output or assume every test can run.
> Propose narrow official-source permissions separately; never grant broad
> website/repository access silently. Keep workflow inputs and repository
> guidance separate from policy and do not reproduce runtime model schemas.
> Validate the JSON with the action's production parser and check representative
> positive/negative applicability cases. Deliver the policy changes, necessary
> workflow/rules changes, rationale, unresolved assumptions, and validation
> results. Do not commit or push unless requested.

## Complete version-3 policy example

This JSON uses only fields accepted by the version-3 parser. Every populated
recipe `match` group must match; values within a group are alternatives. The
recipes show ordinary `integrated` guidance and explicit `independent` verification.

```json
{
  "version": 3,
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
      "execution": "integrated",
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
      "execution": "integrated",
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
  "execution": "integrated",
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
