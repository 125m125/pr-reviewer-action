"""Validated runtime settings and repository-controlled specialist policy."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .types import BudgetLimits, PhaseShares


_V2_TOP_LEVEL_KEYS = frozenset({
    "version", "components", "recipes", "coverage_rules", "sources",
    "generated_artifacts", "verdict_policy", "publishing", "exclude",
})
_V1_TOP_LEVEL_KEYS = frozenset({
    "version", "components", "recipes", "generated_artifacts", "exclude",
})
_MATCH_KEYS = frozenset({
    "paths_any", "component_ids_any", "risk_flags_any", "file_roles_any",
})
_EXECUTION_MODES = frozenset({"coverage", "dedicated", "independent"})
_HOST_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)


def _posix(value: Any) -> str:
    value = str(value or "").replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value.strip("/")


def _slug(value: Any, fallback: str = "focus") -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return (value or fallback)[:80]


def _strings(value: Any, *, field_name: str, limit: int = 100, chars: int = 1000) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be an array")
    values: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must contain non-empty strings")
        clean = item.strip()[:chars]
        if clean not in values:
            values.append(clean)
        if len(values) >= limit:
            break
    return tuple(values)


def _repository_paths(value: Any, *, field_name: str, limit: int = 100) -> tuple[str, ...]:
    paths = []
    for item in _strings(value, field_name=field_name, limit=limit):
        slash_normalized = item.replace("\\", "/")
        normalized = _posix(slash_normalized)
        if (
            not normalized
            or slash_normalized.startswith("/")
            or re.match(r"^[A-Za-z]:", slash_normalized)
            or ".." in normalized.split("/")
        ):
            raise ValueError(f"{field_name} must contain repository-relative paths")
        paths.append(normalized)
    return tuple(paths)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list) or isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    return value


def _entries(value: Any, *, field_name: str) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{field_name} must be an array of objects")
    return value


@dataclass(frozen=True)
class SourceRule:
    host: str
    include_subdomains: bool = False
    path_prefixes: tuple[str, ...] = ()
    classification: str = "reference"
    max_age_hours: int | None = None
    schemes: tuple[str, ...] = ("https",)


@dataclass(frozen=True)
class RecipePolicy:
    id: str
    title: str
    objective: str
    execution: str = "coverage"
    match: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))
    lenses: tuple[str, ...] = ()
    seed_paths: tuple[str, ...] = ()
    related_paths: tuple[str, ...] = ()
    invariants: tuple[str, ...] = ()
    expected_evidence: tuple[str, ...] = ()
    priority: str = "normal"


@dataclass(frozen=True)
class ReviewPolicy:
    version: int = 2
    components: tuple[Mapping[str, Any], ...] = ()
    recipes: tuple[RecipePolicy, ...] = ()
    coverage_rules: tuple[Mapping[str, Any], ...] = ()
    sources: tuple[SourceRule, ...] = ()
    generated_artifacts: tuple[Mapping[str, Any], ...] = ()
    verdict_policy: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    publishing: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    exclude: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({"paths": (), "components": (), "lenses": (), "recipes": ()})
    )

    @classmethod
    def minimal(cls, *, recipes: tuple[RecipePolicy, ...] = ()) -> "ReviewPolicy":
        return cls(recipes=recipes)

    def legacy_projection(self) -> dict[str, Any]:
        """Return the v1 dictionary consumed by unreplaced specialist helpers."""
        recipes = []
        for recipe in self.recipes:
            recipes.append({
                "id": recipe.id,
                "match": {key: list(value) for key, value in recipe.match.items()},
                "title": recipe.title,
                "objective": recipe.objective,
                "lenses": list(recipe.lenses),
                "seed_paths": list(recipe.seed_paths),
                "related_paths": list(recipe.related_paths),
                "invariants": list(recipe.invariants),
                "expected_evidence": list(recipe.expected_evidence),
                "priority": recipe.priority,
                "source": "recipe",
            })
        return {
            "version": 1,
            "components": [_thaw(item) for item in self.components],
            "recipes": recipes,
            "generated_artifacts": [_thaw(item) for item in self.generated_artifacts],
            "exclude": {key: list(value) for key, value in self.exclude.items()},
        }


@dataclass(frozen=True)
class RuntimeConfig:
    review_deadline_sec: int = 7200
    model_request_timeout_sec: int = 300
    phase_shares: PhaseShares = field(default_factory=PhaseShares)
    concurrency: int = 1
    max_sessions: int = 8
    max_followup_sessions: int = 2
    session_limits: BudgetLimits = field(
        default_factory=lambda: BudgetLimits(model_turns=64, tool_calls=20, recoveries=1)
    )
    deprecation_warnings: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "RuntimeConfig":
        warnings: list[str] = []

        def setting(name: str, default: int, *, alias: str | None = None) -> int:
            value = env.get(name)
            if value is None and alias is not None and env.get(alias) is not None:
                value = env[alias]
                warnings.append(alias.lower())
            if value is None:
                return default
            try:
                parsed = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name.lower()} must be a positive integer") from exc
            if parsed <= 0:
                raise ValueError(f"{name.lower()} must be a positive integer")
            return parsed

        raw_shares = env.get("SPECIALIST_PHASE_SHARES")
        if raw_shares is None:
            phase_shares = PhaseShares()
        else:
            try:
                shares = json.loads(raw_shares)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("specialist_phase_shares must be JSON object") from exc
            if not isinstance(shares, dict) or set(shares) != {
                "planning", "initial", "followup", "finalization",
            }:
                raise ValueError("phase shares must define planning, initial, followup, and finalization")
            phase_shares = PhaseShares(**shares)

        return cls(
            review_deadline_sec=setting("SPECIALIST_REVIEW_DEADLINE_SEC", 7200),
            model_request_timeout_sec=setting("AI_REQUEST_TIMEOUT_SEC", 300),
            phase_shares=phase_shares,
            concurrency=setting("SPECIALIST_CONCURRENCY", 1),
            max_sessions=setting(
                "SPECIALIST_MAX_SESSIONS", 8, alias="SPECIALIST_MAX_INITIAL_PASSES"
            ),
            max_followup_sessions=setting(
                "SPECIALIST_MAX_FOLLOWUP_SESSIONS", 2, alias="SPECIALIST_MAX_FOLLOWUP_PASSES"
            ),
            session_limits=BudgetLimits(
                model_turns=setting("SPECIALIST_MAX_MODEL_TURNS_PER_SESSION", 64),
                tool_calls=setting(
                    "SPECIALIST_MAX_TOOL_CALLS_PER_SESSION", 20,
                    alias="SPECIALIST_MAX_TOOL_CALLS_PER_PASS",
                ),
                recoveries=setting("SPECIALIST_MAX_RECOVERIES_PER_SESSION", 1),
            ),
            deprecation_warnings=tuple(warnings),
        )


def migrate_v1_policy(data: Mapping[str, Any]) -> dict[str, Any]:
    """Translate the legacy repository policy into the version-2 schema."""
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("specialist config must be a JSON object with version 1")
    unknown = set(data) - _V1_TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"policy contains unknown top-level keys: {', '.join(sorted(unknown))}")
    recipes = []
    for item in data.get("recipes", []):
        if not isinstance(item, dict):
            raise ValueError("recipes must be an array of objects")
        recipes.append({**item, "execution": item.get("execution", "coverage")})
    return {
        "version": 2,
        "components": data.get("components", []),
        "recipes": recipes,
        "coverage_rules": [],
        "sources": [],
        "generated_artifacts": data.get("generated_artifacts", []),
        "verdict_policy": {},
        "publishing": {},
        "exclude": data.get("exclude", {}),
    }


def _component_policy(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    if not raw.get("id"):
        raise ValueError("every specialist component requires an id")
    return MappingProxyType({
        "id": _slug(raw["id"]),
        "paths": _repository_paths(raw.get("paths", []), field_name="component paths"),
        "responsibilities": _strings(raw.get("responsibilities", []), field_name="responsibilities"),
        "related_components": tuple(
            _slug(item) for item in _strings(raw.get("related_components", []), field_name="related_components")
        ),
        "contracts": _strings(raw.get("contracts", []), field_name="contracts"),
        "invariants": _strings(raw.get("invariants", []), field_name="invariants"),
    })


def _recipe_policy(raw: Mapping[str, Any]) -> RecipePolicy:
    if not raw.get("id"):
        raise ValueError("every specialist recipe requires an id")
    match = _mapping(raw.get("match"), field_name="recipe match")
    unknown_match = set(match) - _MATCH_KEYS
    if unknown_match:
        raise ValueError(f"recipe match contains unknown keys: {', '.join(sorted(unknown_match))}")
    normalized_match: dict[str, tuple[str, ...]] = {}
    for key in _MATCH_KEYS:
        if key in match:
            values = _strings(match[key], field_name=f"recipe match {key}")
            if key == "paths_any":
                values = _repository_paths(list(values), field_name=f"recipe match {key}")
            elif key == "component_ids_any":
                values = tuple(_slug(item) for item in values)
            normalized_match[key] = values
    execution = str(raw.get("execution", "coverage")).strip().lower()
    if execution not in _EXECUTION_MODES:
        raise ValueError("recipe execution must be coverage, dedicated, or independent")
    priority = str(raw.get("priority", "normal")).strip().lower()
    if priority not in {"critical", "high", "normal", "low"}:
        priority = "normal"
    return RecipePolicy(
        id=_slug(raw["id"]),
        title=str(raw.get("title") or raw["id"]).strip()[:160],
        objective=str(raw.get("objective") or "Review the matched change for correctness.").strip()[:1000],
        execution=execution,
        match=MappingProxyType(normalized_match),
        lenses=tuple(_slug(item) for item in _strings(raw.get("lenses", []), field_name="recipe lenses")),
        seed_paths=_repository_paths(raw.get("seed_paths", []), field_name="recipe seed_paths"),
        related_paths=_repository_paths(raw.get("related_paths", []), field_name="recipe related_paths"),
        invariants=_strings(raw.get("invariants", []), field_name="recipe invariants"),
        expected_evidence=_strings(raw.get("expected_evidence", []), field_name="recipe expected_evidence"),
        priority=priority,
    )


def _source_rule(raw: Mapping[str, Any]) -> SourceRule:
    host = raw.get("host")
    if not isinstance(host, str) or host != host.lower() or not _HOST_RE.fullmatch(host):
        raise ValueError("source rule requires a concrete lowercase host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("source rule host must be a DNS name")
    schemes = _strings(raw.get("schemes", ["https"]), field_name="source rule schemes")
    if schemes != ("https",):
        raise ValueError("source rule permits HTTPS only")
    include_subdomains = raw.get("include_subdomains", False)
    if not isinstance(include_subdomains, bool):
        raise ValueError("source rule include_subdomains must be boolean")
    path_prefixes = []
    for prefix in _strings(raw.get("path_prefixes", []), field_name="source rule path_prefixes"):
        if not prefix.startswith("/") or "/../" in f"{prefix}/" or "://" in prefix:
            raise ValueError("source rule path_prefixes must be absolute paths")
        path_prefixes.append(prefix.rstrip("/") or "/")
    classification = raw.get("classification", "reference")
    if not isinstance(classification, str) or not classification.strip():
        raise ValueError("source rule classification must be a non-empty string")
    max_age_hours = raw.get("max_age_hours")
    if max_age_hours is not None and (isinstance(max_age_hours, bool) or not isinstance(max_age_hours, int) or max_age_hours <= 0):
        raise ValueError("source rule max_age_hours must be a positive integer")
    return SourceRule(
        host=host,
        include_subdomains=include_subdomains,
        path_prefixes=tuple(path_prefixes),
        classification=classification.strip()[:100],
        max_age_hours=max_age_hours,
        schemes=schemes,
    )


def _generated_artifact(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    if not raw.get("id"):
        raise ValueError("every generated artifact requires an id")
    return MappingProxyType({
        "id": _slug(raw["id"]),
        "source_of_truth": _repository_paths(raw.get("source_of_truth", []), field_name="source_of_truth", limit=50),
        "generator_config": _repository_paths(raw.get("generator_config", []), field_name="generator_config", limit=50),
        "output_paths": _repository_paths(raw.get("output_paths", []), field_name="output_paths", limit=50),
    })


def _exclude(raw: Any) -> Mapping[str, tuple[str, ...]]:
    data = _mapping(raw, field_name="exclude")
    unknown = set(data) - {"paths", "components", "lenses", "recipes"}
    if unknown:
        raise ValueError(f"exclude contains unknown keys: {', '.join(sorted(unknown))}")
    return MappingProxyType({
        "paths": _repository_paths(data.get("paths", []), field_name="exclude paths"),
        "components": tuple(_slug(item) for item in _strings(data.get("components", []), field_name="exclude components")),
        "lenses": tuple(_slug(item) for item in _strings(data.get("lenses", []), field_name="exclude lenses")),
        "recipes": tuple(_slug(item) for item in _strings(data.get("recipes", []), field_name="exclude recipes")),
    })


def _parse_v2_policy(data: Mapping[str, Any]) -> ReviewPolicy:
    if data.get("version") != 2:
        raise ValueError("review policy must be a JSON object with version 1 or 2")
    unknown = set(data) - _V2_TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f"policy contains unknown top-level keys: {', '.join(sorted(unknown))}")
    if not (set(data) - {"version"}):
        raise ValueError("review policy must define at least one configuration section")
    return ReviewPolicy(
        components=tuple(_component_policy(item) for item in _entries(data.get("components"), field_name="components")),
        recipes=tuple(_recipe_policy(item) for item in _entries(data.get("recipes"), field_name="recipes")),
        coverage_rules=tuple(_freeze(item) for item in _entries(data.get("coverage_rules"), field_name="coverage_rules")),
        sources=tuple(_source_rule(item) for item in _entries(data.get("sources"), field_name="sources")),
        generated_artifacts=tuple(
            _generated_artifact(item) for item in _entries(data.get("generated_artifacts"), field_name="generated_artifacts")
        ),
        verdict_policy=_freeze(_mapping(data.get("verdict_policy"), field_name="verdict_policy")),
        publishing=_freeze(_mapping(data.get("publishing"), field_name="publishing")),
        exclude=_exclude(data.get("exclude")),
    )


def load_review_policy(path: str | Path, legacy_path: str | Path | None = None) -> ReviewPolicy:
    """Load the current-branch policy, falling back to an optional legacy file."""
    candidate = Path(path)
    if not candidate.is_file() and legacy_path is not None:
        candidate = Path(legacy_path)
    if not candidate.is_file():
        return ReviewPolicy.minimal()
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"review policy is not valid JSON: {candidate}") from exc
    if not isinstance(data, dict):
        raise ValueError("review policy must be a JSON object")
    if data.get("version") == 1:
        data = migrate_v1_policy(data)
    return _parse_v2_policy(data)
