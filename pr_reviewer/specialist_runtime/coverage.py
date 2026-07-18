"""Deterministic mandatory-coverage derivation and evidence accounting."""

from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass
from collections.abc import Iterable, Mapping
from typing import Any

from pr_reviewer.specialists import classify_file_roles

from .policy import RecipePolicy, ReviewPolicy
from .types import CoverageObligation, ObligationStatus, RecipeStatus


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
    requires_independent_verification: bool = False,
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
        scope=tuple(scope),
        seed_hints=tuple(seed_hints),
        explanation=explanation,
        recipe_id=recipe_id,
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


def derive_obligations(
    topology: Mapping[str, Any],
    classification: Mapping[str, Any] | None,
    policy: ReviewPolicy,
) -> tuple[CoverageObligation, ...]:
    """Return deterministic mandatory obligations and recipe lifecycle decisions."""
    classification = classification or {}
    changed_files = _paths(topology.get("changed_files"))
    changed_roles = {
        role for path in changed_files for role in classify_file_roles(path)
    }
    roles = set(_strings(topology.get("file_roles"))) | changed_roles
    risk_flags = set(_strings(topology.get("risk_flags"))) | set(_strings(classification.get("risk_flags")))
    components = tuple(
        sorted(
            (component for component in topology.get("components", []) if isinstance(component, Mapping)),
            key=lambda component: _slug(component.get("id")),
        )
    )
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
            scope=changed_files, seed_hints=changed_files,
            explanation=f"Verify the deterministic risk flag: {flag}.",
        )

    relationships = tuple(
        relationship for relationship in topology.get("relationships", []) if isinstance(relationship, Mapping)
    )
    if len(components) > 1 and not relationships:
        relationships = tuple({"source": left.get("id"), "target": right.get("id")}
                              for left, right in zip(components, components[1:]))
    for relationship in sorted(relationships, key=lambda item: (_slug(item.get("source")), _slug(item.get("target")))):
        source, target = _slug(relationship.get("source")), _slug(relationship.get("target"))
        if source and target:
            _add_obligation(
                obligations, origin="topology", subject=f"{source}-to-{target}",
                evidence_category="interaction", scope=changed_files,
                explanation="Trace the changed interaction across component boundaries.",
            )

    recipe_states: dict[str, RecipeStatus] = {}
    excluded_recipes = {_slug(recipe_id) for recipe_id in policy.exclude.get("recipes", ())}
    for recipe in sorted(policy.recipes, key=lambda item: item.id):
        if _slug(recipe.id) in excluded_recipes:
            recipe_states[recipe.id] = RecipeStatus.SUPPRESSED_BY_POLICY
            continue
        recipe_topology = {**topology, "file_roles": sorted(roles)}
        if not _recipe_matches(recipe, recipe_topology, risk_flags):
            recipe_states[recipe.id] = RecipeStatus.NOT_APPLICABLE
            continue
        recipe_states[recipe.id] = RecipeStatus.ASSIGNED
        categories = recipe.expected_evidence or ("review",)
        for category in categories:
            _add_obligation(
                obligations, origin="recipe", subject=recipe.id, evidence_category=category,
                risk_tier=recipe.priority, recipe_id=recipe.id,
                requires_independent_verification=recipe.execution == "independent",
                scope=changed_files, seed_hints=recipe.seed_paths or changed_files,
                explanation=f"Repository recipe '{recipe.id}' requires {category} evidence.",
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

    def mark_unresolved(self, obligation_id: str) -> None:
        if obligation_id not in self._obligations:
            raise KeyError(f"unknown coverage obligation: {obligation_id}")
        if not self._evidence[obligation_id]:
            self._unresolved.add(obligation_id)

    def obligation_statuses(self) -> dict[str, ObligationStatus]:
        statuses: dict[str, ObligationStatus] = {}
        for obligation_id in sorted(self._obligations):
            if self._evidence[obligation_id]:
                statuses[obligation_id] = ObligationStatus.COVERED
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
            if obligation.recipe_id:
                recipe_obligations.setdefault(obligation.recipe_id, []).append(obligation.obligation_id)
        for recipe_id, obligation_ids in recipe_obligations.items():
            values = [obligation_statuses[obligation_id] for obligation_id in obligation_ids]
            if all(value is ObligationStatus.COVERED for value in values):
                statuses[recipe_id] = RecipeStatus.COVERED
            elif any(value is ObligationStatus.COVERED for value in values):
                statuses[recipe_id] = RecipeStatus.PARTIALLY_COVERED
            elif any(value is ObligationStatus.UNRESOLVED for value in values):
                statuses[recipe_id] = RecipeStatus.UNRESOLVED
            else:
                statuses[recipe_id] = RecipeStatus.ASSIGNED
        return {recipe_id: status.value for recipe_id, status in sorted(statuses.items())}


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
