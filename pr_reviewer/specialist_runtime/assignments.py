"""Validate model-proposed specialist work without weakening immutable coverage."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
import re
from typing import Any

from .policy import RuntimeConfig
from .types import CoverageObligation


_REQUIRED_FIELDS = (
    "id", "title", "objective", "obligation_ids", "lenses", "seed_paths",
    "boundary_paths", "priority",
    "overlap_justification",
)
_PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
_MAX_CHANGED_CONTEXT_PATHS = 12
_MAX_CHANGED_CONTEXT_ITEMS = 5


class AssignmentPlanError(ValueError):
    """A model plan failed deterministic, policy-owned validation."""

    def __init__(self, errors: Iterable[str] | str) -> None:
        self.errors = (errors,) if isinstance(errors, str) else tuple(errors)
        super().__init__("; ".join(self.errors))


@dataclass(frozen=True)
class ObligationBrief:
    """Bounded controller-authored context for one immutable obligation ID."""

    obligation_id: str
    subject: str
    explanation: str
    risk_tier: str
    required_evidence: tuple[str, ...]
    satisfaction_predicates: tuple[str, ...]
    scope: tuple[str, ...]


@dataclass(frozen=True)
class ChangedPathContext:
    """Bounded orientation to changed behavior within assignment scope."""

    path: str
    change_type: str
    symbols: tuple[str, ...] = ()
    hunk_summaries: tuple[str, ...] = ()
    action_inputs: tuple[str, ...] = ()
    workflow_steps: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewFamilyBrief:
    """Controller-owned coherent view over atomic obligations."""

    family_id: str
    obligation_ids: tuple[str, ...]
    changed_paths: tuple[str, ...]
    risk_tier: str
    evidence_categories: tuple[str, ...]


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
    primary_obligation_ids: tuple[str, ...] = ()
    obligation_briefs: tuple[ObligationBrief, ...] = ()
    changed_context: tuple[ChangedPathContext, ...] = ()
    changed_context_omitted_paths: int = 0
    families: tuple[ReviewFamilyBrief, ...] = ()
    model_turn_limit: int = 0
    tool_call_limit: int = 0

    @property
    def assignment_id(self) -> str:
        """Compatibility with the existing immutable assignment vocabulary."""
        return self.id


@dataclass(frozen=True)
class AssignmentPlan:
    assignments: tuple[Assignment, ...]
    unassigned_obligation_ids: tuple[str, ...] = ()
    unassigned_obligation_reasons: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class PlannerTransformationResult:
    plan: AssignmentPlan
    ignored: tuple[str, ...] = ()


def _assignable_obligations(obligations: Iterable[CoverageObligation]) -> tuple[CoverageObligation, ...]:
    """Exclude Task 3's non-mandatory, evidence-free lifecycle bookkeeping."""
    return tuple(item for item in obligations if item.mandatory and item.required_evidence_categories)


def _validated_assignable_obligations(
    obligations: Iterable[CoverageObligation],
) -> tuple[CoverageObligation, ...]:
    items = tuple(obligations)
    missing_evidence = sorted(item.id for item in items if item.mandatory and not item.required_evidence_categories)
    if missing_evidence:
        raise AssignmentPlanError(
            "mandatory obligation has no required evidence: " + ", ".join(missing_evidence)
        )
    return _assignable_obligations(items)


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
    # Retained on the wire for backwards compatibility only. Scheduling weight
    # is derived by the controller after the immutable ownership is validated.
    estimated_turns = 1
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

    expected_evidence = tuple(sorted({
        evidence for obligation_id in obligation_ids
        if obligation_id in obligation_by_id
        for evidence in obligation_by_id[obligation_id].required_evidence_categories
    }))
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


