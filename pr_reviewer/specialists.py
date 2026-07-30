"""Repository topology and file-role helpers for specialist reviews.

Planning, scheduling, report validation, and publication belong to the
specialist session runtime. This module retains only the deterministic
repository-shape helpers consumed by that runtime.
"""

from __future__ import annotations

import fnmatch
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath
import subprocess
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
_MAX_CHANGE_FACT_PATHS = 500
_MAX_LOCAL_PATCH_BYTES = 32_000
_MAX_CHANGE_ITEMS = 5



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


def _change_type(status: object) -> str:
    return {
        "a": "adds",
        "added": "adds",
        "c": "adds",
        "copied": "adds",
        "d": "removes",
        "removed": "removes",
        "m": "modifies",
        "modified": "modifies",
        "r": "modifies",
        "renamed": "modifies",
        "t": "modifies",
    }.get(str(status or "").strip().lower(), "modifies")


def _clean_fact_text(value: object, *, limit: int = 160) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return " ".join(text.split())[:limit]


def _facts_from_patch(
    path: str,
    status: object,
    patch: object,
    *,
    include_intent: bool = False,
) -> dict[str, object]:
    symbols: list[str] = []
    hunk_summaries: list[str] = []
    action_inputs: list[str] = []
    workflow_steps: list[str] = []
    workflow_keys: list[str] = []
    headings: list[str] = []
    change_excerpts: list[str] = []
    action_section = ""
    lines = patch.splitlines() if isinstance(patch, str) else ()
    for line in lines:
        hunk_match = re.match(
            r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@\s*(.*)$",
            line,
        )
        if hunk_match and len(hunk_summaries) < _MAX_CHANGE_ITEMS:
            start = int(hunk_match.group(1))
            count = int(hunk_match.group(2) or "1")
            if count == 0:
                line_label = (
                    f"deletion-only hunk near new-file line {start} "
                    "(no new lines)"
                )
            elif count == 1:
                line_label = f"new line {start}"
            else:
                line_label = f"new lines {start}-{start + count - 1}"
            context = re.sub(
                r"[^A-Za-z0-9 _().,:/+[\]-]+", " ",
                hunk_match.group(3),
            )
            context = " ".join(context.split())[:120]
            hunk_summaries.append(
                f"{line_label}: {context}" if context else line_label
            )
            if path.endswith(".py"):
                context_symbol = re.match(
                    r"\s*(?:async\s+)?(?:def|class)\s+"
                    r"([A-Za-z_][A-Za-z0-9_]*)",
                    hunk_match.group(3),
                )
                if context_symbol and context_symbol.group(1) not in symbols:
                    symbols.append(context_symbol.group(1))
        yaml_line = line[1:] if line[:1] in {"+", "-", " "} else line
        if path in {"action.yml", "action.yaml"}:
            section_match = re.match(
                r"^(inputs|outputs|runs|branding):\s*$", yaml_line,
            )
            if section_match:
                action_section = section_match.group(1)
        if not line.startswith("+") or line.startswith("+++"):
            continue
        added = line[1:]
        symbol_match = re.match(
            r"\s*(?:async\s+)?(?:def|class|function)\s+"
            r"([A-Za-z_][A-Za-z0-9_]*)",
            added,
        )
        if symbol_match and symbol_match.group(1) not in symbols:
            symbols.append(symbol_match.group(1))
        if path in {"action.yml", "action.yaml"} and action_section == "inputs":
            input_match = re.match(
                r"\s{2}([A-Za-z_][A-Za-z0-9_-]*):\s*$", added,
            )
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
            key_match = re.match(
                r"\s*(?:-\s+)?([A-Za-z_][A-Za-z0-9_-]*):(?:\s|$)",
                added,
            )
            if (
                key_match
                and key_match.group(1) not in workflow_keys
                and len(workflow_keys) < _MAX_CHANGE_ITEMS
            ):
                workflow_keys.append(key_match.group(1))
        if include_intent and path.lower().endswith((".md", ".adoc", ".asciidoc")):
            heading_match = (
                re.match(r"\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", added)
                if path.lower().endswith(".md")
                else re.match(r"={1,6}\s+(.+?)\s*$", added)
            )
            if heading_match:
                heading = _clean_fact_text(heading_match.group(1))
                if heading and heading not in headings:
                    headings.append(heading)
            else:
                excerpt = _clean_fact_text(added)
                if excerpt and excerpt not in change_excerpts:
                    change_excerpts.append(excerpt)
    result: dict[str, object] = {
        "symbols": symbols[:_MAX_CHANGE_ITEMS],
        "hunk_summaries": hunk_summaries[:_MAX_CHANGE_ITEMS],
        "action_inputs": action_inputs[:_MAX_CHANGE_ITEMS],
        "workflow_steps": workflow_steps[:_MAX_CHANGE_ITEMS],
        "change_type": _change_type(status),
    }
    if include_intent:
        result.update({
            "workflow_keys": workflow_keys[:_MAX_CHANGE_ITEMS],
            "headings": headings[:_MAX_CHANGE_ITEMS],
            "change_excerpts": change_excerpts[:_MAX_CHANGE_ITEMS],
        })
    return result


