# File roles in review policies

`file_roles_any` selects built-in roles inferred deterministically from repository
paths. It does **not** ask an LLM to classify file contents. The implementation is
[`classify_file_roles()`](../pr_reviewer/specialists.py).

Use these roles in recipe `match`, coverage rules, and evidence-requirement `when`
filters. For example:

```json
{
  "paths_any": ["backend/**", "worker/**"],
  "file_roles_any": ["messaging"]
}
```

This requires both a matching changed path and a matching role in the review
topology. Values **within** a filter use OR; separate populated filters use AND.
These are aggregate checks, not a guarantee that the same file matches both
filters. Evidence-requirement `when` controls that requirement's applicability;
it does not itself activate a recipe.

## Available roles

Paths are normalized to forward slashes and lowercased before matching. In the
table, a **segment** is a complete slash-delimited path component; a **substring**
can occur anywhere in the path. Conditions within a row are alternatives. All
matching roles accumulate, except `other`, which is only the fallback.

| Role | Detection rule |
| --- | --- |
| `test` | Segment `test`, `tests`, `spec`, `specs`, or `e2e`; or `test`/`spec` preceded by start-of-path or `.`, `_`, `-` and followed by `.`, `_`, `-`. Thus `tests/Foo.java` and `foo.spec.ts` match, but `FooTest.java` alone does not. |
| `schema-contract` | Basename `openapi.yaml`, `openapi.yml`, `openapi.json`, `asyncapi.yaml`, or `asyncapi.yml`; suffix `.proto`, `.graphql`, or `.avsc`; or segment `openapi`, `asyncapi`, `schema`, `schemas`, `contract`, `contracts`, or `protobuf`. |
| `generated` | Segment `generated`, `gen`, `dist`, or `build`; or substring `generated.`. The role name is **not** `generated-artifact`. |
| `migration` | Segment `migration`, `migrations`, `flyway`, `liquibase`, or `alembic`; or suffix `.sql`. |
| `persistence` | Segment matching `persistence`, `repositories?`, `dao`, `entities`, or `models?`. These are literal regex alternatives: `models?` matches `model`/`models`, while `repositories?` matches `repositorie`/`repositories`, **not** singular `repository`. A filename such as `UserRepository.java` alone does not match. |
| `messaging` | Segment `messaging`, `queue`, `queues`, `worker`, `workers`, `job`, `jobs`, `consumer`, `consumers`, `producer`, or `producers`; or substring `stomp`, `kafka`, `rabbit`, or `celery`. |
| `deployment` | Segment `deploy`, `helm`, `k8s`, `kubernetes`, `ansible`, `terraform`, or `ci`; or substring `dockerfile` or `.github/workflows`. |
| `build-manifest` | Lowercased basename belongs to `MANIFEST_NAMES` or `LOCKFILE_NAMES` in the implementation (listed below); or basename ends with `.lock` or `lock.json`. |
| `configuration` | Segment `config`, `configuration`, or `settings`; or suffix `.ini`, `.toml`, or `.properties`. YAML/JSON and `.env` files are not automatically configuration. |
| `documentation` | Suffix `.md`, `.adoc`, `.rst`, or `.txt`; or segment `doc` or `docs`. |
| `trust-boundary` | Segment `auth`, `security`, `keycloak`, or `identity`; or substring `oauth`, `oidc`, or `jwt`. |
| `implementation` | A recognized suffix (listed below), provided neither `documentation` nor `build-manifest` matched. Can coexist with other roles, including `test`, `generated`, and `schema-contract`. |
| `other` | No other role matched. |

Recognized implementation suffixes:

```text
.py .java .kt .kts .ts .tsx .js .jsx .go .rs .rb .cs .php .swift
.dart .scala .proto .sql .yaml .yml .json .xml .sh .ps1
```

Manifest basenames that currently match the lowercased lookup:

```text
pom.xml build.gradle build.gradle.kts settings.gradle settings.gradle.kts
package.json pyproject.toml setup.py setup.cfg requirements.txt go.mod
composer.json mix.exs pubspec.yaml
```

The constant also contains `Pipfile`, `Cargo.toml`, `Gemfile`, and `Package.swift`,
but those mixed-case entries currently do not match the lowercased lookup. This
documents existing behavior rather than promising classification they do not get.

Lockfile basenames:

```text
package-lock.json npm-shrinkwrap.json pnpm-lock.yaml yarn.lock poetry.lock
pdm.lock pipfile.lock cargo.lock gemfile.lock composer.lock pubspec.lock
```

## Choosing filters

Roles are generic heuristics, not a complete description of your architecture.
For example, `backend/jobs/rebuild.py` receives `messaging` even if it never sends
a message; `backend/OrderListener.java` does not receive that role merely because
its contents consume messages. Unchanged source contents are not inspected to
infer these roles.

Policies reference these built-in names; they cannot redefine their detection
patterns. Prefer explicit `paths_any` or repository-defined `component_ids_any`
when your layout does not fit the heuristics. Do not add a role filter to such a
path-based rule unless you want **both** conditions to be required. Roles influence
automatic review coverage; absence of a role does not prohibit a specialist from
finding a relevant defect.
