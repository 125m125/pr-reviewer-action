"""Select a useful, bounded pull-request diff without changing review scope.

The selector only considers paths from the controller-owned changed-file
manifest.  Project rules can change ordering, but cannot add paths or alter
the immutable revision used to obtain the diff.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


_DEFAULT_RULES: tuple[dict[str, Any], ...] = (
    {"category": "orientation", "priority": 10, "globs": ("README*", "**/*.md", "**/*.adoc")},
    {"category": "contract", "priority": 20, "globs": (
        "action.yml", ".github/workflows/**", ".github/actions/**", "Dockerfile*",
    )},
    {"category": "configuration", "priority": 30, "globs": (
        ".env*", "**/*.properties", "**/*.ini", "**/*.conf", "**/*.yaml", "**/*.yml",
        "**/*.toml", "**/config/**",
    )},
    {"category": "build", "priority": 40, "globs": (
        "pom.xml", "**/pom.xml", "package.json", "**/package.json", "pyproject.toml",
        "**/pyproject.toml", "go.mod", "**/go.mod", "Cargo.toml", "**/Cargo.toml",
        "Makefile", "**/Makefile",
    )},
    {"category": "schema", "priority": 50, "globs": ("**/migrations/**", "**/schema/**", "**/*migration*")},
    {"category": "tests", "priority": 60, "globs": ("**/test/**", "**/tests/**", "**/*test.*", "**/*spec.*")},
    {"category": "source", "priority": 70, "globs": ("*",)},
    {"category": "lockfiles", "priority": 90, "globs": (
        "package-lock.json", "**/package-lock.json", "npm-shrinkwrap.json", "**/npm-shrinkwrap.json",
        "yarn.lock", "**/yarn.lock", "pnpm-lock.yaml", "**/pnpm-lock.yaml", "Pipfile.lock",
        "**/Pipfile.lock", "poetry.lock", "**/poetry.lock", "Cargo.lock", "**/Cargo.lock",
        "go.sum", "**/go.sum", "composer.lock", "**/composer.lock", "Gemfile.lock",
        "**/Gemfile.lock", "**/*.lock", "*.lock",
    )},
)


@dataclass(frozen=True)
class DiffSelection:
    text: str
    index: str
    selected_paths: tuple[str, ...]
    omitted_paths: tuple[str, ...]
    truncated_paths: tuple[str, ...]


@dataclass(frozen=True)
class _Rule:
    category: str
    priority: int
    globs: tuple[str, ...]
    max_bytes: int | None = None
    custom: bool = False


def _normal_path(value: object) -> str:
    return str(value or "").replace("\\", "/").strip("/")


def _rule_matches(path: str, pattern: str) -> bool:
    pattern = _normal_path(pattern)
    if not pattern:
        return False
    candidates = {pattern}
    if "**/" in pattern:
        candidates.add(pattern.replace("**/", ""))
    basename = path.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatchcase(path, candidate)
        or fnmatch.fnmatchcase(basename, candidate)
        for candidate in candidates
    )


def _rules(config: Mapping[str, object] | None) -> tuple[_Rule, ...]:
    raw_rules: object = config.get("rules", ()) if isinstance(config, Mapping) else ()
    result: list[_Rule] = [
        _Rule(
            str(item["category"]), int(item["priority"]), tuple(item["globs"]),
        )
        for item in _DEFAULT_RULES
    ]
    if isinstance(raw_rules, Sequence) and not isinstance(raw_rules, (str, bytes)):
        for raw in raw_rules:
            if not isinstance(raw, Mapping):
                continue
            globs = raw.get("globs", raw.get("patterns", raw.get("glob", ())))
            if isinstance(globs, str):
                globs = (globs,)
            if not isinstance(globs, Sequence) or isinstance(globs, (str, bytes)):
                continue
            normalized_globs = tuple(
                _normal_path(item) for item in globs if _normal_path(item)
            )
            if not normalized_globs:
                continue
            try:
                priority = int(raw.get("priority", 100))
            except (TypeError, ValueError):
                continue
            max_bytes: int | None = None
            if raw.get("max_bytes") is not None:
                try:
                    candidate = int(raw["max_bytes"])
                    if candidate > 0:
                        max_bytes = candidate
                except (TypeError, ValueError):
                    pass
            result.append(_Rule(
                str(raw.get("category", "custom")).strip() or "custom",
                priority,
                normalized_globs,
                max_bytes,
                True,
            ))
    return tuple(result)


def load_priority_config(path: str | Path | None) -> Mapping[str, object]:
    """Load an optional project override; invalid files safely use defaults."""
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _split_diff(text: str) -> dict[str, str]:
    chunks = re.split(r"(?=^diff --git .*$)", text, flags=re.MULTILINE)
    result: dict[str, str] = {}
    for chunk in chunks:
        if not chunk.startswith("diff --git "):
            continue
        path = ""
        for line in chunk.splitlines():
            if line.startswith("+++ b/"):
                path = _normal_path(line[6:])
                break
            if line.startswith("--- a/"):
                path = _normal_path(line[6:])
        if path and path != "/dev/null":
            result[path] = chunk
    return result


def manifest_from_diff(text: str) -> tuple[Mapping[str, object], ...]:
    """Build a minimal authoritative manifest for an immutable diff slice."""
    return tuple(
        {"filename": path, "status": "modified"}
        for path in _split_diff(text)
    )


def _path_rule(path: str, rules: Iterable[_Rule]) -> _Rule:
    matches = [
        rule for rule in rules
        if any(_rule_matches(path, pattern) for pattern in rule.globs)
    ]
    if not matches:
        return _Rule("source", 70, ("*",))
    custom_matches = [rule for rule in matches if rule.custom]
    if custom_matches:
        return min(custom_matches, key=lambda item: item.priority)
    lockfile_matches = [rule for rule in matches if rule.category == "lockfiles"]
    return min(lockfile_matches or matches, key=lambda item: item.priority)


def _clip_utf8(text: str, max_bytes: int, marker: str) -> str:
    marker_bytes = marker.encode("utf-8")
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    if max_bytes <= len(marker_bytes):
        return marker_bytes[:max_bytes].decode("utf-8", errors="ignore")
    prefix = raw[:max_bytes - len(marker_bytes)].decode("utf-8", errors="ignore")
    newline = prefix.rfind("\n")
    if newline >= 0:
        prefix = prefix[:newline + 1]
    return prefix + marker


def _render_index(
    files: Sequence[Mapping[str, object]],
    *,
    total_changed_files: int | None = None,
) -> str:
    lines = ["# Changed Files Index", "", "| Path | Status | Added | Removed |", "| --- | --- | ---: | ---: |"]
    for item in files:
        path = _normal_path(item.get("filename", item.get("path", "")))
        if not path:
            continue
        status = str(item.get("status", "modified"))
        additions = item.get("additions", 0)
        deletions = item.get("deletions", 0)
        lines.append(f"| `{path}` | {status} | {additions} | {deletions} |")
    path_count = sum(
        bool(_normal_path(item.get("filename", item.get("path", ""))))
        for item in files
    )
    if total_changed_files is not None and total_changed_files > path_count:
        lines.extend(("", f"_Index shows the first {path_count} of {total_changed_files} changed files._"))
    return "\n".join(lines) + "\n"


def prioritize_diff(
    diff_text: str,
    files: Sequence[Mapping[str, object]],
    max_bytes: int,
    *,
    config: Mapping[str, object] | None = None,
    total_changed_files: int | None = None,
) -> DiffSelection:
    """Return ranked diff sections and an index for the supplied manifest page."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    manifest_paths = tuple(
        dict.fromkeys(
            _normal_path(item.get("filename", item.get("path", "")))
            for item in files
            if _normal_path(item.get("filename", item.get("path", "")))
        )
    )
    sections = _split_diff(diff_text)
    rules = _rules(config)
    ranked = sorted(
        enumerate(manifest_paths),
        key=lambda item: (_path_rule(item[1], rules).priority, item[0]),
    )
    notice_marker = "\n…[diff sections omitted; see changed-file index]\n"
    total_section_bytes = sum(
        len(sections[path].encode("utf-8"))
        for path in manifest_paths
        if path in sections
    )
    reserve_notice = (
        len(manifest_paths) > len(sections)
        or total_section_bytes > max_bytes
    )
    content_budget = (
        max(0, max_bytes - len(notice_marker.encode("utf-8")))
        if reserve_notice else max_bytes
    )
    selected: list[str] = []
    omitted: list[str] = []
    truncated: list[str] = []
    rendered: list[str] = []
    used = 0
    category_used: dict[str, int] = {}
    for _position, path in ranked:
        section = sections.get(path)
        if not section:
            omitted.append(path)
            continue
        rule = _path_rule(path, rules)
        remaining = content_budget - used
        if rule.max_bytes is not None:
            remaining = min(remaining, max(0, rule.max_bytes - category_used.get(rule.category, 0)))
        if remaining <= 0:
            omitted.append(path)
            continue
        if len(section.encode("utf-8")) <= remaining:
            rendered.append(section)
            selected.append(path)
            size = len(section.encode("utf-8"))
        else:
            marker = f"\n…[diff section truncated: {path}]\n"
            clipped = _clip_utf8(section, remaining, marker)
            if not clipped:
                omitted.append(path)
                continue
            rendered.append(clipped)
            selected.append(path)
            truncated.append(path)
            size = len(clipped.encode("utf-8"))
        used += size
        category_used[rule.category] = category_used.get(rule.category, 0) + size
    notices: list[str] = []
    if omitted:
        notices.append("…[diff sections omitted: " + ", ".join(omitted) + "]")
    if truncated:
        notices.append("…[diff sections truncated: " + ", ".join(truncated) + "]")
    output = "\n".join(rendered)
    if notices:
        detailed_notice = output.rstrip("\n") + "\n" + "\n".join(notices) + "\n"
        if len(detailed_notice.encode("utf-8")) <= max_bytes:
            output = detailed_notice
        else:
            output = _clip_utf8(
                output,
                max_bytes,
                notice_marker,
            )
    return DiffSelection(
        text=output,
        index=_render_index(files, total_changed_files=total_changed_files),
        selected_paths=tuple(selected),
        omitted_paths=tuple(omitted),
        truncated_paths=tuple(truncated),
    )
