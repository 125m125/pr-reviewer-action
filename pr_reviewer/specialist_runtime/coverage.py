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
from .obligation_assessment import ObligationAssessment, ObligationDisposition
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


def _add_obligation(
    obligations: dict[str, CoverageObligation],
    *,
    origin: str,
    subject: str,
    evidence_category: str,
    risk_tier: str = "normal",
    recipe_id: str | None = None,
    recipe_execution: str | None = None,
    requirement_id: str | None = None,
    requirement_mode: str = "required",
    requires_independent_verification: bool = False,
    unresolved_policy: str = "record_unknown",
    required_evidence_categories: tuple[str, ...] | None = None,
    mandatory: bool = True,
    scope: Iterable[str] = (),
    seed_hints: Iterable[str] = (),
    explanation: str,
) -> None:
    obligation_id = _obligation_id(origin, subject, evidence_category)
    obligations.setdefault(obligation_id, CoverageObligation(
        obligation_id=obligation_id,
        origin=origin,
        subject=subject,
        required_evidence_categories=(
            (evidence_category,) if required_evidence_categories is None else required_evidence_categories
        ),
        satisfaction_predicates=("recorded_evidence",),
        risk_tier=risk_tier,
        requires_independent_verification=requires_independent_verification,
        unresolved_policy=unresolved_policy,
        scope=tuple(scope),
        seed_hints=tuple(seed_hints),
        explanation=explanation,
        recipe_id=recipe_id,
        recipe_execution=recipe_execution,
        requirement_id=requirement_id,
        requirement_mode=requirement_mode,
        mandatory=mandatory,
    ))


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
    """Return deterministic mandatory obligations and recipe lifecycle decisions."""
    classification = classification or {}
    excluded_paths = tuple(policy.exclude.get("paths", ()))

    def included(path: str) -> bool:
        return not any(
            fnmatch.fnmatchcase(path, pattern) for pattern in excluded_paths
        )

    changed_files = tuple(
        path for path in _paths(topology.get("changed_files")) if included(path)
    )
    changed_roles = {
        role for path in changed_files for role in classify_file_roles(path)
    }
    roles = set(_strings(topology.get("file_roles"))) | changed_roles
    risk_flags = set(_strings(topology.get("risk_flags"))) | set(_strings(classification.get("risk_flags")))
    excluded_components = {
        _slug(item) for item in policy.exclude.get("components", ())
    }
    components = tuple(
        sorted(
            (
                {
                    **component,
                    "changed_files": tuple(
                        path for path in _paths(component.get("changed_files"))
                        if included(path)
                    ),
                }
                for component in topology.get("components", [])
                if isinstance(component, Mapping)
                and _slug(component.get("id")) not in excluded_components
            ),
            key=lambda component: _slug(component.get("id")),
        )
    )
    effective_topology = {
        **topology,
        "changed_files": changed_files,
        "components": components,
        "file_roles": sorted(roles),
    }
    obligations: dict[str, CoverageObligation] = {}

    for path in changed_files:
        if "implementation" in classify_file_roles(path):
            _add_obligation(
                obligations, origin="topology", subject=path, evidence_category="implementation",
                scope=(path,), seed_hints=(path,),
                explanation="Inspect the changed implementation behavior.",
            )

    available_tests = _paths((topology.get("available_role_paths") or {}).get("test"))
    changed_tests = tuple(path for path in changed_files if "test" in classify_file_roles(path))
    if available_tests or changed_tests:
        _add_obligation(
            obligations, origin="topology", subject="relevant-tests", evidence_category="tests",
            scope=changed_files, seed_hints=changed_tests or available_tests,
            explanation="Inspect relevant tests when they are available to the review.",
        )

    for path in changed_files:
        path_roles = set(classify_file_roles(path))
        if "schema-contract" in path_roles:
            for category in ("producer", "consumer"):
                _add_obligation(
                    obligations, origin="topology", subject=path, evidence_category=category,
                    scope=(path,), seed_hints=(path,),
                    explanation="Trace the changed schema or contract through its producers and consumers.",
                )

    for component in components:
        component_id = _slug(component.get("id"), "repository")
        component_roles = set(_strings(component.get("file_roles")))
        component_paths = _paths(component.get("changed_files"))
        if "messaging" in component_roles or (not component_roles and "messaging" in roles):
            for category in ("producer", "consumer", "delivery"):
                _add_obligation(
                    obligations, origin="topology", subject=component_id, evidence_category=category,
                    scope=component_paths, seed_hints=component_paths,
                    explanation="Verify message production, consumption, and delivery behavior.",
                )
        if component_roles.intersection({"persistence", "migration"}) or (
            not component_roles and roles.intersection({"persistence", "migration"})
        ):
            for category in ("persistence", "migration"):
                _add_obligation(
                    obligations, origin="topology", subject=component_id, evidence_category=category,
                    scope=component_paths, seed_hints=component_paths,
                    explanation="Verify persistence and migration consistency for the changed component.",
                )
        if component_roles.intersection({"deployment", "build-manifest", "generated"}) or (
            not component_roles and roles.intersection({"deployment", "build-manifest", "generated"})
        ):
            _add_obligation(
                obligations, origin="topology", subject=component_id, evidence_category="deployment-artifact",
                scope=component_paths, seed_hints=component_paths,
                explanation="Confirm deployment or generated artifacts consume the changed revision.",
            )

    for artifact in topology.get("generated_artifacts", []):
        if not isinstance(artifact, Mapping):
            continue
        source_paths = _paths(artifact.get("source_of_truth"))
        if not set(source_paths).intersection(changed_files):
            continue
        artifact_id = _slug(artifact.get("id"), "generated-artifact")
        _add_obligation(
            obligations, origin="topology", subject=artifact_id, evidence_category="deployment-artifact",
            scope=source_paths, seed_hints=_paths(artifact.get("output_paths")),
            explanation="Confirm the changed source propagates to its generated deployment artifact.",
        )

    for flag in sorted(risk_flags):
        _add_obligation(
            obligations, origin="risk-rule", subject=flag, evidence_category="risk-assessment",
            risk_tier="high" if flag.startswith("linked_priority") else "critical",
            unresolved_policy="block_when_unresolved",
            scope=changed_files, seed_hints=changed_files,
            explanation=f"Verify the deterministic risk flag: {flag}.",
        )

    relationships = tuple(
        relationship for relationship in topology.get("relationships", []) if isinstance(relationship, Mapping)
    )
    for relationship in sorted(relationships, key=lambda item: (_slug(item.get("source")), _slug(item.get("target")))):
        source, target = _slug(relationship.get("source")), _slug(relationship.get("target"))
        active = (
            bool(relationship.get("active"))
            if "active" in relationship
            else source in {_slug(item.get("id")) for item in components}
            and target in {_slug(item.get("id")) for item in components}
        )
        if source and target and active:
            _add_obligation(
                obligations, origin="topology", subject=f"{source}-to-{target}",
                evidence_category="interaction", scope=changed_files,
                explanation="Trace the changed interaction across component boundaries.",
            )

    recipe_states: dict[str, RecipeStatus] = {}
    excluded_recipes = {
        _slug(recipe_id) for recipe_id in policy.exclude.get("recipes", ())
    }
    excluded_lenses = {
        _slug(lens) for lens in policy.exclude.get("lenses", ())
    }
    forced_recipes: dict[str, tuple[str, str]] = {}
    risk_rank = {"low": 0, "normal": 1, "high": 2, "critical": 3}
    for rule in policy.coverage_rules:
        if not _rule_matches(rule, effective_topology, risk_flags):
            continue
        for recipe_id in rule.get("required_recipe_ids", ()):
            current = forced_recipes.get(str(recipe_id))
            candidate = (
                str(rule.get("risk_tier", "high")),
                str(rule.get("unresolved_policy", "block_when_unresolved")),
            )
            if current is None or risk_rank[candidate[0]] > risk_rank[current[0]]:
                forced_recipes[str(recipe_id)] = candidate
    for recipe in sorted(policy.recipes, key=lambda item: item.id):
        if (
            _slug(recipe.id) in excluded_recipes
            or excluded_lenses.intersection(_slug(item) for item in recipe.lenses)
            or set(recipe.match.get("component_ids_any", ())).intersection(
                excluded_components
            )
        ):
            recipe_states[recipe.id] = RecipeStatus.SUPPRESSED_BY_POLICY
            continue
        if (
            recipe.id not in forced_recipes
            and not _recipe_matches(recipe, effective_topology, risk_flags)
        ):
            recipe_states[recipe.id] = RecipeStatus.NOT_APPLICABLE
            continue
        recipe_states[recipe.id] = RecipeStatus.ASSIGNED
        forced = forced_recipes.get(recipe.id)
        risk_tier = forced[0] if forced else recipe.priority
        unresolved_policy = (
            forced[1]
            if forced
            else (
                "block_when_unresolved"
                if recipe.priority in {"critical", "high"}
                else "record_unknown"
            )
        )
        requirements = tuple(
            item for item in recipe.evidence_requirements
            if not item.when or _rule_matches(item.when, effective_topology, risk_flags)
        )
        matched_requirement_ids = {item.id for item in requirements}
        for requirement in recipe.evidence_requirements:
            if requirement.id in matched_requirement_ids:
                continue
            obligation_id = _obligation_id(
                "requirement-accounting",
                f"{recipe.id}:{requirement.id}",
                "not-applicable",
            )
            obligations.setdefault(obligation_id, CoverageObligation(
                obligation_id=obligation_id,
                origin="requirement-accounting",
                subject=f"{recipe.id}:{requirement.id}",
                satisfaction_predicates=("requirement_status:not_applicable",),
                risk_tier=risk_tier,
                unresolved_policy=unresolved_policy,
                explanation=(
                    f"Repository recipe '{recipe.id}' requirement "
                    f"'{requirement.id}' did not match the immutable change."
                ),
                recipe_id=recipe.id,
                recipe_execution=recipe.execution,
                requirement_id=requirement.id,
                requirement_mode=requirement.mode,
                mandatory=False,
            ))
        entries: list[tuple[str, tuple[str, ...], str, tuple[str, ...], bool]] = []
        for category in recipe.expected_evidence:
            entries.append((category, (category,), "required", (), True))
        for requirement in requirements:
            if requirement.mode.startswith("one_of:"):
                continue
            entries.append((
                requirement.id,
                (requirement.category,),
                requirement.mode,
                tuple(dict.fromkeys((*requirement.seed_paths, *requirement.related_paths))),
                requirement.mode != "optional",
            ))
        groups = sorted({
            item.mode for item in requirements if item.mode.startswith("one_of:")
        })
        for group in groups:
            members = tuple(item for item in requirements if item.mode == group)
            entries.append((
                group,
                tuple(sorted({item.category for item in members})),
                group,
                tuple(dict.fromkeys(
                    path for item in members
                    for path in (*item.seed_paths, *item.related_paths)
                )),
                True,
            ))
        if not recipe.expected_evidence and not recipe.evidence_requirements:
            entries.append(("review", ("review",), "required", (), True))
        for requirement_id, categories, mode, requirement_paths, mandatory in entries:
            _add_obligation(
                obligations, origin="recipe",
                subject=(
                    recipe.id if requirement_id in recipe.expected_evidence
                    else f"{recipe.id}:{requirement_id}"
                ),
                evidence_category=categories[0],
                required_evidence_categories=categories,
                risk_tier=risk_tier, recipe_id=recipe.id,
                recipe_execution=recipe.execution,
                requirement_id=requirement_id,
                requirement_mode=mode,
                requires_independent_verification=recipe.execution == "independent",
                unresolved_policy=unresolved_policy,
                scope=changed_files,
                seed_hints=(
                    tuple(dict.fromkeys((*recipe.seed_paths, *requirement_paths)))
                    or changed_files
                ),
                mandatory=mandatory,
                explanation=(
                    f"Repository recipe '{recipe.id}' requires "
                    f"{' or '.join(categories)} evidence."
                ),
            )

    for recipe_id, status in recipe_states.items():
        if status not in {RecipeStatus.NOT_APPLICABLE, RecipeStatus.SUPPRESSED_BY_POLICY}:
            continue
        marker = _recipe_accounting_obligation(recipe_id, status)
        obligations.setdefault(marker.obligation_id, marker)

    return tuple(obligations[obligation_id] for obligation_id in sorted(obligations))


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
            if not reconciled[obligation_id]
        }
        self._closures = dict(closed_statuses or {})

    def obligation_statuses(self) -> dict[str, ObligationStatus]:
        statuses: dict[str, ObligationStatus] = {}
        for obligation_id in sorted(self._obligations):
            if self._evidence[obligation_id]:
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
            elif any(value is ObligationStatus.COVERED for value in values):
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
        ObligationStatus.UNRESOLVED,
        ObligationStatus.NOT_APPLICABLE,
        ObligationStatus.EXHAUSTED,
        ObligationStatus.BLOCKED,
    }
    for obligation_id, status in statuses.items():
        if status not in allowed_statuses:
            raise ValueError(f"unsupported wave-start status for '{obligation_id}'")
        has_evidence = bool(seeded_evidence[obligation_id])
        if (status is ObligationStatus.COVERED) != has_evidence:
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
        }
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
            )
        for assessment in assessments:
            obligation_id = assessment.obligation_id
            if obligation_id not in owned_ids:
                raise ValueError(
                    "checkpoint assessment references an unowned obligation"
                )
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
            if assessment.disposition is not ObligationDisposition.COVERED:
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
        and status in {ObligationStatus.PENDING, ObligationStatus.UNRESOLVED}
    ))
    attempted = tuple(sorted(
        obligation_id for obligation_id in uncovered
        if after[obligation_id] is ObligationStatus.UNRESOLVED
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