def build_change_facts(
    workspace: Path | str,
    base_sha: str,
    head_sha: str,
    changed_paths: Iterable[str],
) -> dict[str, object]:
    """Build bounded semantic facts from the immutable local review range."""
    if (
        re.fullmatch(r"[0-9a-fA-F]{40,64}", str(base_sha or "")) is None
        or re.fullmatch(r"[0-9a-fA-F]{40,64}", str(head_sha or "")) is None
    ):
        raise ValueError("change facts require full base and head object IDs")
    root = Path(workspace)
    all_paths = tuple(dict.fromkeys(
        path for path in (_posix(item) for item in changed_paths) if path
    ))
    paths = all_paths[:_MAX_CHANGE_FACT_PATHS]
    try:
        status_result = subprocess.run(
            [
                "git", "diff", "--name-status", "--find-renames",
                f"{base_sha}...{head_sha}", "--",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return {
            "facts": {},
            "bounded": True,
            "path_limit": _MAX_CHANGE_FACT_PATHS,
            "included_path_count": 0,
            "omitted_path_count": len(all_paths),
            "failed_path_count": 0,
            "status": "degraded",
            "failures": [{
                "scope": "range",
                "reason": "immutable diff command unavailable",
            }],
        }
    if status_result.returncode != 0:
        return {
            "facts": {},
            "bounded": True,
            "path_limit": _MAX_CHANGE_FACT_PATHS,
            "included_path_count": 0,
            "omitted_path_count": len(all_paths),
            "failed_path_count": 0,
            "status": "degraded",
            "failures": [{
                "scope": "range",
                "reason": "immutable diff range unavailable",
            }],
        }
    local_status: dict[str, str] = {}
    for line in status_result.stdout.splitlines():
        columns = line.split("\t")
        if len(columns) < 2:
            continue
        status = columns[0][:1]
        path = _posix(columns[-1])
        if path:
            local_status[path] = status
    facts: dict[str, dict[str, object]] = {}
    failures: list[dict[str, str]] = []
    for path in paths:
        if path not in local_status:
            failures.append({
                "scope": "path",
                "path": path,
                "reason": "immutable diff path unavailable",
            })
            continue
        try:
            result = subprocess.run(
                [
                    "git", "diff", "--no-ext-diff", "--no-color",
                    "--find-renames", "--unified=3",
                    f"{base_sha}...{head_sha}", "--", path,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError:
            failures.append({
                "scope": "path",
                "path": path,
                "reason": "immutable diff command unavailable",
            })
            continue
        if result.returncode != 0:
            failures.append({
                "scope": "path",
                "path": path,
                "reason": "immutable diff command failed",
            })
            continue
        patch = result.stdout
        if not patch.strip():
            failures.append({
                "scope": "path",
                "path": path,
                "reason": "immutable diff path unavailable",
            })
            continue
        patch = patch.encode("utf-8")[:_MAX_LOCAL_PATCH_BYTES].decode(
            "utf-8", errors="replace",
        )
        facts[path] = _facts_from_patch(
            path,
            local_status.get(path, "modified"),
            patch,
            include_intent=True,
        )
    return {
        "facts": facts,
        "bounded": True,
        "path_limit": _MAX_CHANGE_FACT_PATHS,
        "included_path_count": len(facts),
        "omitted_path_count": len(all_paths) - len(facts),
        "failed_path_count": len(failures),
        "status": "degraded" if failures else "ok",
        "failures": failures,
    }


def build_topology(
    pr_files: list[dict[str, Any]],
    classification: dict[str, Any] | None,
    tracked_paths: Iterable[str],
    config: dict[str, Any] | None = None,
    workspace_paths: Iterable[str] | None = None,
    change_facts: dict[str, object] | None = None,
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
    immutable_facts_value = (
        change_facts.get("facts", {})
        if isinstance(change_facts, dict)
        else {}
    )
    immutable_facts = (
        immutable_facts_value
        if isinstance(immutable_facts_value, dict)
        else {}
    )
    for item in pr_files:
        path = _posix(item.get("filename"))
        patch = item.get("patch")
        if not path:
            continue
        local = immutable_facts.get(path)
        changed_contract_facts[path] = (
            dict(local)
            if isinstance(local, dict)
            else _facts_from_patch(path, item.get("status"), patch)
        )
    topology = {
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
    if change_facts is not None:
        topology["change_facts"] = change_facts
    return topology
