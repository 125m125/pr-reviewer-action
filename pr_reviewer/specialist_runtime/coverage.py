"""Deterministic mandatory-coverage derivation and evidence accounting."""

from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass, replace
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any

from pr_reviewer.specialists import classify_file_roles

from .assignments import Assignment
from .evidence import EvidenceRecord, EvidenceSnapshot
from .obligation_assessment import ObligationAssessment, ObligationAssessmentLedger, ObligationDisposition
from .policy import RecipePolicy, ReviewPolicy
from .types import (
    CoverageObligation,
    ObligationStatus,
    RecipeStatus,
    SessionCheckpoint,
    SpecialistAssignment,
)


def _slug(value: object, fallback: str = "review") -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return normalized or fallback


def _paths(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(sorted({str(item).replace("\\", "/").strip("/") for item in value if str(item).strip()}))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(sorted({str(item).strip() for item in value if str(item).strip()}))


def _obligation_id(origin: str, subject: str, evidence_category: str) -> str:
    """Build a readable, stable identifier from deterministic inputs only."""
    raw_identity = "\x1f".join((origin, subject, evidence_category)).encode("utf-8")
    identity_digest = hashlib.sha256(raw_identity).hexdigest()[:12]
    return "obligation:" + ":".join(
        (_slug(origin), _slug(subject), _slug(evidence_category), identity_digest)
    )


@dataclass(frozen=True)
class _RecipeAccountingObligation(CoverageObligation):
    """Private, tuple-safe lifecycle marker excluded from evidence work."""

    recipe_status: RecipeStatus = RecipeStatus.NOT_APPLICABLE


@dataclass(frozen=True)
class CoverageSnapshot:
    """Detached coverage state fixed at the beginning of a work wave."""

    obligation_statuses: tuple[tuple[str, ObligationStatus], ...]
    recipe_statuses: tuple[tuple[str, str], ...]
    evidence_by_obligation: tuple[tuple[str, tuple[str, ...]], ...]


@dataclass(frozen=True)
class CoverageReconciliation:
    """Immutable controller projection after one completed work wave."""

    snapshot: CoverageSnapshot
    newly_covered_obligation_ids: tuple[str, ...]
    uncovered_obligation_ids: tuple[str, ...]
    attempted_unresolved_obligation_ids: tuple[str, ...]
    never_covered_obligation_ids: tuple[str, ...]


@dataclass(frozen=True)
class SessionOwnership:
    """Controller-owned link between a durable session and its assignment."""

    session_id: str
    assignment_id: str
    primary_obligation_ids: tuple[str, ...] = ()
    secondary_obligation_ids: tuple[str, ...] = ()
    independent_obligation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("session ownership requires a non-empty session_id")
        if not isinstance(self.assignment_id, str) or not self.assignment_id.strip():
            raise ValueError("session ownership requires a non-empty assignment_id")
        for field_name in (
            "primary_obligation_ids", "secondary_obligation_ids",
            "independent_obligation_ids",
        ):
            values = getattr(self, field_name)
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise ValueError(f"{field_name} must contain non-empty strings")
            if len(set(values)) != len(values):
                raise ValueError(f"{field_name} must not contain duplicates")

    @property
    def obligation_ids(self) -> tuple[str, ...]:
        return tuple(sorted(
            set(self.primary_obligation_ids)
            .union(self.secondary_obligation_ids)
            .union(self.independent_obligation_ids)
        ))


def _normalized_path(value: object) -> str:
    path = str(value).strip().replace("\\", "/")
    if not path:
        return ""
    normalized = str(PurePosixPath(path))
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.strip("/")


def evidence_satisfies_obligation(
    record: EvidenceRecord,
    obligation: CoverageObligation,
) -> bool:
    """Apply the runtime's deterministic evidence satisfaction predicates."""
    if not record.is_usable_for_coverage:
        return False
    predicates = set(obligation.satisfaction_predicates)
    if predicates and "recorded_evidence" not in predicates:
        return False
    scoped_paths = tuple(dict.fromkeys((*obligation.scope, *obligation.seed_hints)))
    if scoped_paths:
        source_path = _normalized_path(record.source_path or "")
        if not source_path:
            return False
        if not any(
            source_path == scope_path or source_path.startswith(scope_path + "/")
            or fnmatch.fnmatchcase(source_path, scope_path)
            for raw_path in scoped_paths
            if (scope_path := _normalized_path(raw_path))
        ):
            return False
    category = record.category.strip().lower()
    return bool(category) and category in {
        item.strip().lower()
        for item in obligation.required_evidence_categories
        if item.strip()
    }


def _associated_collections_satisfying(
    evidence: EvidenceSnapshot,
    record: EvidenceRecord,
    obligation: CoverageObligation,
    *,
    session_id: str | None = None,
) -> tuple[str, ...]:
    satisfying: list[str] = []
    for collection, association in evidence.associations_for(
        record.id, obligation.id,
    ):
        if session_id is not None and collection.session_id != session_id:
            continue
        if any(
            evidence_satisfies_obligation(
                replace(record, category=category), obligation,
            )
            for category in association.categories
        ):
            satisfying.append(collection.id)
    return tuple(satisfying)


def _recipe_accounting_obligation(
    recipe_id: str,
    status: RecipeStatus,
) -> _RecipeAccountingObligation:
    evidence_category = f"status-{status.value}"
    obligation_id = _obligation_id("recipe-accounting", recipe_id, evidence_category)
    return _RecipeAccountingObligation(
        obligation_id=obligation_id,
        origin="recipe-accounting",
        subject=recipe_id,
        required_evidence_categories=(),
        satisfaction_predicates=(f"recipe_status:{status.value}",),
        explanation=f"Recipe '{recipe_id}' is {status.value}.",
        recipe_id=recipe_id,
        mandatory=False,
        recipe_status=status,
    )




def _recipe_matches(recipe: RecipePolicy, topology: Mapping[str, Any], risk_flags: set[str]) -> bool:
    changed_files = _paths(topology.get("changed_files"))
    component_ids = {
        _slug(component.get("id"))
        for component in topology.get("components", [])
        if isinstance(component, Mapping) and component.get("id")
    }
    roles = set(_strings(topology.get("file_roles")))
    match = recipe.match or {}
    checks = {
        "paths_any": lambda wanted: any(
            fnmatch.fnmatchcase(path, pattern) for path in changed_files for pattern in wanted
        ),
        "component_ids_any": lambda wanted: bool(component_ids.intersection(_slug(item) for item in wanted)),
        "risk_flags_any": lambda wanted: bool(risk_flags.intersection(wanted)),
        "file_roles_any": lambda wanted: bool(roles.intersection(wanted)),
    }
    return all(checks[key](wanted) for key, wanted in match.items() if key in checks)


def _rule_matches(
    rule: Mapping[str, Any],
    topology: Mapping[str, Any],
    risk_flags: set[str],
) -> bool:
    changed_files = _paths(topology.get("changed_files"))
    component_ids = {
        _slug(component.get("id"))
        for component in topology.get("components", [])
        if isinstance(component, Mapping) and component.get("id")
    }
    roles = set(_strings(topology.get("file_roles")))
    checks = {
        "paths_any": lambda wanted: any(
            fnmatch.fnmatchcase(path, pattern)
            for path in changed_files for pattern in wanted
        ),
        "component_ids_any": lambda wanted: bool(
            component_ids.intersection(_slug(item) for item in wanted)
        ),
        "risk_flags_any": lambda wanted: bool(risk_flags.intersection(wanted)),
        "file_roles_any": lambda wanted: bool(roles.intersection(wanted)),
    }
    populated = False
    for key, check in checks.items():
        if key not in rule:
            continue
        populated = True
        if not check(rule[key]):
            return False
    return populated


def derive_obligations(
    topology: Mapping[str, Any],
    classification: Mapping[str, Any] | None,
    policy: ReviewPolicy,
) -> tuple[CoverageObligation, ...]:
    """Derive substantive owner questions; file inventories and topology are hints."""
    excluded_paths = tuple(policy.exclude.get("paths", ()))
    excluded_components = set(policy.exclude.get("components", ()))
    changed = tuple(path for path in _paths(topology.get("changed_files")) if not any(
        fnmatch.fnmatchcase(path, pattern) for pattern in excluded_paths
    ))
    component_by_id = {str(item["id"]): item for item in policy.components}
    owner_paths: dict[str, tuple[str, ...]] = {}
    for component in topology.get("components", ()):
        owner = str(component["id"])
        if owner in excluded_components:
            continue
        paths = tuple(path for path in _paths(component.get("changed_files")) if path in changed)
        if paths:
            owner_paths[owner] = paths
    # The topology is authoritative for ownership, but must never silently lose a path.
    accounted = {path for paths in owner_paths.values() for path in paths}
    excluded_owned = {
        path for item in topology.get("components", ()) if item["id"] in excluded_components
        for path in _paths(item.get("changed_files"))
    }
    remainder = tuple(path for path in changed if path not in accounted | excluded_owned)
    if remainder:
        owner_paths["repository-remainder"] = tuple(sorted(set(
            owner_paths.get("repository-remainder", ()) + remainder
        )))
    changed = tuple(path for path in changed if path not in excluded_owned)
    risks = set(_strings(topology.get("risk_flags"))) | set(_strings((classification or {}).get("risk_flags")))
    active_topology = {**topology, "changed_files": changed}
    excluded_recipes = set(policy.exclude.get("recipes", ()))
    excluded_lenses = set(policy.exclude.get("lenses", ()))
    obligations: list[CoverageObligation] = []
    active_recipes: list[tuple[RecipePolicy, str, str]] = []
    rank = {"low": 0, "normal": 1, "high": 2, "critical": 3}
    for recipe in policy.recipes:
        if (recipe.id in excluded_recipes or excluded_lenses.intersection(recipe.lenses)
                or set(recipe.match.get("component_ids_any", ())).intersection(excluded_components)):
            obligations.append(_recipe_accounting_obligation(recipe.id, RecipeStatus.SUPPRESSED_BY_POLICY))
            continue
        rules = [rule for rule in policy.coverage_rules
                 if recipe.id in rule.get("required_recipe_ids", ())
                 and _rule_matches(rule, active_topology, risks)]
        if not changed or (not rules and not _recipe_matches(recipe, active_topology, risks)):
            obligations.append(_recipe_accounting_obligation(recipe.id, RecipeStatus.NOT_APPLICABLE))
            continue
        risk = max((recipe.priority, *(str(rule.get("risk_tier", "high")) for rule in rules)), key=rank.__getitem__)
        unresolved = ("block_when_unresolved" if risk in {"critical", "high"} or any(
            rule.get("unresolved_policy") == "block_when_unresolved" for rule in rules
        ) else "record_unknown")
        active_recipes.append((recipe, risk, unresolved))

    def requirements(recipes: Iterable[RecipePolicy], local: Mapping[str, Any]) -> tuple[Mapping[str, object], ...]:
        return tuple(
            {"id": f"{recipe.id}:{item.id}", "category": item.category,
             "mode": f"one_of:{recipe.id}:{item.mode[7:]}" if item.mode.startswith("one_of:") else item.mode,
             "seed_paths": item.seed_paths, "related_paths": item.related_paths}
            for recipe in recipes for item in recipe.evidence_requirements
            if not item.when or _rule_matches(item.when, local, risks)
        )

    for owner, paths in sorted(owner_paths.items()):
        component = component_by_id.get(owner, {})
        local = {**topology, "changed_files": paths, "components": [{"id": owner}],
                 "file_roles": tuple({role for path in paths for role in classify_file_roles(path)})}
        recipes = [(recipe, risk, unresolved) for recipe, risk, unresolved in active_recipes
                   if recipe.execution == "integrated" and (
                       _recipe_matches(recipe, local, risks) or any(
                           recipe.id in rule.get("required_recipe_ids", ())
                           and _rule_matches(rule, local, risks) for rule in policy.coverage_rules
                       )
                   )]
        questions = tuple(recipe.objective for recipe, _, _ in recipes)
        invariants = tuple(dict.fromkeys((
            *component.get("invariants", ()),
            *(invariant for recipe, _, _ in recipes for invariant in recipe.invariants),
        )))
        risk = max(("normal", *(risk for _, risk, _ in recipes)), key=rank.__getitem__)
        obligations.append(CoverageObligation(
            obligation_id=_obligation_id("component", owner, "changed-behavior"),
            origin="component", subject=owner, owner_component_id=owner,
            required_evidence_categories=("tool-result", "implementation", "tests", "test-result", "review"),
            satisfaction_predicates=("recorded_evidence",), risk_tier=risk,
            unresolved_policy="block_when_unresolved" if any(v == "block_when_unresolved" for _, _, v in recipes) else "record_unknown",
            scope=paths,
            seed_hints=tuple(dict.fromkeys((*paths, *(p for recipe, _, _ in recipes for p in (*recipe.seed_paths, *recipe.related_paths))))),
            explanation=("Review changed behavior owned by " + owner
                         + "; unassessed paths and unresolved behavior stay incomplete. "
                         + " ".join(component.get("responsibilities", ()))),
            recipe_objective=" ".join(questions),
            recipe_invariants=invariants,
            evidence_requirements=requirements((recipe for recipe, _, _ in recipes), local),
            evidence_hints=tuple(dict.fromkeys(hint for recipe, _, _ in recipes for hint in recipe.expected_evidence)),
        ))

    for recipe, risk, unresolved in active_recipes:
        if recipe.execution != "independent":
            continue
        paths = tuple(path for path in changed if (
            not recipe.match.get("paths_any") or any(fnmatch.fnmatchcase(path, pattern) for pattern in recipe.match["paths_any"])
        ) and (
            not recipe.match.get("component_ids_any") or any(
                path in owner_paths.get(owner, ()) for owner in recipe.match["component_ids_any"]
            )
        ))
        obligations.append(CoverageObligation(
            obligation_id=_obligation_id("recipe", recipe.id, "independent-review"),
            origin="recipe", subject=recipe.id, recipe_id=recipe.id,
            recipe_execution="independent", recipe_objective=recipe.objective,
            recipe_invariants=recipe.invariants, requires_independent_verification=True,
            required_evidence_categories=("tool-result", "implementation", "tests", "test-result", "review"),
            satisfaction_predicates=("recorded_evidence",), risk_tier=risk,
            unresolved_policy=unresolved, scope=paths,
            seed_hints=tuple(dict.fromkeys((*paths, *recipe.seed_paths, *recipe.related_paths))),
            explanation=recipe.objective,
            evidence_requirements=requirements((recipe,), active_topology),
            evidence_hints=recipe.expected_evidence,
        ))

    for boundary in policy.boundaries:
        contract_changes = tuple(path for path in changed if any(
            fnmatch.fnmatchcase(path, pattern) for pattern in boundary.contract_paths
        ))
        endpoint_changes = tuple(path for path in changed if any(
            fnmatch.fnmatchcase(path, pattern)
            for patterns in boundary.endpoint_paths.values() for pattern in patterns
        ))
        if not contract_changes and not endpoint_changes:
            continue
        affected_owners = [owner for owner in boundary.participants if owner in owner_paths]
        fallback_owner = boundary.contract_change_owner if contract_changes else (
            affected_owners[0] if affected_owners else boundary.contract_change_owner
        )
        for participant in boundary.participants:
            owner = participant if participant in owner_paths else fallback_owner
            if owner in excluded_components:
                continue
            local_paths = tuple(path for path in owner_paths.get(participant, ()) if path not in contract_changes)
            obligations.append(CoverageObligation(
                obligation_id=_obligation_id("boundary-participant", boundary.id, participant),
                origin="boundary-participant", subject=f"{boundary.id}:{participant}",
                owner_component_id=owner, boundary_id=boundary.id, participant_id=participant,
                required_evidence_categories=("tool-result", "implementation", "review"),
                satisfaction_predicates=("recorded_evidence",), scope=local_paths,
                seed_hints=tuple(dict.fromkeys((
                    *contract_changes, *boundary.contract_paths,
                    *boundary.endpoint_paths.get(participant, ()),
                    *component_by_id[participant].get("paths", ()),
                ))),
                explanation=f"Check {participant}'s side of {boundary.id}: {boundary.objective}",
                recipe_objective=boundary.objective,
            ))
        obligations.append(CoverageObligation(
            obligation_id=_obligation_id("boundary", boundary.id, "compatibility"),
            origin="boundary", subject=boundary.id, boundary_id=boundary.id,
            evaluator_owned=True, required_evidence_categories=("implementation",),
            satisfaction_predicates=("recorded_evidence",),
            scope=tuple(dict.fromkeys((*contract_changes, *endpoint_changes))),
            seed_hints=boundary.contract_paths, explanation=boundary.objective,
        ))
    return tuple(sorted(obligations, key=lambda item: item.id))

class CoverageLedger:
    """Mutable evidence-to-obligation accounting with immutable obligations."""

    def __init__(self, obligations: Iterable[CoverageObligation]) -> None:
        items = tuple(obligations)
        self._recipe_states: dict[str, RecipeStatus] = {}
        self._obligations = {}
        for item in items:
            if isinstance(item, _RecipeAccountingObligation):
                if item.mandatory or item.required_evidence_categories or not item.recipe_id:
                    raise ValueError("recipe accounting obligations must be non-mandatory and evidence-free")
                self._recipe_states[item.recipe_id] = item.recipe_status
                continue
            self._obligations[item.obligation_id] = item
        self._evidence: dict[str, set[str]] = {item_id: set() for item_id in self._obligations}
        self._unresolved: set[str] = set()
        self._closures: dict[str, ObligationStatus] = {
            obligation_id: ObligationStatus.NOT_APPLICABLE
            for obligation_id, obligation in self._obligations.items()
            if obligation.origin == "requirement-accounting"
        }
        for obligation in self._obligations.values():
            if obligation.recipe_id:
                self._recipe_states.setdefault(obligation.recipe_id, RecipeStatus.ASSIGNED)

    def attach_evidence(self, obligation_id: str, evidence_id: str) -> None:
        if obligation_id not in self._obligations:
            raise KeyError(f"unknown coverage obligation: {obligation_id}")
        if not str(evidence_id).strip():
            raise ValueError("evidence_id must be non-empty")
        self._evidence[obligation_id].add(str(evidence_id))
        if _group_scoped(self._obligations[obligation_id]):
            return  # Evidence retention is not a substantive group assessment.
        self._unresolved.discard(obligation_id)
        self._closures.pop(obligation_id, None)

    def obligation(self, obligation_id: str) -> CoverageObligation:
        """Return immutable obligation metadata for deterministic association."""
        try:
            return self._obligations[obligation_id]
        except KeyError as exc:
            raise KeyError(f"unknown coverage obligation: {obligation_id}") from exc

    def obligations(self) -> tuple[CoverageObligation, ...]:
        """Return the immutable obligation set in stable identifier order."""
        return tuple(self._obligations[key] for key in sorted(self._obligations))

    def mark_unresolved(self, obligation_id: str) -> None:
        if obligation_id not in self._obligations:
            raise KeyError(f"unknown coverage obligation: {obligation_id}")
        if not self._evidence[obligation_id]:
            self._closures.pop(obligation_id, None)
            self._unresolved.add(obligation_id)

    def close_obligation(
        self, obligation_id: str, status: ObligationStatus,
    ) -> None:
        if obligation_id not in self._obligations:
            raise KeyError(f"unknown coverage obligation: {obligation_id}")
        if status not in {
            ObligationStatus.NOT_APPLICABLE,
            ObligationStatus.EXHAUSTED,
            ObligationStatus.BLOCKED,
        }:
            raise ValueError("unsupported obligation closure status")
        if not self._evidence[obligation_id]:
            self._unresolved.discard(obligation_id)
            self._closures[obligation_id] = status

    def replace_reconciled_state(
        self,
        evidence_by_obligation: Mapping[str, Iterable[str]],
        unresolved_obligation_ids: Iterable[str],
        closed_statuses: Mapping[str, ObligationStatus] | None = None,
    ) -> None:
        """Replace optimistic session accounting with controller-validated state."""
        unresolved_ids = tuple(unresolved_obligation_ids)
        unknown = sorted(
            set(evidence_by_obligation).union(unresolved_ids)
            - set(self._obligations)
        )
        if unknown:
            raise KeyError("unknown coverage obligation: " + ", ".join(unknown))
        reconciled: dict[str, set[str]] = {
            obligation_id: set() for obligation_id in self._obligations
        }
        for obligation_id, evidence_ids in evidence_by_obligation.items():
            for evidence_id in evidence_ids:
                if not str(evidence_id).strip():
                    raise ValueError("evidence_id must be non-empty")
                reconciled[obligation_id].add(str(evidence_id))
        self._evidence = reconciled
        self._unresolved = {
            obligation_id for obligation_id in unresolved_ids
            if not reconciled[obligation_id] or _group_scoped(self._obligations[obligation_id])
        }
        self._closures = dict(closed_statuses or {})

    def obligation_statuses(self) -> dict[str, ObligationStatus]:
        statuses: dict[str, ObligationStatus] = {}
        for obligation_id in sorted(self._obligations):
            if _group_scoped(self._obligations[obligation_id]):
                statuses[obligation_id] = self._closures.get(
                    obligation_id,
                    ObligationStatus.UNRESOLVED if obligation_id in self._unresolved else ObligationStatus.PENDING,
                )
            elif self._evidence[obligation_id]:
                statuses[obligation_id] = ObligationStatus.COVERED
            elif obligation_id in self._closures:
                statuses[obligation_id] = self._closures[obligation_id]
            elif obligation_id in self._unresolved:
                statuses[obligation_id] = ObligationStatus.UNRESOLVED
            else:
                statuses[obligation_id] = ObligationStatus.PENDING
        return statuses

    def recipe_statuses(self) -> dict[str, str]:
        statuses = dict(self._recipe_states)
        obligation_statuses = self.obligation_statuses()
        recipe_obligations: dict[str, list[str]] = {}
        for obligation in self._obligations.values():
            if obligation.recipe_id and obligation.mandatory:
                recipe_obligations.setdefault(obligation.recipe_id, []).append(obligation.obligation_id)
        for recipe_id, obligation_ids in recipe_obligations.items():
            values = [obligation_statuses[obligation_id] for obligation_id in obligation_ids]
            if all(value is ObligationStatus.COVERED for value in values):
                statuses[recipe_id] = RecipeStatus.COVERED
            elif all(value is ObligationStatus.NOT_APPLICABLE for value in values):
                statuses[recipe_id] = RecipeStatus.NOT_APPLICABLE
            elif any(value in {ObligationStatus.COVERED, ObligationStatus.PARTIALLY_COVERED} for value in values):
                statuses[recipe_id] = RecipeStatus.PARTIALLY_COVERED
            elif any(value in {
                ObligationStatus.UNRESOLVED,
                ObligationStatus.EXHAUSTED,
                ObligationStatus.BLOCKED,
            } for value in values):
                statuses[recipe_id] = RecipeStatus.UNRESOLVED
            else:
                statuses[recipe_id] = RecipeStatus.ASSIGNED
        return {recipe_id: status.value for recipe_id, status in sorted(statuses.items())}

    def snapshot(self) -> CoverageSnapshot:
        """Return a stable immutable view detached from later ledger updates."""
        return CoverageSnapshot(
            obligation_statuses=tuple(self.obligation_statuses().items()),
            recipe_statuses=tuple(self.recipe_statuses().items()),
            evidence_by_obligation=tuple(
                (obligation_id, tuple(sorted(self._evidence[obligation_id])))
                for obligation_id in sorted(self._evidence)
            ),
        )


def _group_scoped(obligation: CoverageObligation) -> bool:
    return bool(obligation.owner_component_id or obligation.boundary_id or obligation.evaluator_owned)


def _assignment_id(assignment: Assignment | SpecialistAssignment) -> str:
    value = (
        assignment.id if isinstance(assignment, Assignment)
        else assignment.assignment_id
    )
    return str(value).strip()


def _assignment_ownership(
    assignment: Assignment | SpecialistAssignment,
    obligation_by_id: Mapping[str, CoverageObligation],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    primary = set(assignment.primary_obligation_ids)
    if isinstance(assignment, SpecialistAssignment):
        return (
            tuple(sorted(primary)),
            (),
            tuple(sorted(assignment.independent_obligation_ids)),
        )
    assigned = set(assignment.obligation_ids)
    non_primary = assigned - primary
    independent = {
        obligation_id for obligation_id in assigned
        if obligation_id in obligation_by_id
        and obligation_by_id[obligation_id].requires_independent_verification
    }
    return (
        tuple(sorted(primary)),
        tuple(sorted(non_primary)),
        tuple(sorted(independent)),
    )


def session_ownership_for_assignment(
    assignment: Assignment | SpecialistAssignment,
    obligations: Iterable[CoverageObligation],
    *,
    session_id: str,
) -> SessionOwnership:
    """Build the canonical durable ownership projection for an assignment."""
    obligation_items = tuple(obligations)
    obligation_by_id = {item.id: item for item in obligation_items}
    if len(obligation_by_id) != len(obligation_items):
        raise ValueError("obligation ids must be unique")
    primary, secondary, independent = _assignment_ownership(
        assignment, obligation_by_id,
    )
    return SessionOwnership(
        session_id=session_id,
        assignment_id=_assignment_id(assignment),
        primary_obligation_ids=primary,
        secondary_obligation_ids=secondary,
        independent_obligation_ids=independent,
    )


def _validated_wave_start(
    snapshot: CoverageSnapshot,
    obligation_by_id: Mapping[str, CoverageObligation],
    evidence: EvidenceSnapshot,
) -> tuple[
    dict[str, ObligationStatus],
    dict[str, set[str]],
    set[str],
    dict[str, ObligationStatus],
]:
    if not isinstance(snapshot, CoverageSnapshot):
        raise TypeError("wave_start_coverage must be a CoverageSnapshot")
    records = {record.id: record for record in evidence.records}
    status_ids = [obligation_id for obligation_id, _ in snapshot.obligation_statuses]
    evidence_ids = [obligation_id for obligation_id, _ in snapshot.evidence_by_obligation]
    if len(set(status_ids)) != len(status_ids):
        raise ValueError("wave-start coverage has duplicate obligation statuses")
    if len(set(evidence_ids)) != len(evidence_ids):
        raise ValueError("wave-start coverage has duplicate evidence entries")
    expected_ids = set(obligation_by_id)
    if set(status_ids) != expected_ids or set(evidence_ids) != expected_ids:
        raise ValueError("wave-start coverage must contain every ledger obligation exactly once")

    statuses = dict(snapshot.obligation_statuses)
    seeded_evidence: dict[str, set[str]] = {}
    for obligation_id, raw_evidence_ids in snapshot.evidence_by_obligation:
        if len(set(raw_evidence_ids)) != len(raw_evidence_ids):
            raise ValueError(f"wave-start evidence for '{obligation_id}' contains duplicates")
        obligation = obligation_by_id[obligation_id]
        retained: set[str] = set()
        for evidence_id in raw_evidence_ids:
            record = records.get(evidence_id)
            if record is None:
                raise ValueError(f"wave-start coverage references unknown evidence: {evidence_id}")
            if not (
                _associated_collections_satisfying(
                    evidence, record, obligation,
                )
                or evidence_satisfies_obligation(record, obligation)
            ):
                raise ValueError(
                    f"wave-start evidence does not satisfy obligation '{obligation_id}'"
                )
            retained.add(evidence_id)
        seeded_evidence[obligation_id] = retained

    allowed_statuses = {
        ObligationStatus.PENDING,
        ObligationStatus.COVERED,
        ObligationStatus.PARTIALLY_COVERED,
        ObligationStatus.UNRESOLVED,
        ObligationStatus.NOT_APPLICABLE,
        ObligationStatus.EXHAUSTED,
        ObligationStatus.BLOCKED,
    }
    for obligation_id, status in statuses.items():
        if status not in allowed_statuses:
            raise ValueError(f"unsupported wave-start status for '{obligation_id}'")
        has_evidence = bool(seeded_evidence[obligation_id])
        needs_evidence = status in {ObligationStatus.COVERED, ObligationStatus.PARTIALLY_COVERED}
        if (needs_evidence and not has_evidence) or (
            not _group_scoped(obligation_by_id[obligation_id]) and needs_evidence != has_evidence
        ):
            raise ValueError(
                f"wave-start status/evidence mismatch for obligation '{obligation_id}'"
            )
    unresolved = {
        obligation_id for obligation_id, status in statuses.items()
        if status is ObligationStatus.UNRESOLVED
    }
    closures = {
        obligation_id: status for obligation_id, status in statuses.items()
        if status in {
            ObligationStatus.NOT_APPLICABLE,
            ObligationStatus.EXHAUSTED,
            ObligationStatus.BLOCKED,
            ObligationStatus.PARTIALLY_COVERED,
        }
        or (_group_scoped(obligation_by_id[obligation_id]) and status is ObligationStatus.COVERED)
    }
    return statuses, seeded_evidence, unresolved, closures


def reconcile_wave(
    ledger: CoverageLedger,
    *,
    wave_start_coverage: CoverageSnapshot,
    checkpoints: Iterable[SessionCheckpoint],
    evidence: EvidenceSnapshot,
    assignments: Iterable[Assignment | SpecialistAssignment],
    session_ownership: Iterable[SessionOwnership],
) -> CoverageReconciliation:
    """Reconcile a wave without trusting specialist-declared coverage states."""
    if not isinstance(ledger, CoverageLedger):
        raise TypeError("ledger must be a CoverageLedger")
    if not isinstance(evidence, EvidenceSnapshot):
        raise TypeError("evidence must be an EvidenceSnapshot")

    obligation_by_id = {item.id: item for item in ledger.obligations()}
    records = {record.id: record for record in evidence.records}
    (
        before, reconciled_evidence, reconciled_unresolved,
        reconciled_closures,
    ) = _validated_wave_start(
        wave_start_coverage, obligation_by_id, evidence
    )
    assignment_by_id: dict[str, Assignment | SpecialistAssignment] = {}
    for assignment in assignments:
        assignment_id = _assignment_id(assignment)
        if not assignment_id:
            raise ValueError("assignment must have a non-empty id")
        if assignment_id in assignment_by_id:
            raise ValueError(f"duplicate assignment id: {assignment_id}")
        assignment_by_id[assignment_id] = assignment

    owned_by_session: dict[str, SessionOwnership] = {}
    for ownership in session_ownership:
        if ownership.session_id in owned_by_session:
            raise ValueError(f"duplicate durable session id: {ownership.session_id}")
        assignment = assignment_by_id.get(ownership.assignment_id)
        if assignment is None:
            raise ValueError(
                f"session '{ownership.session_id}' references unknown assignment "
                f"'{ownership.assignment_id}'"
            )
        expected_primary, expected_secondary, expected_independent = _assignment_ownership(
            assignment, obligation_by_id
        )
        if tuple(sorted(ownership.primary_obligation_ids)) != expected_primary:
            raise ValueError("session primary ownership differs from its assignment")
        if tuple(sorted(ownership.secondary_obligation_ids)) != expected_secondary:
            raise ValueError("session secondary ownership differs from its assignment")
        if tuple(sorted(ownership.independent_obligation_ids)) != expected_independent:
            raise ValueError("session independent ownership differs from its assignment")
        unknown_ids = sorted(set(ownership.obligation_ids) - set(obligation_by_id))
        if unknown_ids:
            raise ValueError("session ownership contains unknown obligations: " + ", ".join(unknown_ids))
        owned_by_session[ownership.session_id] = ownership

    for checkpoint in sorted(tuple(checkpoints), key=lambda item: item.session_id):
        ownership = owned_by_session.get(checkpoint.session_id)
        if ownership is None:
            raise ValueError(
                f"checkpoint references unknown durable session: {checkpoint.session_id}"
            )
        owned_ids = ownership.obligation_ids
        assessments = tuple(
            item for item in checkpoint.obligation_assessments
            if isinstance(item, ObligationAssessment)
        )
        if not assessments:
            legacy_ids = tuple(sorted(set(
                checkpoint.evidence_ids + checkpoint.imported_evidence_ids
            )))
            assessments = tuple(
                ObligationAssessment(
                    target=f"legacy:{index}", obligation_id=obligation_id,
                    disposition=ObligationDisposition.COVERED,
                    reason="Legacy checkpoint evidence projection.",
                    evidence_ids=legacy_ids,
                )
                for index, obligation_id in enumerate(owned_ids, start=1)
                if not _group_scoped(obligation_by_id[obligation_id])
            )
        for assessment in assessments:
            obligation_id = assessment.obligation_id
            if obligation_id not in owned_ids:
                raise ValueError(
                    "checkpoint assessment references an unowned obligation"
                )
            obligation = obligation_by_id[obligation_id]
            if obligation.evaluator_owned:
                continue
            if _group_scoped(obligation):
                # Reuse admission validation; persisted/model assessments are not authority.
                validator = ObligationAssessmentLedger(
                    session_id=checkpoint.session_id, obligations=(obligation,), obligation_ids=(obligation_id,),
                )
                proposal = validator.propose(
                    target="O1", disposition=assessment.disposition.value,
                    reason=assessment.reason, evidence_ids=assessment.evidence_ids,
                    next_actions=assessment.next_actions, assessed_paths=assessment.assessed_paths,
                    omitted_paths=assessment.omitted_paths, evidence=evidence,
                    eligible=lambda record, item: bool(_associated_collections_satisfying(
                        evidence, record, item, session_id=checkpoint.session_id,
                    )) or evidence_satisfies_obligation(record, item),
                )
                if not proposal.accepted:
                    continue
                assessment = validator.assessment("O1")
                reconciled_closures[obligation_id] = ObligationStatus(assessment.disposition.value)
                reconciled_unresolved.discard(obligation_id)
            if assessment.disposition is ObligationDisposition.NOT_APPLICABLE:
                reconciled_closures[obligation_id] = ObligationStatus.NOT_APPLICABLE
                continue
            if assessment.disposition is ObligationDisposition.EXHAUSTED:
                reconciled_closures[obligation_id] = ObligationStatus.EXHAUSTED
                continue
            if assessment.disposition is ObligationDisposition.BLOCKED:
                reconciled_closures[obligation_id] = ObligationStatus.BLOCKED
                continue
            if assessment.disposition is ObligationDisposition.UNRESOLVED:
                reconciled_unresolved.add(obligation_id)
                continue
            if assessment.disposition not in {ObligationDisposition.COVERED, ObligationDisposition.PARTIALLY_COVERED}:
                continue
            obligation = obligation_by_id[obligation_id]
            referenced_ids = assessment.evidence_ids
            obligation = obligation_by_id[obligation_id]
            for evidence_id in referenced_ids:
                record = records.get(evidence_id)
                associated = (
                    ()
                    if record is None
                    else _associated_collections_satisfying(
                        evidence,
                        record,
                        obligation,
                        session_id=checkpoint.session_id,
                    )
                )
                independent_collection = (
                    obligation_id in ownership.independent_obligation_ids
                    and record is not None
                    and (
                        bool(associated)
                        or (
                            record.collector_session_id == checkpoint.session_id
                            and checkpoint.session_id in record.imported_by
                            and evidence_id not in checkpoint.imported_evidence_ids
                        )
                    )
                )
                if (
                    record is not None
                    and (
                        bool(associated)
                        or evidence_satisfies_obligation(record, obligation)
                    )
                    and (
                        not obligation.requires_independent_verification
                        or independent_collection
                    )
                ):
                    reconciled_evidence[obligation_id].add(evidence_id)

        declared_unresolved = set(checkpoint.unknowns)
        declared_unresolved.update(
            obligation_id
            for obligation_id, status in checkpoint.obligation_statuses
            if status is ObligationStatus.UNRESOLVED
        )
        for obligation_id in sorted(declared_unresolved.intersection(owned_ids)):
            reconciled_unresolved.add(obligation_id)

    ledger.replace_reconciled_state(
        reconciled_evidence, reconciled_unresolved, reconciled_closures,
    )
    snapshot = ledger.snapshot()
    after = dict(snapshot.obligation_statuses)
    newly_covered = tuple(sorted(
        obligation_id for obligation_id, status in after.items()
        if status is ObligationStatus.COVERED
        and before.get(obligation_id) is not ObligationStatus.COVERED
    ))
    uncovered = tuple(sorted(
        obligation_id for obligation_id, status in after.items()
        if obligation_by_id[obligation_id].mandatory
        and status in {ObligationStatus.PENDING, ObligationStatus.UNRESOLVED, ObligationStatus.PARTIALLY_COVERED}
    ))
    attempted = tuple(sorted(
        obligation_id for obligation_id in uncovered
        if after[obligation_id] in {ObligationStatus.UNRESOLVED, ObligationStatus.PARTIALLY_COVERED}
    ))
    never_covered = tuple(sorted(set(uncovered) - set(attempted)))
    return CoverageReconciliation(
        snapshot=snapshot,
        newly_covered_obligation_ids=newly_covered,
        uncovered_obligation_ids=uncovered,
        attempted_unresolved_obligation_ids=attempted,
        never_covered_obligation_ids=never_covered,
    )


def evaluate_coverage(
    obligations: Iterable[CoverageObligation] | CoverageLedger,
    evidence_by_obligation: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, ObligationStatus]:
    """Evaluate recorded evidence without allowing model output to set status."""
    ledger = obligations if isinstance(obligations, CoverageLedger) else CoverageLedger(obligations)
    for obligation_id, evidence_ids in (evidence_by_obligation or {}).items():
        for evidence_id in evidence_ids:
            ledger.attach_evidence(obligation_id, evidence_id)
    return ledger.obligation_statuses()


def recipe_statuses(
    obligations: Iterable[CoverageObligation] | CoverageLedger,
    evidence_by_obligation: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, str]:
    """Project explicit recipe lifecycle status from deterministic coverage data."""
    ledger = obligations if isinstance(obligations, CoverageLedger) else CoverageLedger(obligations)
    for obligation_id, evidence_ids in (evidence_by_obligation or {}).items():
        for evidence_id in evidence_ids:
            ledger.attach_evidence(obligation_id, evidence_id)
    return ledger.recipe_statuses()
