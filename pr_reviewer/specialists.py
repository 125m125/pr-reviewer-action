"""Generic topology and scheduling primitives for specialist PR reviews.

This module deliberately contains no model or GitHub I/O.  It turns repository
facts and strictly-structured planner/configuration data into a bounded review
schedule, validates specialist reports, and prepares publishable candidates.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


BUILTIN_LENSES = {
    "trust-boundary-security",
    "state-lifecycle-concurrency",
    "data-integrity-persistence",
    "protocol-contract-compatibility",
    "background-work-retry-idempotency",
    "resource-boundary-numeric",
    "generated-build-deployment",
    "test-observability",
    "interaction-data-flow",
    "component-correctness",
}

MANIFEST_NAMES = {
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "settings.gradle.kts", "package.json", "pyproject.toml", "setup.py",
    "setup.cfg", "requirements.txt", "Pipfile", "Cargo.toml", "go.mod",
    "Gemfile", "composer.json", "mix.exs", "pubspec.yaml", "Package.swift",
}
LOCKFILE_NAMES = {
    "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock",
    "poetry.lock", "pdm.lock", "pipfile.lock", "cargo.lock", "gemfile.lock",
    "composer.lock", "pubspec.lock",
}

LANGUAGES = {
    ".py": "python", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".cs": "csharp", ".php": "php", ".swift": "swift", ".dart": "dart",
    ".scala": "scala", ".proto": "protobuf", ".sql": "sql",
    ".yaml": "yaml", ".yml": "yaml", ".json": "json", ".xml": "xml",
    ".sh": "shell", ".ps1": "powershell",
}

PRIORITY_SCORE = {"critical": 40, "high": 30, "normal": 20, "low": 10}
SEVERITIES = {"blocker", "major", "minor", "info"}
CATEGORIES = {"bug", "security", "performance", "style", "docs", "question", "other"}


def _posix(value: Any) -> str:
    value = str(value or "").replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value.strip("/")


def _strings(value: Any, *, limit: int = 50, chars: int = 500) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            clean = item.strip()[:chars]
            if clean not in result:
                result.append(clean)
        if len(result) >= limit:
            break
    return result


def _slug(value: Any, fallback: str = "focus") -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (text or fallback)[:80]


def _match(path: str, patterns: Iterable[str]) -> bool:
    path = _posix(path)
    return any(fnmatch.fnmatchcase(path, _posix(pattern)) for pattern in patterns)


def classify_file_roles(path: str) -> list[str]:
    """Return generic, composable roles inferred from a repository path."""
    value = _posix(path)
    low = value.lower()
    name = PurePosixPath(value).name.lower()
    roles: list[str] = []
    if re.search(r"(^|/)(tests?|specs?|e2e)(/|$)|(^|[._-])(test|spec)[._-]", low):
        roles.append("test")
    if name in {"openapi.yaml", "openapi.yml", "openapi.json", "asyncapi.yaml", "asyncapi.yml"} \
            or low.endswith((".proto", ".graphql", ".avsc")) \
            or re.search(r"(^|/)(openapi|asyncapi|schemas?|contracts?|protobuf)(/|$)", low):
        roles.append("schema-contract")
    if re.search(r"(^|/)(generated|gen|dist|build)(/|$)|generated\.", low):
        roles.append("generated")
    if re.search(r"(^|/)(migrations?|flyway|liquibase|alembic)(/|$)|\.sql$", low):
        roles.append("migration")
    if re.search(r"(^|/)(persistence|repositories?|dao|entities|models?)(/|$)", low):
        roles.append("persistence")
    if re.search(r"(^|/)(messaging|queues?|workers?|jobs?|consumers?|producers?)(/|$)|stomp|kafka|rabbit|celery", low):
        roles.append("messaging")
    if re.search(r"(^|/)(deploy|helm|k8s|kubernetes|ansible|terraform|ci)(/|$)|dockerfile|\.github/workflows", low):
        roles.append("deployment")
    if name in MANIFEST_NAMES or name in LOCKFILE_NAMES or name.endswith((".lock", "lock.json")):
        roles.append("build-manifest")
    if re.search(r"(^|/)(config|configuration|settings)(/|$)|\.(ini|toml|properties)$", low):
        roles.append("configuration")
    if low.endswith((".md", ".adoc", ".rst", ".txt")) or re.search(r"(^|/)docs?(/|$)", low):
        roles.append("documentation")
    if re.search(r"(^|/)(auth|security|keycloak|identity)(/|$)|oauth|oidc|jwt", low):
        roles.append("trust-boundary")
    suffix = PurePosixPath(value).suffix.lower()
    if suffix in LANGUAGES and not {"documentation", "build-manifest"}.intersection(roles):
        roles.append("implementation")
    return list(dict.fromkeys(roles or ["other"]))


def discover_component_roots(tracked_paths: Iterable[str]) -> list[str]:
    roots: set[str] = set()
    for raw in tracked_paths:
        path = PurePosixPath(_posix(raw))
        if path.name in MANIFEST_NAMES:
            parent = str(path.parent)
            roots.add("" if parent == "." else parent)
    return sorted(roots, key=lambda item: (item.count("/"), item))


def _component_for(path: str, roots: list[str]) -> str:
    value = _posix(path)
    matches = [root for root in roots if root and (value == root or value.startswith(root + "/"))]
    if matches:
        return max(matches, key=len)
    if roots == [""]:
        return ""
    first = value.split("/", 1)[0] if "/" in value else ""
    return first


def build_topology(
    pr_files: list[dict[str, Any]],
    classification: dict[str, Any] | None,
    tracked_paths: Iterable[str],
    config: dict[str, Any] | None = None,
    workspace_paths: Iterable[str] | None = None,
) -> dict[str, Any]:
    classification = classification or {}
    config = config or empty_config()
    changed = [_posix(item.get("filename")) for item in pr_files if item.get("filename")]
    tracked = [_posix(path) for path in tracked_paths]
    present = set(tracked) | {_posix(path) for path in (workspace_paths or [])}
    roots = discover_component_roots(tracked)
    configured = config.get("components", [])
    components: dict[str, dict[str, Any]] = {}
    path_component: dict[str, str] = {}

    for path in changed:
        configured_component = next(
            (item for item in configured if _match(path, item.get("paths", []))), None
        )
        root = _component_for(path, roots)
        component_id = (
            configured_component["id"] if configured_component else _slug(root or "repository", "repository")
        )
        path_component[path] = component_id
        entry = components.setdefault(component_id, {
            "id": component_id,
            "root": root,
            "changed_files": [],
            "languages": [],
            "file_roles": [],
            "responsibilities": [],
            "related_components": [],
            "contracts": [],
            "invariants": [],
            "configured": bool(configured_component),
        })
        entry["changed_files"].append(path)
        suffix = PurePosixPath(path).suffix.lower()
        language = LANGUAGES.get(suffix)
        if language and language not in entry["languages"]:
            entry["languages"].append(language)
        for role in classify_file_roles(path):
            if role not in entry["file_roles"]:
                entry["file_roles"].append(role)
        if configured_component:
            for field in ("responsibilities", "related_components", "contracts", "invariants"):
                entry[field] = _strings(configured_component.get(field))

    relationships: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for component in components.values():
        for target in component["related_components"]:
            key = (component["id"], target, "configured")
            if key not in seen:
                relationships.append({"source": key[0], "target": key[1], "reason": key[2]})
                seen.add(key)

    contract_components = [
        item for item in components.values() if "schema-contract" in item["file_roles"]
    ]
    if contract_components and len(components) > 1:
        consumer_roles = {"implementation", "messaging", "persistence", "trust-boundary"}
        for contract in contract_components:
            contract_names = set(contract.get("contracts", []))
            for target_id, target in components.items():
                if target_id == contract["id"]:
                    continue
                shared_identity = contract_names.intersection(target.get("contracts", []))
                changed_consumer = consumer_roles.intersection(target["file_roles"])
                if not shared_identity and not changed_consumer:
                    continue
                reason = "shared contract identity" if shared_identity else "changed contract consumer/producer"
                key = (contract["id"], target_id, reason)
                if key not in seen:
                    relationships.append({"source": key[0], "target": key[1], "reason": key[2]})
                    seen.add(key)

    all_roles = sorted({role for item in components.values() for role in item["file_roles"]})
    all_languages = sorted({lang for item in components.values() for lang in item["languages"]})
    available_role_paths: dict[str, list[str]] = defaultdict(list)
    for path in tracked:
        for role in classify_file_roles(path):
            if len(available_role_paths[role]) < 25:
                available_role_paths[role].append(path)
    generated_artifacts = []
    configured_artifacts = config.get("generated_artifacts", [])
    if configured_artifacts:
        candidates = configured_artifacts
    else:
        sources = [path for path in tracked if "schema-contract" in classify_file_roles(path)]
        manifests = [path for path in tracked if "build-manifest" in classify_file_roles(path)]
        candidates = [{
            "id": f"generated-{_slug(PurePosixPath(source).stem)}",
            "source_of_truth": [source],
            "generator_config": manifests[:10],
            "output_paths": ["target/generated-sources/**", "build/generated/**", "src/generated/**"],
        } for source in sources[:10]]
    for artifact in candidates:
        outputs = artifact.get("output_paths", [])
        available = any(_match(path, outputs) for path in present)
        generated_artifacts.append({
            "id": _slug(artifact.get("id"), "generated-artifact"),
            "availability": "available-in-review-workspace" if available
                            else "not-generated-in-review-workspace",
            "source_of_truth": [_posix(v) for v in artifact.get("source_of_truth", [])][:20],
            "generator_config": [_posix(v) for v in artifact.get("generator_config", [])][:20],
            "output_paths": [_posix(v) for v in outputs][:20],
        })
    return {
        "changed_files": changed,
        "components": list(components.values()),
        "path_components": path_component,
        "file_roles": all_roles,
        "languages": all_languages,
        "relationships": relationships,
        "available_role_paths": dict(available_role_paths),
        "risk_flags": _strings(classification.get("risk_flags")),
        "pr_kind": str(classification.get("pr_kind") or "unknown"),
        "generated_artifacts": generated_artifacts,
    }


def empty_config() -> dict[str, Any]:
    return {"version": 1, "components": [], "recipes": [], "generated_artifacts": [], "exclude": {
        "paths": [], "components": [], "lenses": [], "recipes": [],
    }}


def load_specialist_config(path: str | Path) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.is_file():
        return empty_config()
    data = json.loads(candidate.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("specialist config must be a JSON object with version 1")
    result = empty_config()
    for raw in data.get("components", []):
        if not isinstance(raw, dict) or not raw.get("id"):
            raise ValueError("every specialist component requires an id")
        result["components"].append({
            "id": _slug(raw["id"]),
            "paths": [_posix(v) for v in _strings(raw.get("paths"), limit=100)],
            "responsibilities": _strings(raw.get("responsibilities")),
            "related_components": [_slug(v) for v in _strings(raw.get("related_components"))],
            "contracts": _strings(raw.get("contracts")),
            "invariants": _strings(raw.get("invariants")),
        })
    for raw in data.get("recipes", []):
        if not isinstance(raw, dict) or not raw.get("id"):
            raise ValueError("every specialist recipe requires an id")
        match = raw.get("match") if isinstance(raw.get("match"), dict) else {}
        result["recipes"].append({
            "id": _slug(raw["id"]),
            "match": {
                key: _strings(match.get(key), limit=100)
                for key in ("paths_any", "component_ids_any", "risk_flags_any", "file_roles_any")
                if _strings(match.get(key), limit=100)
            },
            "title": str(raw.get("title") or raw["id"])[:160],
            "objective": str(raw.get("objective") or "Review the matched change for correctness.")[:1000],
            "lenses": [_slug(v) for v in _strings(raw.get("lenses"))],
            "seed_paths": [_posix(v) for v in _strings(raw.get("seed_paths"), limit=100)],
            "related_paths": [_posix(v) for v in _strings(raw.get("related_paths"), limit=100)],
            "invariants": _strings(raw.get("invariants")),
            "expected_evidence": _strings(raw.get("expected_evidence")),
            "priority": _priority(raw.get("priority")),
            "source": "recipe",
        })
    for raw in data.get("generated_artifacts", []):
        if not isinstance(raw, dict) or not raw.get("id"):
            raise ValueError("every generated artifact requires an id")
        result["generated_artifacts"].append({
            "id": _slug(raw["id"]),
            "source_of_truth": [_posix(v) for v in _strings(raw.get("source_of_truth"), limit=50)],
            "generator_config": [_posix(v) for v in _strings(raw.get("generator_config"), limit=50)],
            "output_paths": [_posix(v) for v in _strings(raw.get("output_paths"), limit=50)],
        })
    exclude = data.get("exclude") if isinstance(data.get("exclude"), dict) else {}
    result["exclude"] = {
        "paths": [_posix(v) for v in _strings(exclude.get("paths"), limit=100)],
        "components": [_slug(v) for v in _strings(exclude.get("components"), limit=100)],
        "lenses": [_slug(v) for v in _strings(exclude.get("lenses"), limit=100)],
        "recipes": [_slug(v) for v in _strings(exclude.get("recipes"), limit=100)],
    }
    return result


def _priority(value: Any) -> str:
    candidate = str(value or "normal").strip().lower()
    return candidate if candidate in PRIORITY_SCORE else "normal"


def normalize_focus(raw: Any, *, source: str = "planner", index: int = 0) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = str(raw.get("title") or raw.get("id") or "").strip()[:160]
    objective = str(raw.get("objective") or "").strip()[:1000]
    if not title or not objective:
        return None
    focus_id = _slug(raw.get("id") or title, f"focus-{index + 1}")
    return {
        "id": focus_id,
        "title": title,
        "objective": objective,
        "rationale": str(raw.get("rationale") or "")[:1000],
        "lenses": [_slug(v) for v in _strings(raw.get("lenses"), limit=20)],
        "seed_paths": [_posix(v) for v in _strings(raw.get("seed_paths"), limit=100)],
        "related_paths": [_posix(v) for v in _strings(raw.get("related_paths"), limit=100)],
        "related_symbols": _strings(raw.get("related_symbols"), limit=100, chars=200),
        "invariants": _strings(raw.get("invariants"), limit=50),
        "expected_evidence": _strings(raw.get("expected_evidence"), limit=50, chars=200),
        "priority": _priority(raw.get("priority")),
        "source": source,
        "source_ids": _strings(raw.get("source_ids"), limit=20) or [focus_id],
    }


def validate_planner_plan(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("planner output must be a JSON object")
    focuses = []
    for index, item in enumerate(raw.get("focuses", [])):
        focus = normalize_focus(item, index=index)
        if focus:
            focuses.append(focus)
    if not focuses:
        raise ValueError("planner output did not contain a valid focus")
    return {
        "summary": str(raw.get("summary") or "")[:2000],
        "focuses": focuses,
        "coverage_notes": _strings(raw.get("coverage_notes"), limit=50),
    }


def _recipe_matches(recipe: dict[str, Any], topology: dict[str, Any]) -> bool:
    match = recipe.get("match", {})
    changed = topology.get("changed_files", [])
    component_ids = {item["id"] for item in topology.get("components", [])}
    values = {
        "paths_any": lambda wanted: any(_match(path, wanted) for path in changed),
        "component_ids_any": lambda wanted: bool(component_ids.intersection(map(_slug, wanted))),
        "risk_flags_any": lambda wanted: bool(set(topology.get("risk_flags", [])).intersection(wanted)),
        "file_roles_any": lambda wanted: bool(set(topology.get("file_roles", [])).intersection(wanted)),
    }
    return all(values[key](wanted) for key, wanted in match.items() if key in values)


def recipe_focuses(config: dict[str, Any], topology: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        normalize_focus(item, source="recipe", index=index)
        for index, item in enumerate(config.get("recipes", []))
        if _recipe_matches(item, topology)
    ]


def deterministic_focuses(topology: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    risk_flags = topology.get("risk_flags", [])
    if risk_flags:
        result.append(normalize_focus({
            "id": "risk-boundaries",
            "title": "Risk and trust-boundary verification",
            "objective": "Trace the concrete behavior behind every deterministic risk flag and look for exploitable or destructive failure paths.",
            "rationale": "Deterministic classification identified: " + ", ".join(risk_flags),
            "lenses": ["trust-boundary-security", "resource-boundary-numeric"],
            "seed_paths": topology.get("changed_files", []),
            "invariants": ["risk checks are supported by concrete implementation evidence"],
            "expected_evidence": ["changed implementation", "callers", "tests"],
            "priority": "critical",
        }, source="deterministic"))

    for component in topology.get("components", []):
        roles = set(component.get("file_roles", []))
        lenses = ["component-correctness", "test-observability"]
        invariants = list(component.get("invariants", []))
        if "messaging" in roles:
            lenses.append("background-work-retry-idempotency")
            invariants.append("asynchronous work has correct acknowledgement, retry, and duplicate behavior")
        if roles.intersection({"persistence", "migration"}):
            lenses.append("data-integrity-persistence")
            invariants.append("writes, reads, schema, and transaction boundaries remain consistent")
        if "schema-contract" in roles:
            lenses.append("protocol-contract-compatibility")
            invariants.append("contract names, argument order, limits, and generated consumers agree")
        if roles.intersection({"deployment", "build-manifest", "generated"}):
            lenses.append("generated-build-deployment")
        result.append(normalize_focus({
            "id": f"component-{component['id']}",
            "title": f"{component['id']} component correctness",
            "objective": "Review the changed behavior in this component and trace material callers, dependencies, failure paths, and tests.",
            "rationale": "Deterministic topology fallback for a changed component.",
            "lenses": lenses,
            "seed_paths": component.get("changed_files", []),
            "related_symbols": component.get("responsibilities", []),
            "invariants": invariants,
            "expected_evidence": ["changed implementation", "material callers or dependencies", "relevant tests"],
            "priority": "normal",
        }, source="deterministic"))

    if len(topology.get("components", [])) > 1 or topology.get("relationships"):
        result.append(normalize_focus({
            "id": "component-interactions",
            "title": "Changed component interactions",
            "objective": "Trace values, identity, units, ordering, lifecycle, and errors across the changed component boundaries.",
            "rationale": "The topology contains multiple changed components or an explicit relationship.",
            "lenses": ["interaction-data-flow", "protocol-contract-compatibility"],
            "seed_paths": topology.get("changed_files", []),
            "invariants": ["the same semantic value keeps its identity, order, unit, limit, and failure meaning across boundaries"],
            "expected_evidence": ["at least two participating components or one component and its contract"],
            "priority": "high",
        }, source="deterministic"))
    return [item for item in result if item]


def apply_exclusions(
    focuses: Iterable[dict[str, Any]], config: dict[str, Any], topology: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    exclude = config.get("exclude", {})
    paths = exclude.get("paths", [])
    components = set(exclude.get("components", []))
    lenses = set(exclude.get("lenses", []))
    recipes = set(exclude.get("recipes", []))
    path_components = topology.get("path_components", {})
    kept: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    for original in focuses:
        focus = dict(original)
        if focus.get("source") == "recipe" and focus.get("id") in recipes:
            applied.append({"focus": focus["id"], "recipe": focus["id"], "dropped": True})
            continue
        removed_lenses = [lens for lens in focus.get("lenses", []) if lens in lenses]
        focus["lenses"] = [lens for lens in focus.get("lenses", []) if lens not in lenses]
        seed = []
        removed_paths = []
        for path in focus.get("seed_paths", []):
            component = path_components.get(path)
            if _match(path, paths) or component in components:
                removed_paths.append(path)
            else:
                seed.append(path)
        focus["seed_paths"] = seed
        if removed_lenses or removed_paths:
            applied.append({"focus": focus["id"], "lenses": removed_lenses, "paths": removed_paths})
        if (original.get("seed_paths") and not seed) or (original.get("lenses") and not focus["lenses"]):
            applied.append({"focus": focus["id"], "dropped": True})
            continue
        kept.append(focus)
    return kept, applied


def schedule_focuses(
    planner: Iterable[dict[str, Any]],
    recipes: Iterable[dict[str, Any]],
    fallback: Iterable[dict[str, Any]],
    config: dict[str, Any],
    topology: dict[str, Any],
    max_passes: int,
) -> dict[str, Any]:
    candidates = [item for item in [*planner, *recipes, *fallback] if item]
    candidates, exclusions = apply_exclusions(candidates, config, topology)
    merged: list[dict[str, Any]] = []
    merge_decisions: list[dict[str, Any]] = []
    for focus in candidates:
        target = next((item for item in merged if _focuses_substantially_overlap(
            item, focus, topology)), None)
        if target is None:
            merged.append(dict(focus))
            continue
        absorbed_id = focus["id"]
        for field, limit in (("lenses", 20), ("seed_paths", 30), ("related_paths", 30),
                             ("related_symbols", 40), ("invariants", 40),
                             ("expected_evidence", 40), ("source_ids", 20)):
            target[field] = list(dict.fromkeys(
                [*target.get(field, []), *focus.get(field, [])]
            ))[:limit]
        if PRIORITY_SCORE[focus["priority"]] > PRIORITY_SCORE[target["priority"]]:
            target["priority"] = focus["priority"]
        target["sources"] = list(dict.fromkeys([
            *target.get("sources", [target.get("source")]), focus.get("source")
        ]))
        merge_decisions.append({
            "kept": target["id"], "merged": absorbed_id,
            "source_ids": target["source_ids"],
            "reason": "substantial shared component/path and investigation ownership",
        })

    selected: list[dict[str, Any]] = []
    remaining = list(merged)
    covered: set[str] = set()
    selection_log: list[dict[str, Any]] = []
    while remaining and len(selected) < max_passes:
        scored = [(_marginal_focus_score(item, topology, covered), item) for item in remaining]
        score, chosen = max(scored, key=lambda pair: (pair[0],
                                                       PRIORITY_SCORE[pair[1]["priority"]],
                                                       pair[1]["source"] == "planner",
                                                       pair[1]["id"]))
        features = _focus_features(chosen, topology)
        chosen = dict(chosen)
        chosen["marginal_coverage_score"] = score
        chosen["coverage_features"] = sorted(features)
        selected.append(chosen)
        newly_covered = features - covered
        covered.update(features)
        remaining.remove(next(item for item in remaining if item["id"] == chosen["id"]))
        selection_log.append({"focus": chosen["id"], "score": score,
                              "new_features": sorted(newly_covered),
                              "reason": "highest marginal uncovered coverage"})

    omitted = []
    for item in remaining:
        candidate = dict(item)
        candidate["marginal_coverage_score"] = _marginal_focus_score(item, topology, covered)
        candidate["omission_reason"] = "pass limit reached after higher marginal coverage focuses"
        omitted.append(candidate)
    return {
        "selected": selected,
        "omitted": omitted,
        "applied_exclusions": exclusions,
        "merge_decisions": merge_decisions,
        "selection_log": selection_log,
    }


_FOCUS_STOP = {"the", "and", "for", "from", "with", "into", "review", "trace",
               "verify", "change", "changed", "behavior", "correctness", "component"}


def _focus_terms(focus: dict[str, Any]) -> set[str]:
    value = " ".join(str(focus.get(field) or "") for field in
                     ("title", "objective", "rationale"))
    return {word for word in re.findall(r"[a-z0-9]+", value.lower())
            if len(word) >= 4 and word not in _FOCUS_STOP}


def _focus_components(focus: dict[str, Any], topology: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for path in topology.get("changed_files", []):
        if _match(path, [*focus.get("seed_paths", []), *focus.get("related_paths", [])]):
            component = topology.get("path_components", {}).get(path)
            if component:
                result.add(component)
    component_ids = {item.get("id") for item in topology.get("components", [])}
    result.update(set(focus.get("related_symbols", [])).intersection(component_ids))
    return result


def _focuses_substantially_overlap(left: dict[str, Any], right: dict[str, Any],
                                    topology: dict[str, Any]) -> bool:
    lenses = set(left.get("lenses", [])) & set(right.get("lenses", []))
    paths = set(left.get("seed_paths", [])) & set(right.get("seed_paths", []))
    components = _focus_components(left, topology) & _focus_components(right, topology)
    symbols = set(left.get("related_symbols", [])) & set(right.get("related_symbols", []))
    terms_a, terms_b = _focus_terms(left), _focus_terms(right)
    term_ratio = len(terms_a & terms_b) / max(1, min(len(terms_a), len(terms_b)))
    invariants_a = {word for value in left.get("invariants", []) for word in re.findall(r"[a-z0-9]+", value.lower())}
    invariants_b = {word for value in right.get("invariants", []) for word in re.findall(r"[a-z0-9]+", value.lower())}
    invariant_overlap = len((invariants_a & invariants_b) - _FOCUS_STOP) >= 2
    # A shared boundary is not enough: distinct persistence identity and
    # protocol propagation ownership remain separate without lens/invariant similarity.
    ownership_overlap = bool(lenses) or term_ratio >= 0.45 or invariant_overlap
    scope_overlap = bool(paths or components or symbols)
    return scope_overlap and ownership_overlap and sum((bool(lenses), bool(paths),
                                                        bool(components), bool(symbols),
                                                        term_ratio >= 0.45,
                                                        invariant_overlap)) >= 3


def _focus_features(focus: dict[str, Any], topology: dict[str, Any]) -> set[str]:
    features = {f"component:{item}" for item in _focus_components(focus, topology)}
    features.update(f"lens:{item}" for item in focus.get("lenses", []))
    features.update(f"invariant:{item}" for item in _focus_terms({
        "title": " ".join(focus.get("invariants", [])), "objective": "", "rationale": ""
    }))
    components = _focus_components(focus, topology)
    for rel in topology.get("relationships", []):
        if rel.get("source") in components or rel.get("target") in components:
            features.add(f"relationship:{rel.get('source')}->{rel.get('target')}")
    for flag in topology.get("risk_flags", []):
        if "trust-boundary-security" in focus.get("lenses", []) or flag.lower() in " ".join(_focus_terms(focus)):
            features.add(f"risk:{flag}")
    if "component-correctness" in focus.get("lenses", []):
        features.add("role:broad-scout")
    if focus.get("source") == "recipe":
        features.add(f"recipe:{focus['id']}")
    return features


def _marginal_focus_score(focus: dict[str, Any], topology: dict[str, Any],
                          covered: set[str]) -> int:
    features = _focus_features(focus, topology)
    new = features - covered
    weights = {"component": 16, "relationship": 15, "lens": 10, "risk": 14,
               "invariant": 5, "recipe": 4, "role": 5}
    score = PRIORITY_SCORE[focus["priority"]]
    score += sum(weights.get(item.split(":", 1)[0], 2) for item in new)
    score -= sum(4 for item in features & covered)
    return score


def normalize_specialist_report(raw: Any, focus: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("specialist report must be a JSON object")
    findings = []
    for item in raw.get("findings", []):
        if not isinstance(item, dict):
            continue
        claim = str(item.get("claim") or item.get("message") or "").strip()[:2000]
        evidence = _strings(item.get("evidence"), limit=20, chars=2000)
        causal = str(item.get("causal_chain") or "").strip()[:2000]
        if not claim or not evidence or not causal:
            continue
        severity = str(item.get("severity") or "info").lower()
        category = str(item.get("category") or "other").lower()
        line = item.get("line")
        findings.append({
            "severity": severity if severity in SEVERITIES else "info",
            "category": category if category in CATEGORIES else "other",
            "file": _posix(item.get("file")) or None,
            "line": line if isinstance(line, int) and line > 0 else None,
            "claim": claim,
            "evidence": evidence,
            "causal_chain": causal,
            "focus_id": focus["id"],
        })
    inspected = [_posix(v) for v in _strings(raw.get("inspected_files"), limit=200)]
    return {
        "domain": str(raw.get("domain") or focus["id"])[:160],
        "completion_status": "complete" if raw.get("completion_status") == "complete" else "incomplete",
        "inspected_files": inspected,
        "unchecked_material_files": [_posix(v) for v in _strings(raw.get("unchecked_material_files"), limit=200)],
        "invariants_checked": _strings(raw.get("invariants_checked"), limit=100),
        "findings": findings,
        "unknowns": _strings(raw.get("unknowns"), limit=100),
    }


def coverage_gaps(focus: dict[str, Any], report: dict[str, Any], topology: dict[str, Any]) -> list[str]:
    gaps: list[str] = []
    inspected = report.get("inspected_files", [])
    for pattern in focus.get("seed_paths", []):
        concrete = [path for path in topology.get("changed_files", []) if _match(path, [pattern])]
        if concrete and not any(path in inspected for path in concrete):
            gaps.append(f"inspect at least one seed file matching {pattern}")
        elif not concrete and pattern in topology.get("changed_files", []) and pattern not in inspected:
            gaps.append(f"inspect seed file {pattern}")
    if "interaction-data-flow" in focus.get("lenses", []):
        components = {
            topology.get("path_components", {}).get(path)
            for path in inspected
            if topology.get("path_components", {}).get(path)
        }
        has_contract = any("schema-contract" in classify_file_roles(path) for path in inspected)
        if len(components) < 2 and not (components and has_contract):
            gaps.append("inspect at least two participating components, or a component and its contract")
    if focus.get("invariants") and not report.get("invariants_checked"):
        gaps.append("record which declared invariants were checked")
    for category in focus.get("expected_evidence", []):
        low = category.lower()
        roles = {role for path in inspected for role in classify_file_roles(path)}
        available = topology.get("available_role_paths", {})
        satisfied = False
        applicable = True
        if "test" in low:
            satisfied = "test" in roles
            applicable = bool(available.get("test"))
        elif "contract" in low or "schema" in low:
            satisfied = "schema-contract" in roles
            applicable = bool(available.get("schema-contract"))
        elif "persist" in low or "repository" in low or "database" in low:
            satisfied = "persistence" in roles or "migration" in roles
            applicable = bool(available.get("persistence") or available.get("migration"))
        elif "message" in low or "worker" in low or "queue" in low:
            satisfied = "messaging" in roles
            applicable = bool(available.get("messaging"))
        elif "deploy" in low or "manifest" in low or "build" in low:
            satisfied = bool(roles.intersection({"deployment", "build-manifest", "generated"}))
            applicable = any(available.get(role) for role in ("deployment", "build-manifest", "generated"))
        elif "caller" in low or "dependenc" in low:
            satisfied = len(inspected) >= 2
            applicable = len(available.get("implementation", [])) >= 2
        elif "implementation" in low:
            satisfied = "implementation" in roles
            applicable = bool(available.get("implementation"))
        else:
            tokens = [token for token in re.findall(r"[a-z0-9]+", low) if len(token) >= 3]
            satisfied = any(any(token in path.lower() for token in tokens) for path in inspected)
            applicable = any(
                any(token in path.lower() for token in tokens)
                for paths in available.values() for path in paths
            ) if tokens else False
        if applicable and not satisfied:
            gaps.append(f"inspect evidence category: {category}")
    return gaps


def parse_diff_changed_lines(diff_text: str) -> dict[str, set[int]]:
    changed: dict[str, set[int]] = defaultdict(set)
    current = ""
    new_line = 0
    for line in diff_text.splitlines():
        header = re.match(r"^\+\+\+ b/(.*)$", line)
        if header:
            current = _posix(header.group(1))
            continue
        hunk = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if not current or line.startswith("\\ No newline"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            changed[current].add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            new_line += 1
    return changed


def validate_candidates(
    reports: Iterable[dict[str, Any]], changed_files: Iterable[str], diff_text: str,
    rejected_keys: set[str] | None = None,
) -> dict[str, Any]:
    changed_set = {_posix(path) for path in changed_files}
    changed_lines = parse_diff_changed_lines(diff_text)
    rejected_keys = rejected_keys or set()
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for report in reports:
        for item in report.get("findings", []):
            key = candidate_key(item)
            reason = ""
            if key in rejected_keys:
                reason = "critic-rejected"
            elif item.get("file") not in changed_set:
                reason = "outside-review-scope"
            elif key in seen:
                reason = "duplicate"
            else:
                root = next((existing for existing in accepted if _same_root_cause(existing, item)), None)
                if root is not None:
                    root["evidence"] = list(dict.fromkeys(root.get("evidence", []) + item.get("evidence", [])))
                    reason = "duplicate-root-cause"
            if reason:
                rejected.append({**item, "validation_reason": reason})
                continue
            seen.add(key)
            line = item.get("line")
            inline = bool(line and line in changed_lines.get(item["file"], set()))
            accepted.append({**item, "inline_eligible": inline})
    return {"accepted": accepted, "rejected": rejected}


def _same_root_cause(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if _posix(left.get("file")) != _posix(right.get("file")):
        return False
    if left.get("line") and left.get("line") == right.get("line"):
        return True
    stop = {"the", "and", "that", "this", "with", "from", "when", "then", "into", "for", "not"}

    def tokens(value: Any) -> set[str]:
        result = set()
        for token in re.findall(r"[a-z0-9]+", str(value or "").lower()):
            if len(token) < 3 or token in stop:
                continue
            if token.startswith("cancel"):
                token = "cancel"
            result.add(token)
        return result

    for field, threshold in (("causal_chain", 0.65), ("claim", 0.7)):
        a, b = tokens(left.get(field)), tokens(right.get(field))
        if a and b and len(a & b) / len(a | b) >= threshold:
            return True
    return False


def candidate_key(item: dict[str, Any]) -> str:
    material = "|".join((
        _posix(item.get("file")),
        str(item.get("line") or ""),
        re.sub(r"\s+", " ", str(item.get("claim") or item.get("message") or "").lower()).strip(),
    ))
    return material


def findings_for_review(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "severity": item["severity"],
        "category": item["category"],
        "file": item.get("file"),
        "line": item.get("line") if item.get("inline_eligible") else None,
        "message": item["claim"],
    } for item in candidates]


def policy_notice(config_path: str, config_changed: bool, exclusions: list[dict[str, Any]]) -> str:
    lines = []
    if config_changed:
        lines.append(f"The specialist policy `{config_path}` is changed by this PR and was used for this review.")
    if exclusions:
        focus_ids = sorted({item.get("focus", "unknown") for item in exclusions})
        lines.append("Authoritative specialist exclusions were applied to: " + ", ".join(f"`{v}`" for v in focus_ids) + ".")
    if not lines:
        return ""
    return "> **Specialist review policy notice:** " + " ".join(lines) + "\n\n"


def dump_json(path: str | Path, value: Any) -> None:
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