def _validate_overlap(
    assignments: tuple[Assignment, ...], obligation_by_id: Mapping[str, CoverageObligation],
) -> list[str]:
    errors: list[str] = []
    owners: dict[str, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        for obligation_id in assignment.obligation_ids:
            owners[obligation_id].append(assignment)
    for obligation_id, shared in sorted(owners.items()):
        if len(shared) < 2:
            continue
        if _priority((obligation_by_id[obligation_id],)) not in {"critical", "high"}:
            errors.append(f"shared obligation '{obligation_id}' is only allowed for high or critical risk")
        if any(not assignment.overlap_justification for assignment in shared):
            errors.append(f"shared obligation '{obligation_id}' requires overlap justification")
        if len({assignment.objective for assignment in shared}) != len(shared) or (
            len({assignment.lenses for assignment in shared}) != len(shared)
        ):
            errors.append(f"shared obligation '{obligation_id}' has no distinct analytical focus")
    return errors


def _with_primary_ownership(assignments: tuple[Assignment, ...]) -> tuple[Assignment, ...]:
    owners: dict[str, list[str]] = defaultdict(list)
    for assignment in assignments:
        for obligation_id in assignment.obligation_ids:
            owners[obligation_id].append(assignment.id)
    primary_by_assignment: dict[str, list[str]] = defaultdict(list)
    for obligation_id, assignment_ids in owners.items():
        primary_by_assignment[min(assignment_ids)].append(obligation_id)
    return tuple(
        replace(assignment, primary_obligation_ids=tuple(sorted(primary_by_assignment[assignment.id])))
        for assignment in assignments
    )


def _deadline_turn_capacity(config: RuntimeConfig) -> int:
    return _per_lane_deadline_turn_capacity(config) * config.concurrency


def _per_lane_deadline_turn_capacity(config: RuntimeConfig) -> int:
    exploration_percent = config.phase_shares.initial + config.phase_shares.followup
    exploration_seconds = (config.review_deadline_sec * exploration_percent) // 100
    return exploration_seconds // config.model_request_timeout_sec


def _validate_budget(assignments: tuple[Assignment, ...], config: RuntimeConfig) -> list[str]:
    errors: list[str] = []
    if len(assignments) > config.max_sessions:
        errors.append(f"session cap exceeded: {len(assignments)} assignments > {config.max_sessions}")
    global_session_cap = min(
        config.max_sessions,
        config.max_total_model_turns,
        config.max_total_tool_calls,
    )
    if len(assignments) > global_session_cap:
        errors.append(
            f"global lease cannot admit {len(assignments)} positive-budget sessions"
        )
    return errors


def _with_scheduling_weights(
    assignments: tuple[Assignment, ...], config: RuntimeConfig,
) -> tuple[Assignment, ...]:
    """Derive a coarse ordering hint without treating it as runtime capacity."""
    limit = max(1, config.session_limits.model_turns)
    weights = tuple(
        {"critical": 4, "high": 3, "normal": 2, "low": 1}.get(item.priority, 2)
        for item in assignments
    )

    def leases(total: int, per_session: int) -> tuple[int, ...]:
        if not assignments:
            return ()
        remaining = max(0, total)
        result = [0] * len(assignments)
        for index in sorted(range(len(assignments)), key=lambda i: (-weights[i], assignments[i].id)):
            if remaining <= 0:
                break
            result[index] = 1
            remaining -= 1
        while remaining > 0:
            eligible = [
                index for index in range(len(assignments))
                if result[index] < per_session
            ]
            if not eligible:
                break
            index = min(
                eligible,
                key=lambda i: (result[i] / weights[i], -weights[i], assignments[i].id),
            )
            result[index] += 1
            remaining -= 1
        return tuple(result)

    turn_limits = leases(config.max_total_model_turns, limit)
    tool_limits = leases(
        config.max_total_tool_calls,
        max(1, config.session_limits.tool_calls),
    )
    return tuple(
        replace(
            item,
            estimated_turns=min(limit, max(1, len(item.obligation_ids))),
            model_turn_limit=turn_limits[index],
            tool_call_limit=tool_limits[index],
        )
        for index, item in enumerate(assignments)
    )


def validate_assignment_plan(
    raw: Mapping[str, Any], obligations: Iterable[CoverageObligation], topology: Mapping[str, Any],
    runtime_config: RuntimeConfig,
) -> AssignmentPlan:
    """Validate model grouping; the immutable obligation set remains authoritative."""
    assignable = _validated_assignable_obligations(obligations)
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
    assignments = tuple(
        _with_semantic_brief(item, obligation_by_id, topology)
        for item in parsed
    )
    if len({item.id for item in assignments}) != len(assignments):
        errors.append("assignment ids must be unique")
    assigned_ids = {obligation_id for item in assignments for obligation_id in item.obligation_ids}
    missing = sorted(set(obligation_by_id) - assigned_ids)
    if missing:
        errors.append(f"unassigned mandatory obligations: {', '.join(missing)}")
    for assignment in assignments:
        effective_priority = _priority(
            obligation_by_id[obligation_id] for obligation_id in assignment.obligation_ids
            if obligation_id in obligation_by_id
        )
        if assignment.priority != effective_priority:
            errors.append(
                f"assignment '{assignment.id}' priority must equal immutable risk "
                f"'{effective_priority}'"
            )
    errors.extend(_validate_overlap(assignments, obligation_by_id))
    errors.extend(_validate_recipe_execution(assignments, obligation_by_id))
    errors.extend(_validate_budget(assignments, runtime_config))
    if errors:
        raise AssignmentPlanError(errors)
    return AssignmentPlan(assignments=_with_primary_ownership(
        _with_scheduling_weights(assignments, runtime_config)
    ))


def _bounded_transformation_error(index: int, reason: str) -> str:
    clean = " ".join(str(reason).split())
    return f"transformation {index}: {clean[:300]}"


def _is_isolated_assignment(
    assignment: Assignment, obligation_by_id: Mapping[str, CoverageObligation],
) -> bool:
    return any(
        _recipe_execution(obligation_by_id[obligation_id]) in {"dedicated", "independent"}
        for obligation_id in assignment.obligation_ids
        if obligation_id in obligation_by_id
    )


def _replace_assignment(
    assignments: list[Assignment], assignment_id: str, replacement: Assignment,
) -> None:
    assignments[assignments.index(next(
        item for item in assignments if item.id == assignment_id
    ))] = replacement


def _rebuild_assignment(
    original: Assignment,
    obligation_ids: Iterable[str],
    obligation_by_id: Mapping[str, CoverageObligation],
    config: RuntimeConfig,
    topology: Mapping[str, Any],
    *,
    assignment_id: str | None = None,
) -> Assignment:
    ids = tuple(obligation_ids)
    obligations = tuple(obligation_by_id[item] for item in ids)
    seed_paths = tuple(sorted({
        path.replace("\\", "/").strip("/")
        for obligation in obligations for path in obligation.seed_hints
    }))
    boundary_paths = tuple(sorted({
        path.replace("\\", "/").strip("/")
        for obligation in obligations for path in obligation.scope
        if path.replace("\\", "/").strip("/") not in seed_paths
    }))
    changed_context, omitted = _changed_context(obligations, topology)
    title, objective = _semantic_assignment_text(obligations)
    return replace(
        original,
        id=assignment_id or original.id,
        title=title,
        objective=objective,
        obligation_ids=ids,
        recipe_ids=tuple(sorted({
            item.recipe_id for item in obligations if item.recipe_id
        })),
        seed_paths=seed_paths,
        boundary_paths=boundary_paths,
        expected_evidence=tuple(sorted({
            category for item in obligations
            for category in item.required_evidence_categories
        })),
        priority=_priority(obligations),
        estimated_turns=min(
            max(1, config.session_limits.model_turns), max(1, len(ids)),
        ),
        primary_obligation_ids=(),
        obligation_briefs=_obligation_briefs(obligations),
        changed_context=changed_context,
        changed_context_omitted_paths=omitted,
    )


def _transformed_plan_errors(
    assignments: tuple[Assignment, ...],
    base_plan: AssignmentPlan,
    obligation_by_id: Mapping[str, CoverageObligation],
    runtime_config: RuntimeConfig,
) -> list[str]:
    errors: list[str] = []
    assignment_ids = [item.id for item in assignments]
    if len(assignment_ids) != len(set(assignment_ids)):
        errors.append("assignment IDs must remain unique")
    base_owned = {
        obligation_id
        for item in base_plan.assignments
        for obligation_id in item.obligation_ids
    }
    transformed_owned = {
        obligation_id
        for item in assignments
        for obligation_id in item.obligation_ids
    }
    if transformed_owned != base_owned:
        errors.append("transformations must preserve complete base obligation ownership")
    for assignment in assignments:
        if not assignment.obligation_ids:
            errors.append(f"assignment '{assignment.id}' must own an obligation")
            continue
        unknown = sorted(set(assignment.obligation_ids) - set(obligation_by_id))
        if unknown:
            errors.append(
                f"assignment '{assignment.id}' contains unknown obligations: "
                + ", ".join(unknown)
            )
            continue
        obligations = tuple(
            obligation_by_id[item] for item in assignment.obligation_ids
        )
        if assignment.priority != _priority(obligations):
            errors.append(
                f"assignment '{assignment.id}' changed immutable risk priority"
            )
        allowed_paths = {
            path.replace("\\", "/").strip("/")
            for obligation in obligations
            for path in (*obligation.scope, *obligation.seed_hints)
        }
        outside = sorted(
            set((*assignment.seed_paths, *assignment.boundary_paths)) - allowed_paths
        )
        if outside:
            errors.append(
                f"assignment '{assignment.id}' paths outside immutable obligation scope: "
                + ", ".join(outside)
            )
    errors.extend(_validate_overlap(assignments, obligation_by_id))
    errors.extend(_validate_recipe_execution(assignments, obligation_by_id))
    errors.extend(_validate_budget(assignments, runtime_config))
    return errors


def _next_split_id(base_id: str, used_ids: set[str]) -> str:
    suffix = 2
    while f"{base_id}-split-{suffix}" in used_ids:
        suffix += 1
    assignment_id = f"{base_id}-split-{suffix}"
    used_ids.add(assignment_id)
    return assignment_id


def apply_planner_transformations(
    raw: Mapping[str, Any],
    base_plan: AssignmentPlan,
    obligations: Iterable[CoverageObligation],
    runtime_config: RuntimeConfig,
    *,
    topology: Mapping[str, Any],
) -> PlannerTransformationResult:
    """Apply optional planner improvements without transferring ownership authority."""
    assignable = _validated_assignable_obligations(obligations)
    obligation_by_id = {item.id: item for item in assignable}
    transformations = raw.get("transformations") if isinstance(raw, Mapping) else None
    if not isinstance(transformations, list):
        return PlannerTransformationResult(
            base_plan, ("planner result transformations must be an array",),
        )
    assignments = list(base_plan.assignments)
    ignored: list[str] = []
    selected_transformations = transformations[:64]
    if len(transformations) > len(selected_transformations):
        ignored.append(
            f"planner returned {len(transformations)} transformations; only the first "
            f"{len(selected_transformations)} were considered"
        )
    for index, transformation in enumerate(selected_transformations):
        before = list(assignments)
        try:
            if not isinstance(transformation, Mapping):
                raise ValueError("must be an object")
            kind = transformation.get("kind")
            by_id = {item.id: item for item in assignments}
            if kind == "reorder":
                requested = transformation.get("assignment_ids")
                if not isinstance(requested, list) or any(
                    not isinstance(item, str) for item in requested
                ):
                    raise ValueError("reorder assignment_ids must be an array of strings")
                if len(set(requested)) != len(requested) or any(
                    item not in by_id for item in requested
                ):
                    raise ValueError("reorder references unknown or duplicate assignment IDs")
                assignments = [by_id[item] for item in requested] + [
                    item for item in assignments if item.id not in requested
                ]
            elif kind == "improve":
                assignment_id = transformation.get("assignment_id")
                if not isinstance(assignment_id, str) or assignment_id not in by_id:
                    raise ValueError("improve references an unknown assignment ID")
                item = by_id[assignment_id]
                changes: dict[str, object] = {}
                for field in ("objective",):
                    if field in transformation:
                        value = transformation[field]
                        if not isinstance(value, str) or not value.strip():
                            raise ValueError(f"{field} must be a non-empty string")
                        changes[field] = value.strip()
                if "lenses" in transformation:
                    errors: list[str] = []
                    changes["lenses"] = _strings(
                        transformation["lenses"], "lenses", errors,
                    )
                    if errors:
                        raise ValueError("; ".join(errors))
                allowed_paths = {
                    path.replace("\\", "/").strip("/")
                    for obligation_id in item.obligation_ids
                    for path in (
                        *obligation_by_id[obligation_id].scope,
                        *obligation_by_id[obligation_id].seed_hints,
                    )
                }
                for field in ("seed_paths", "boundary_paths"):
                    if field in transformation:
                        errors = []
                        paths = _paths(transformation[field], field, errors)
                        outside = sorted(set(paths) - allowed_paths)
                        if outside:
                            errors.append(
                                "paths outside immutable obligation scope: "
                                + ", ".join(outside)
                            )
                        if errors:
                            raise ValueError("; ".join(errors))
                        changes[field] = paths
                _replace_assignment(assignments, assignment_id, replace(item, **changes))
            elif kind == "merge":
                target_id = transformation.get("target_assignment_id")
                source_ids = transformation.get("source_assignment_ids")
                # Models commonly compress the two merge fields into the
                # reorder-like ``assignment_ids`` shape.  This is safe to
                # normalize because all IDs still pass the same immutable
                # ownership and isolation checks below: first is the target,
                # remaining IDs are the sources.
                if (
                    target_id is None
                    and isinstance(transformation.get("assignment_ids"), list)
                ):
                    merged_ids = transformation["assignment_ids"]
                    if merged_ids:
                        target_id = merged_ids[0]
                        source_ids = merged_ids[1:]
                if not isinstance(target_id, str) or target_id not in by_id:
                    raise ValueError("merge references an unknown target assignment ID")
                if not isinstance(source_ids, list) or not source_ids or any(
                    not isinstance(item, str) for item in source_ids
                ):
                    raise ValueError("merge source_assignment_ids must be a non-empty array")
                merge_ids = [target_id, *source_ids]
                if len(set(merge_ids)) != len(merge_ids) or any(
                    item not in by_id for item in merge_ids
                ):
                    raise ValueError("merge references unknown or duplicate assignment IDs")
                merged = [by_id[item] for item in merge_ids]
                if any(_is_isolated_assignment(item, obligation_by_id) for item in merged):
                    raise ValueError("cannot merge an isolated recipe assignment")
                target = by_id[target_id]
                obligation_ids = tuple(dict.fromkeys(
                    obligation_id for item in merged
                    for obligation_id in item.obligation_ids
                ))
                rebuilt = _rebuild_assignment(
                    target, obligation_ids, obligation_by_id, runtime_config,
                    topology,
                )
                assignments = [
                    rebuilt if item.id == target_id else item
                    for item in assignments if item.id not in source_ids
                ]
            elif kind == "split":
                assignment_id = transformation.get("assignment_id")
                groups = transformation.get("obligation_groups")
                if not isinstance(assignment_id, str) or assignment_id not in by_id:
                    raise ValueError("split references an unknown assignment ID")
                original = by_id[assignment_id]
                if _is_isolated_assignment(original, obligation_by_id):
                    raise ValueError("cannot split an isolated recipe assignment")
                if not isinstance(groups, list) or len(groups) < 2:
                    raise ValueError("split obligation_groups must contain at least two groups")
                parsed_groups: list[tuple[str, ...]] = []
                seen: set[str] = set()
                for group in groups:
                    if not isinstance(group, list) or not group or any(
                        not isinstance(item, str) for item in group
                    ):
                        raise ValueError("split groups must be non-empty arrays of obligation IDs")
                    ids = tuple(group)
                    if set(ids) - set(original.obligation_ids) or seen.intersection(ids):
                        raise ValueError("split uses foreign or duplicate obligation IDs")
                    seen.update(ids)
                    parsed_groups.append(ids)
                remainder = tuple(
                    item for item in original.obligation_ids if item not in seen
                )
                if remainder:
                    parsed_groups.append(remainder)
                if len(assignments) + len(parsed_groups) - 1 > runtime_config.max_sessions:
                    raise ValueError("split exceeds controller-owned session cap")
                used_ids = {
                    item.id for item in assignments if item.id != original.id
                }
                split_items = [
                    _rebuild_assignment(
                        original, group, obligation_by_id, runtime_config,
                        topology,
                        assignment_id=(
                            original.id if split_index == 0
                            else _next_split_id(original.id, used_ids)
                        ),
                    )
                    for split_index, group in enumerate(parsed_groups)
                ]
                position = assignments.index(original)
                assignments[position:position + 1] = split_items
            else:
                raise ValueError(f"unsupported transformation kind: {kind!r}")
            invariant_errors = _transformed_plan_errors(
                tuple(assignments), base_plan, obligation_by_id, runtime_config,
            )
            if invariant_errors:
                raise ValueError("; ".join(invariant_errors))
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            assignments = before
            ignored.append(_bounded_transformation_error(index, str(exc)))
    final_errors = _transformed_plan_errors(
        tuple(assignments), base_plan, obligation_by_id, runtime_config,
    )
    if final_errors:
        ignored.append(_bounded_transformation_error(
            len(selected_transformations), "; ".join(final_errors),
        ))
        assignments = list(base_plan.assignments)
    plan = AssignmentPlan(
        assignments=_with_primary_ownership(
            _with_scheduling_weights(tuple(assignments), runtime_config)
        ),
        unassigned_obligation_ids=base_plan.unassigned_obligation_ids,
        unassigned_obligation_reasons=base_plan.unassigned_obligation_reasons,
    )
    return PlannerTransformationResult(plan=plan, ignored=tuple(ignored))


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


def _bounded_text(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _obligation_briefs(
    obligations: Iterable[CoverageObligation],
) -> tuple[ObligationBrief, ...]:
    return tuple(
        ObligationBrief(
            obligation_id=item.id,
            subject=_bounded_text(item.subject, 160),
            explanation=_bounded_text(item.explanation, 500),
            risk_tier=item.risk_tier,
            required_evidence=tuple(
                _bounded_text(value, 80)
                for value in item.required_evidence_categories[:16]
            ),
            satisfaction_predicates=tuple(
                _bounded_text(value, 240)
                for value in item.satisfaction_predicates[:12]
            ),
            scope=tuple(item.scope[:32]),
        )
        for item in obligations
    )


def _bounded_fact_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(
        text for text in (
            _bounded_text(item, 160)
            for item in value[:_MAX_CHANGED_CONTEXT_ITEMS]
        )
        if text
    )


def _changed_context(
    obligations: Iterable[CoverageObligation],
    topology: Mapping[str, Any],
) -> tuple[tuple[ChangedPathContext, ...], int]:
    obligations = tuple(obligations)
    relevant_paths = {
        path.replace("\\", "/").strip("/")
        for obligation in obligations
        for path in (*obligation.scope, *obligation.seed_hints)
        if path
    }
    changed_paths = {
        str(path).replace("\\", "/").strip("/")
        for path in topology.get("changed_files", ())
        if isinstance(path, str) and path
    }
    facts_value = topology.get("changed_contract_facts", {})
    facts = facts_value if isinstance(facts_value, Mapping) else {}
    seed_paths = {
        path.replace("\\", "/").strip("/")
        for obligation in obligations for path in obligation.seed_hints if path
    }
    minimum_scope = {
        path: min(
            len(set((*obligation.scope, *obligation.seed_hints)))
            for obligation in obligations
            if path in {
                item.replace("\\", "/").strip("/")
                for item in (*obligation.scope, *obligation.seed_hints)
            }
        )
        for path in relevant_paths
    }
    scoped_paths = sorted(
        (
            path for path in relevant_paths
            if path in changed_paths or path in facts
        ),
        key=lambda path: (
            0 if path in seed_paths else 1,
            minimum_scope[path],
            1 if path.lower().endswith((".md", ".adoc", ".rst", ".txt")) else 0,
            path,
        ),
    )
    selected = scoped_paths[:_MAX_CHANGED_CONTEXT_PATHS]
    context: list[ChangedPathContext] = []
    for path in selected:
        raw = facts.get(path, {})
        fact = raw if isinstance(raw, Mapping) else {}
        context.append(ChangedPathContext(
            path=path,
            change_type=_bounded_text(fact.get("change_type", "changes"), 24)
            or "changes",
            symbols=_bounded_fact_strings(fact.get("symbols")),
            hunk_summaries=_bounded_fact_strings(fact.get("hunk_summaries")),
            action_inputs=_bounded_fact_strings(fact.get("action_inputs")),
            workflow_steps=_bounded_fact_strings(fact.get("workflow_steps")),
        ))
    return tuple(context), max(0, len(scoped_paths) - len(selected))


def _with_semantic_brief(
    assignment: Assignment,
    obligation_by_id: Mapping[str, CoverageObligation],
    topology: Mapping[str, Any],
) -> Assignment:
    obligations = tuple(
        obligation_by_id[item]
        for item in assignment.obligation_ids
        if item in obligation_by_id
    )
    changed_context, omitted = _changed_context(obligations, topology)
    return replace(
        assignment,
        obligation_briefs=_obligation_briefs(obligations),
        changed_context=changed_context,
        changed_context_omitted_paths=omitted,
        families=_review_families(obligations, topology),
    )


def _review_families(
    obligations: Iterable[CoverageObligation], topology: Mapping[str, Any],
) -> tuple[ReviewFamilyBrief, ...]:
    component_paths = _component_paths(topology)
    grouped: dict[tuple[object, ...], list[CoverageObligation]] = defaultdict(list)
    for obligation in sorted(obligations, key=lambda item: item.id):
        scope = set(obligation.scope).union(obligation.seed_hints)
        components = tuple(sorted(
            component for component, paths in component_paths.items()
            if scope.intersection(paths)
        ))
        isolated = bool(
            obligation.requires_independent_verification
            or _recipe_execution(obligation) in {"dedicated", "independent"}
        )
        key = (
            "isolated:" + obligation.id if isolated else "ordinary",
            obligation.risk_tier,
            obligation.unresolved_policy,
            components,
            obligation.recipe_id or "",
            tuple(sorted(obligation.required_evidence_categories)),
        )
        grouped[key].append(obligation)

    families: list[ReviewFamilyBrief] = []
    for key, items in sorted(grouped.items(), key=lambda item: repr(item[0])):
        batch: list[CoverageObligation] = []
        paths: set[str] = set()
        for obligation in items:
            item_paths = set((*obligation.scope, *obligation.seed_hints))
            if batch and (len(batch) >= 10 or len(paths.union(item_paths)) > 8):
                families.append(_family_brief(len(families) + 1, batch))
                batch, paths = [], set()
            batch.append(obligation)
            paths.update(item_paths)
        if batch:
            families.append(_family_brief(len(families) + 1, batch))
    return tuple(families)


def _family_brief(index: int, items: Iterable[CoverageObligation]) -> ReviewFamilyBrief:
    obligations = tuple(items)
    return ReviewFamilyBrief(
        family_id=f"family:{index}",
        obligation_ids=tuple(item.id for item in obligations),
        changed_paths=tuple(sorted({
            path for item in obligations for path in (*item.scope, *item.seed_hints)
        })),
        risk_tier=_priority(obligations),
        evidence_categories=tuple(sorted({
            category for item in obligations for category in item.required_evidence_categories
        })),
    )


def _semantic_theme(obligation: CoverageObligation) -> str:
    subject = _bounded_text(obligation.subject, 80) or "assigned behavior"
    lowered = subject.lower()
    if "interaction" in obligation.required_evidence_categories and "interaction" not in lowered:
        subject += " interaction"
    elif any(
        "test" in category.lower()
        for category in obligation.required_evidence_categories
    ) and "test" not in lowered:
        subject += " tests"
    if (
        obligation.recipe_id
        and obligation.recipe_id.lower() not in subject.lower()
    ):
        subject = f"{_bounded_text(obligation.recipe_id, 48)} {subject}"
    return subject


def _join_semantic_themes(obligations: Iterable[CoverageObligation]) -> str:
    themes = tuple(dict.fromkeys(
        _semantic_theme(item)
        for item in sorted(obligations, key=lambda item: item.id)
    ))
    visible = themes[:3]
    if not visible:
        return "assigned behavior"
    if len(visible) == 1:
        result = visible[0]
    else:
        result = ", ".join(visible[:-1]) + " and " + visible[-1]
    if len(themes) > len(visible):
        result += f" and {len(themes) - len(visible)} related behaviors"
    return result


def _semantic_assignment_text(
    obligations: Iterable[CoverageObligation],
) -> tuple[str, str]:
    themes = _join_semantic_themes(obligations)
    return (
        f"Review {themes}",
        (
            f"Verify changed behavior for {themes} from the scoped diffs, "
            "required evidence, and satisfaction predicates."
        ),
    )


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


def _fallback_assignment(
    key: str,
    obligations: tuple[CoverageObligation, ...],
    config: RuntimeConfig,
    topology: Mapping[str, Any],
) -> Assignment:
    suffix = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-") or "review"
    seed_paths = tuple(sorted({path for item in obligations for path in item.seed_hints}))
    boundary_paths = tuple(sorted({path for item in obligations for path in item.scope if path not in seed_paths}))
    title, objective = _semantic_assignment_text(obligations)
    assignment = Assignment(
        id=f"fallback-{suffix}", title=title, objective=objective,
        obligation_ids=tuple(item.id for item in obligations),
        recipe_ids=tuple(sorted({item.recipe_id for item in obligations if item.recipe_id})),
        lenses=("deterministic-coverage",), seed_paths=seed_paths, boundary_paths=boundary_paths,
        expected_evidence=tuple(sorted({category for item in obligations for category in item.required_evidence_categories})),
        estimated_turns=len(obligations),
        priority=_priority(obligations),
    )
    return _with_semantic_brief(
        assignment, {item.id: item for item in obligations}, topology,
    )


def fallback_assignment_plan(
    obligations: Iterable[CoverageObligation], topology: Mapping[str, Any], runtime_config: RuntimeConfig,
) -> AssignmentPlan:
    """Create complete controller-owned coverage within the hard session cap."""
    groups: dict[str, list[CoverageObligation]] = defaultdict(list)
    session_cap = min(
        runtime_config.max_sessions,
        runtime_config.max_total_model_turns,
        runtime_config.max_total_tool_calls,
    )
    components = _component_paths(topology)
    for obligation in _validated_assignable_obligations(obligations):
        groups[_fallback_group(obligation, components)].append(obligation)
    ordered = sorted(groups.items(), key=lambda item: (_PRIORITY_RANK[_priority(item[1])], item[0]))
    isolated_candidates: list[tuple[str, list[CoverageObligation]]] = []
    ordinary_groups: list[tuple[str, list[CoverageObligation]]] = []
    for key, group in ordered:
        items = sorted(group, key=lambda item: item.id)
        if key.startswith("recipe:dedicated:") or key.startswith("recipe:independent:"):
            isolated_candidates.append((key, items))
        else:
            ordinary_groups.append((key, items))

    ordinary_candidates: list[tuple[str, list[CoverageObligation]]] = []
    if ordinary_groups and session_cap > 0:
        # Preserve room for isolated candidates while always creating at least
        # one ordinary contender so immutable risk, not isolation kind, decides
        # who receives the final hard session slot.
        ordinary_items = sorted(
            (item for _, group in ordinary_groups for item in group),
            key=lambda item: (_PRIORITY_RANK[item.risk_tier], item.id),
        )
        ordinary_paths = {
            path for item in ordinary_items for path in (*item.scope, *item.seed_hints)
        }
        workload_slots = max(
            min(len(ordinary_groups), 6),
            len({_priority(group) for _, group in ordinary_groups}),
            (len(ordinary_items) + 5) // 6,
            (len(ordinary_paths) + 7) // 8,
        )
        available_slots = max(
            1,
            session_cap
            - min(len(isolated_candidates), session_cap - 1),
        )
        ordinary_slots = min(available_slots, max(1, workload_slots))
        bucket_count = min(ordinary_slots, len(ordinary_items))
        buckets: list[list[CoverageObligation]] = [[] for _ in range(bucket_count)]
        for index, obligation in enumerate(ordinary_items):
            buckets[index % bucket_count].append(obligation)
        ordinary_candidates = [
            (f"combined:{index + 1}", items)
            for index, items in enumerate(buckets)
            if items
        ]

    candidates = sorted(
        (*isolated_candidates, *ordinary_candidates),
        key=lambda item: (_PRIORITY_RANK[_priority(item[1])], item[0]),
    )
    admitted = candidates[:session_cap]
    overflow_groups = candidates[session_cap:]
    assignments = [
        _fallback_assignment(
            key,
            tuple(sorted(items, key=lambda item: item.id)),
            runtime_config,
            topology,
        )
        for key, items in admitted
    ]
    unassigned = tuple(sorted(
        obligation.id for _, items in overflow_groups for obligation in items
    ))
    reasons = tuple(
        (
            obligation_id,
            "max_sessions exhausted after deterministic risk and tie-break ordering",
        )
        for obligation_id in unassigned
    )
    return AssignmentPlan(
        assignments=_with_primary_ownership(
            _with_scheduling_weights(tuple(assignments), runtime_config)
        ),
        unassigned_obligation_ids=unassigned,
        unassigned_obligation_reasons=reasons,
    )


def planner_prompt(
    obligations: Iterable[CoverageObligation], topology: Mapping[str, Any], runtime_config: RuntimeConfig,
) -> dict[str, Any]:
    """Return the planner's immutable, structured input surface."""
    return {
        "required_assignment_fields": list(_REQUIRED_FIELDS),
        "authority": {
            "obligation_ids": "exact immutable identifiers; do not invent or paraphrase",
            "paths": (
                "seed_paths and boundary_paths may contain only paths from each assigned "
                "obligation's scope or seed_hints"
            ),
            "expected_evidence": (
                "derived by the controller from assigned obligation_ids; planner values are ignored"
            ),
        },
        "obligations": {
            item.id: {
                "origin": item.origin, "subject": item.subject,
                "required_evidence": list(item.required_evidence_categories),
                "risk_tier": item.risk_tier, "scope": list(item.scope),
                "seed_hints": list(item.seed_hints), "recipe_id": item.recipe_id,
                "recipe_execution": _recipe_execution(item),
                "requires_independent_verification": item.requires_independent_verification,
            }
            for item in _validated_assignable_obligations(obligations)
        },
        "topology": topology,
        "budget": {
            "review_deadline_sec": runtime_config.review_deadline_sec,
            "concurrency": runtime_config.concurrency,
            "max_sessions": runtime_config.max_sessions,
            "max_turns_per_session": runtime_config.session_limits.model_turns,
            "deadline_turn_capacity": _deadline_turn_capacity(runtime_config),
        },
    }
