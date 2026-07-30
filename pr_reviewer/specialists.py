"""Repository topology and file-role helpers for specialist reviews.

Planning, scheduling, report validation, and publication belong to the
specialist session runtime. This module retains only the deterministic
repository-shape helpers consumed by that runtime.
"""

from __future__ import annotations

import fnmatch
import re
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any, Iterable


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
    config = config or {}
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
    changed_contract_facts: dict[str, dict[str, object]] = {}
    for item in pr_files:
        path = _posix(item.get("filename"))
        patch = item.get("patch")
        if not path:
            continue
        change_type = {
            "added": "adds",
            "removed": "removes",
            "modified": "modifies",
            "renamed": "modifies",
            "copied": "adds",
        }.get(str(item.get("status") or "").strip().lower(), "modifies")
        symbols: list[str] = []
        hunk_summaries: list[str] = []
        action_inputs: list[str] = []
        workflow_steps: list[str] = []
        action_section = ""
        for line in patch.splitlines() if isinstance(patch, str) else ():
            hunk_match = re.match(
                r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@\s*(.*)$",
                line,
            )
            if hunk_match and len(hunk_summaries) < 5:
                start = int(hunk_match.group(1))
                count = int(hunk_match.group(2) or "1")
                line_label = (
                    f"new line {start}"
                    if count == 1
                    else f"new lines {start}-{start + max(1, count) - 1}"
                )
                context = re.sub(
                    r"[^A-Za-z0-9 _().,:/+[\]-]+", " ",
                    hunk_match.group(3),
                )
                context = " ".join(context.split())[:120]
                hunk_summaries.append(
                    f"{line_label}: {context}" if context else line_label
                )
            yaml_line = line[1:] if line[:1] in {"+", "-", " "} else line
            if path in {"action.yml", "action.yaml"}:
                section_match = re.match(r"^(inputs|outputs|runs|branding):\s*$", yaml_line)
                if section_match:
                    action_section = section_match.group(1)
            if not line.startswith("+") or line.startswith("+++"):
                continue
            added = line[1:]
            symbol_match = re.match(
                r"\s*(?:async\s+)?(?:def|class|function)\s+([A-Za-z_][A-Za-z0-9_]*)",
                added,
            )
            if symbol_match and symbol_match.group(1) not in symbols:
                symbols.append(symbol_match.group(1))
            if path in {"action.yml", "action.yaml"} and action_section == "inputs":
                input_match = re.match(r"\s{2}([A-Za-z_][A-Za-z0-9_-]*):\s*$", added)
                if input_match and input_match.group(1) not in {
                    "name", "description", "inputs", "outputs", "runs", "branding",
                } and input_match.group(1) not in action_inputs:
                    action_inputs.append(input_match.group(1))
            if path.startswith(".github/workflows/"):
                step_match = re.match(r"\s*-\s+name:\s*(.+?)\s*$", added)
                if step_match:
                    step = re.sub(
                        r"[^A-Za-z0-9 .:/+_-]+", " ",
                        step_match.group(1).strip("'\""),
                    )
                    step = " ".join(step.split())[:120]
                    if step and step not in workflow_steps:
                        workflow_steps.append(step)
        changed_contract_facts[path] = {
            "symbols": symbols[:5],
            "hunk_summaries": hunk_summaries,
            "action_inputs": action_inputs[:5],
            "workflow_steps": workflow_steps[:5],
            "change_type": change_type,
        }
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
        "changed_contract_facts": changed_contract_facts,
    }
