"""Validate model-proposed specialist work without weakening immutable coverage."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re
from typing import Any

from .policy import RuntimeConfig
from .types import CoverageObligation


_REQUIRED_FIELDS = (
    "id", "title", "objective", "obligation_ids", "lenses", "seed_paths",
    "boundary_paths", "expected_evidence", "estimated_turns", "priority",
    "overlap_justification",
)
_PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}


class AssignmentPlanError(ValueError):
    """A model plan failed deterministic, policy-owned validation."""

    def __init__(self, errors: Iterable[str] | str) -> None:
        self.errors = (errors,) if isinstance(errors, str) else tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class Assignment:
    id: str
    title: str
    objective: str
    obligation_ids: tuple[str, ...]
    recipe_ids: tuple[str, ...]
    lenses: tuple[str, ...]
    seed_paths: tuple[str, ...]
    boundary_paths: tuple[str, ...]
    expected_evidence: tuple[str, ...]
    estimated_turns: int
    priority: str
    overlap_justification: str = ""

    @property
    def assignment_id(self) -> str:
        """Compatibility with the existing immutable assignment vocabulary."""
        return self.id


@dataclass(frozen=True)
class AssignmentPlan:
    assignments: tuple[Assignment, ...]
    unassigned_obligation_ids: tuple[str, ...] = ()


def _assignable_obligations(obligations: Iterable[CoverageObligation]) -> tuple[CoverageObligation, ...]:
    """Exclude Task 3's non-mandatory, evidence-free lifecycle bookkeeping."""
    return tuple(item for item in obligations if item.mandatory and item.required_evidence_categories)


def _strings(value: Any, field: str, errors: list[str], *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        errors.append(f"{field} must be an array")
        return ()
    values: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{field} must contain non-empty strings")
            continue
        item = item.strip()
        if item not in values:
            values.append(item)
    if not allow_empty and not values:
        errors.append(f"{field} must not be empty")
    return tuple(values)


def _paths(value: Any, field: str, errors: list[str]) -> tuple[str, ...]:
    values = _strings(value, field, errors, allow_empty=True)
    clean: list[str] = []
    for path in values:
        normalized = path.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        if (not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized)
                or ".." in normalized.split("/")):
            errors.append(f"{field} must contain repository-relative paths")
            continue
        clean.append(normalized.strip("/"))
    return tuple(clean)


def _parse_assignment(
    raw: Any, index: int, obligation_by_id: Mapping[str, CoverageObligation],
) -> tuple[Assignment | None, list[str]]:
    errors: list[str] = []
    label = f"assignment {index}"
    if not isinstance(raw, Mapping):
        return None, [f"{label} must be an object"]
    missing = [field for field in _REQUIRED_FIELDS if field not in raw]
    errors.extend(f"{label} missing {field}" for field in missing)
    if missing:
        return None, errors
    text_values: dict[str, str] = {}
    for field in ("id", "title", "objective"):
        value = raw[field]
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{label} {field} must be a non-empty string")
            text_values[field] = ""
        else:
            text_values[field] = value.strip()
    obligation_ids = _strings(raw["obligation_ids"], f"{label} obligation_ids", errors)
    unknown = sorted(set(obligation_ids) - set(obligation_by_id))
    if unknown:
        errors.append(f"{label} contains unknown or non-assignable obligations: {', '.join(unknown)}")
    lenses = _strings(raw["lenses"], f"{label} lenses", errors)
    seed_paths = _paths(raw["seed_paths"], f"{label} seed_paths", errors)
    boundary_paths = _paths(raw["boundary_paths"], f"{label} boundary_paths", errors)
    expected_evidence = _strings(raw["expected_evidence"], f"{label} expected_evidence", errors)
    estimated_turns = raw["estimated_turns"]
    if isinstance(estimated_turns, bool) or not isinstance(estimated_turns, int) or estimated_turns <= 0:
        errors.append(f"{label} estimated_turns must be a positive integer")
        estimated_turns = 0
    priority = raw["priority"]
    if not isinstance(priority, str) or priority.strip().lower() not in _PRIORITY_RANK:
        errors.append(f"{label} priority must be critical, high, normal, or low")
        priority = "normal"
    else:
        priority = priority.strip().lower()
    overlap_justification = raw["overlap_justification"]
    if not isinstance(overlap_justification, str):
        errors.append(f"{label} overlap_justification must be a string")
        overlap_justification = ""
    else:
        overlap_justification = overlap_justification.strip()

    required_evidence = {
        evidence for obligation_id in obligation_ids
        if obligation_id in obligation_by_id
        for evidence in obligation_by_id[obligation_id].required_evidence_categories
    }
    missing_evidence = sorted(required_evidence - set(expected_evidence))
    if missing_evidence:
        errors.append(f"{label} expected_evidence omits required categories: {', '.join(missing_evidence)}")
    recipe_ids = tuple(sorted({
        obligation_by_id[obligation_id].recipe_id for obligation_id in obligation_ids
        if obligation_id in obligation_by_id and obligation_by_id[obligation_id].recipe_id
    }))
    allowed_paths = {
        path.replace("\\", "/").strip("/") for obligation_id in obligation_ids
        if obligation_id in obligation_by_id
        for path in (*obligation_by_id[obligation_id].scope, *obligation_by_id[obligation_id].seed_hints)
    }
    out_of_scope = sorted(set(seed_paths).union(boundary_paths) - allowed_paths)
    if out_of_scope:
        errors.append(
            f"{label} paths outside immutable obligation scope: {', '.join(out_of_scope)}"
        )
    if errors:
        return None, errors
    return Assignment(
        id=text_values["id"], title=text_values["title"], objective=text_values["objective"],
        obligation_ids=obligation_ids, recipe_ids=recipe_ids, lenses=lenses,
        seed_paths=seed_paths, boundary_paths=boundary_paths, expected_evidence=expected_evidence,
        estimated_turns=estimated_turns, priority=priority,
        overlap_justification=overlap_justification,
    ), []


def _recipe_execution(obligation: CoverageObligation) -> str | None:
    return "independent" if obligation.requires_independent_verification else obligation.recipe_execution


def _validate_recipe_execution(
    assignments: tuple[Assignment, ...], obligation_by_id: Mapping[str, CoverageObligation],
) -> list[str]:
    errors: list[str] = []
    owners: dict[str, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        for obligation_id in assignment.obligation_ids:
            owners[obligation_id].append(assignment)
    recipes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for obligation in obligation_by_id.values():
        execution = _recipe_execution(obligation)
        if obligation.recipe_id and execution in {"dedicated", "independent"}:
            recipes[(obligation.recipe_id, execution)].add(obligation.id)
    occupied: dict[str, tuple[str, str]] = {}
    for (recipe_id, execution), obligation_ids in sorted(recipes.items()):
        assigned_to = {assignment.id for obligation_id in obligation_ids for assignment in owners.get(obligation_id, [])}
        if len(assigned_to) != 1:
            errors.append(f"{execution} recipe '{recipe_id}' must have one distinct assignment")
            continue
        assignment = next(item for item in assignments if item.id in assigned_to)
        if any(obligation_id not in obligation_ids for obligation_id in assignment.obligation_ids):
            errors.append(f"{execution} recipe '{recipe_id}' must be isolated in its assignment")
        prior = occupied.get(assignment.id)
        if prior and prior != (recipe_id, execution):
            errors.append(
                f"{execution} recipe '{recipe_id}' must be in a distinct assignment from "
                f"{prior[1]} recipe '{prior[0]}'"
            )
        occupied[assignment.id] = (recipe_id, execution)
    return errors


def _validate_overlap(assignments: tuple[Assignment, ...]) -> list[str]:
    errors: list[str] = []
    owners: dict[str, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        for obligation_id in assignment.obligation_ids:
            owners[obligation_id].append(assignment)
    for obligation_id, shared in sorted(owners.items()):
        if len(shared) < 2:
            continue
        if any(not assignment.overlap_justification for assignment in shared):
            errors.append(f"shared obligation '{obligation_id}' requires overlap justification")
        if len({(assignment.objective, assignment.lenses) for assignment in shared}) == 1:
            errors.append(f"shared obligation '{obligation_id}' has no distinct analytical focus")
    return errors


def _validate_budget(assignments: tuple[Assignment, ...], config: RuntimeConfig) -> list[str]:
    errors: list[str] = []
    if len(assignments) > config.max_sessions:
        errors.append(f"session cap exceeded: {len(assignments)} assignments > {config.max_sessions}")
    for assignment in assignments:
        if assignment.estimated_turns > config.session_limits.model_turns:
            errors.append(f"assignment '{assignment.id}' estimated turns exceed per-session limit")
        if assignment.estimated_turns > config.review_deadline_sec:
            errors.append(f"assignment '{assignment.id}' estimated turns exceed review deadline")
    if sum(item.estimated_turns for item in assignments) > config.review_deadline_sec * config.concurrency:
        errors.append("estimated turns exceed deadline capacity")
    return errors


def validate_assignment_plan(
    raw: Mapping[str, Any], obligations: Iterable[CoverageObligation], topology: Mapping[str, Any],
    runtime_config: RuntimeConfig,
) -> AssignmentPlan:
    """Validate model grouping; the immutable obligation set remains authoritative."""
    del topology
    assignable = _assignable_obligations(obligations)
    obligation_by_id = {item.id: item for item in assignable}
    errors: list[str] = []
    if len(obligation_by_id) != len(assignable):
        errors.append("immutable obligations contain duplicate identifiers")
    if not isinstance(raw, Mapping) or not isinstance(raw.get("assignments"), list):
        raise AssignmentPlanError("plan assignments must be an array")
    parsed: list[Assignment] = []
    for index, item in enumerate(raw["assignments"]):
        assignment, assignment_errors = _parse_assignment(item, index, obligation_by_id)
        errors.extend(assignment_errors)
        if assignment is not None:
            parsed.append(assignment)
    assignments = tuple(parsed)
    if len({item.id for item in assignments}) != len(assignments):
        errors.append("assignment ids must be unique")
    assigned_ids = {obligation_id for item in assignments for obligation_id in item.obligation_ids}
    missing = sorted(set(obligation_by_id) - assigned_ids)
    if missing:
        errors.append(f"unassigned mandatory obligations: {', '.join(missing)}")
    errors.extend(_validate_overlap(assignments))
    errors.extend(_validate_recipe_execution(assignments, obligation_by_id))
    errors.extend(_validate_budget(assignments, runtime_config))
    if errors:
        raise AssignmentPlanError(errors)
    return AssignmentPlan(assignments=assignments)


def repair_prompt(errors: Iterable[str], previous_plan: Mapping[str, Any]) -> dict[str, Any]:
    """The one repair request exposes only plan errors and the previous plan."""
    return {"errors": list(errors), "previous_plan": previous_plan}


def _priority(obligations: Iterable[CoverageObligation]) -> str:
    values = [item.risk_tier if item.risk_tier in _PRIORITY_RANK else "normal" for item in obligations]
    return min(values, key=lambda value: _PRIORITY_RANK[value], default="normal")


def _component_paths(topology: Mapping[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for component in topology.get("components", []):
        if isinstance(component, Mapping) and isinstance(component.get("id"), str):
            paths = component.get("changed_files", [])
            if isinstance(paths, list):
                result[component["id"]] = {str(path).replace("\\", "/").strip("/") for path in paths}
    return result


def _fallback_group(obligation: CoverageObligation, component_paths: Mapping[str, set[str]]) -> str:
    execution = _recipe_execution(obligation)
    if obligation.recipe_id and execution in {"dedicated", "independent"}:
        return f"recipe:{execution}:{obligation.recipe_id}"
    scope = set(obligation.scope)
    matches = [component for component, paths in component_paths.items() if scope.intersection(paths)]
    if obligation.required_evidence_categories == ("interaction",) or len(matches) > 1:
        return f"boundary:{obligation.subject}"
    if len(matches) == 1:
        return f"component:{matches[0]}"
    if obligation.subject in component_paths:
        return f"component:{obligation.subject}"
    return f"repository:{obligation.subject}"


def _fallback_assignment(key: str, obligations: tuple[CoverageObligation, ...], config: RuntimeConfig) -> Assignment:
    suffix = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-") or "review"
    seed_paths = tuple(sorted({path for item in obligations for path in item.seed_hints}))
    boundary_paths = tuple(sorted({path for item in obligations for path in item.scope if path not in seed_paths}))
    return Assignment(
        id=f"fallback-{suffix}", title=f"Fallback {key}",
        objective=f"Deterministically cover {key} obligations.",
        obligation_ids=tuple(item.id for item in obligations),
        recipe_ids=tuple(sorted({item.recipe_id for item in obligations if item.recipe_id})),
        lenses=("deterministic-coverage",), seed_paths=seed_paths, boundary_paths=boundary_paths,
        expected_evidence=tuple(sorted({category for item in obligations for category in item.required_evidence_categories})),
        estimated_turns=min(config.session_limits.model_turns, max(1, len(obligations))),
        priority=_priority(obligations),
    )


def fallback_assignment_plan(
    obligations: Iterable[CoverageObligation], topology: Mapping[str, Any], runtime_config: RuntimeConfig,
) -> AssignmentPlan:
    """Create a policy-preserving deterministic plan, recording capacity overflow."""
    groups: dict[str, list[CoverageObligation]] = defaultdict(list)
    components = _component_paths(topology)
    for obligation in _assignable_obligations(obligations):
        groups[_fallback_group(obligation, components)].append(obligation)
    ordered = sorted(groups.items(), key=lambda item: (_PRIORITY_RANK[_priority(item[1])], item[0]))
    assignments = tuple(
        _fallback_assignment(key, tuple(sorted(items, key=lambda item: item.id)), runtime_config)
        for key, items in ordered[:runtime_config.max_sessions]
    )
    unassigned = tuple(sorted(obligation.id for _, items in ordered[runtime_config.max_sessions:] for obligation in items))
    return AssignmentPlan(assignments=assignments, unassigned_obligation_ids=unassigned)


def planner_prompt(
    obligations: Iterable[CoverageObligation], topology: Mapping[str, Any], runtime_config: RuntimeConfig,
) -> dict[str, Any]:
    """Return the planner's immutable, structured input surface."""
    return {
        "required_assignment_fields": list(_REQUIRED_FIELDS),
        "obligations": {
            item.id: {
                "origin": item.origin, "subject": item.subject,
                "required_evidence": list(item.required_evidence_categories),
                "risk_tier": item.risk_tier, "scope": list(item.scope),
                "seed_hints": list(item.seed_hints), "recipe_id": item.recipe_id,
                "recipe_execution": _recipe_execution(item),
                "requires_independent_verification": item.requires_independent_verification,
            }
            for item in _assignable_obligations(obligations)
        },
        "topology": topology,
        "budget": {
            "review_deadline_sec": runtime_config.review_deadline_sec,
            "concurrency": runtime_config.concurrency,
            "max_sessions": runtime_config.max_sessions,
            "max_turns_per_session": runtime_config.session_limits.model_turns,
        },
    }
