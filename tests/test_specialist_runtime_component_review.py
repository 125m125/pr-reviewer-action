"""Small, parser-independent fixture contract for component-owned reviews."""

from __future__ import annotations

import json
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "specialist_runtime" / "component-owned" / "fixture.json"


def load_component_fixture() -> dict:
    """Load the public synthetic fixture without invoking a model or parser."""
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert set(fixture) == {"policy", "changed_files", "tracked_files"}
    return fixture


def test_fixture_has_v3_policy_and_representative_changed_paths():
    fixture = load_component_fixture()
    policy = fixture["policy"]

    assert policy["version"] == 3
    assert {component["id"] for component in policy["components"]} == {
        "java-orders", "java-billing", "python-worker",
    }
    assert sum(component["id"].startswith("java-") for component in policy["components"]) == 2
    assert {"contracts/events/order-created.json", "deploy/fulfillment/worker.yaml", "docs/release-notes.md"} <= set(fixture["changed_files"])
    assert len(fixture["tracked_files"]) >= len(fixture["changed_files"])


def test_fixture_keeps_shared_contract_and_independent_recipe_explicit():
    fixture = load_component_fixture()
    policy = fixture["policy"]

    boundary = next(item for item in policy["boundaries"] if item["id"] == "order-events")
    assert set(boundary["participants"]) == {"java-orders", "python-worker"}
    assert boundary["contract_change_owner"] == "java-orders"
    assert set(boundary["endpoint_paths"]) == {"java-orders", "python-worker"}

    recipe = next(item for item in policy["recipes"] if item["id"] == "deployment-rollout-review")
    assert recipe["execution"] == "independent"
    assert recipe["match"]["paths_any"] == ["deploy/fulfillment/**"]


def test_fixture_does_not_turn_unmatched_paths_into_an_obligation_count():
    """Unmatched files are input facts; obligation cardinality belongs to derivation."""
    fixture = load_component_fixture()
    owned = [path for component in fixture["policy"]["components"] for pattern in component["paths"] for path in fixture["changed_files"] if path.startswith(pattern.removesuffix("**"))]

    assert "docs/release-notes.md" not in owned
    assert "docs/release-notes.md" in fixture["changed_files"]


def test_existing_replay_fixture_remains_candidate_and_publication_baseline():
    from pr_reviewer.specialist_runtime.replay import replay_fixture

    result = replay_fixture(Path(__file__).parent / "fixtures" / "specialist_runtime" / "multilingual-pr")

    assert result.unsupported_published_claims == ()
    assert result.artifact["accepted_candidates"] == []
    assert result.artifact["handoff"]["markdown"]


def _derive(fixture=None):
    from pr_reviewer.specialists import build_topology
    from pr_reviewer.specialist_runtime.coverage import derive_obligations
    from pr_reviewer.specialist_runtime.policy import parse_review_policy
    fixture = fixture or load_component_fixture()
    policy = parse_review_policy(fixture["policy"])
    topology = build_topology(
        [{"filename": path} for path in fixture["changed_files"]], {},
        fixture["tracked_files"], policy.legacy_projection(),
    )
    return topology, derive_obligations(topology, {}, policy)


def test_component_requirements_do_not_grow_with_changed_file_count():
    fixture = load_component_fixture()
    topology, obligations = _derive(fixture)
    owners = {o.owner_component_id for o in obligations if o.origin == "component"}
    assert owners == {"java-orders", "java-billing", "python-worker", "repository-remainder"}
    fixture["changed_files"] += [f"services/orders/src/main/java/Extra{i}.java" for i in range(40)]
    _, expanded = _derive(fixture)
    assert sum(o.origin == "component" for o in expanded) == len(owners)
    assert topology["path_components"]["docs/release-notes.md"] == "repository-remainder"
    assert not any(o.origin in {"topology", "risk-rule"} for o in obligations)


def test_contract_only_change_keeps_one_owner_and_all_participant_checks():
    fixture = load_component_fixture()
    fixture["changed_files"] = ["contracts/events/order-created.json"]
    topology, obligations = _derive(fixture)
    assert {o.owner_component_id for o in obligations if o.origin == "component"} == {"java-orders"}
    locals_ = [o for o in obligations if o.boundary_id == "order-events" and not o.evaluator_owned]
    assert {o.participant_id for o in locals_} == {"java-orders", "python-worker"}
    assert {o.owner_component_id for o in locals_} == {"java-orders"}
    assert sum(o.evaluator_owned for o in obligations) == 1


def test_owner_assignments_do_not_assign_evaluator_or_silently_drop_capacity():
    from pr_reviewer.specialist_runtime.assignments import component_assignment_plan
    from pr_reviewer.specialist_runtime.policy import RuntimeConfig
    topology, obligations = _derive()
    plan = component_assignment_plan(obligations, topology, RuntimeConfig(max_sessions=2))
    assigned = {i for a in plan.assignments for i in a.obligation_ids}
    assert len(plan.assignments) == 2
    assert all(not o.evaluator_owned for o in obligations if o.id in assigned)
    assert {o.id for o in obligations if o.mandatory and not o.evaluator_owned} == assigned | set(plan.unassigned_obligation_ids)
    assert len({a.owner_component_id for a in plan.assignments if not a.overlap_justification}) == sum(not a.overlap_justification for a in plan.assignments)


def test_overlapping_owners_require_explicit_precedence():
    import pytest
    fixture = load_component_fixture()
    fixture["policy"]["components"].append({"id": "all-java", "paths": ["services/**"]})
    with pytest.raises(ValueError, match="ownership_precedence"):
        _derive(fixture)
    fixture["policy"]["ownership_precedence"] = ["java-orders", "java-billing", "all-java"]
    topology, _ = _derive(fixture)
    assert topology["path_components"][fixture["changed_files"][0]] == "java-orders"


def test_related_components_and_risk_flags_are_hints_not_boundary_jobs():
    fixture = load_component_fixture()
    fixture["changed_files"] = fixture["changed_files"][:2]
    fixture["policy"]["components"][0]["related_components"] = ["java-billing"]
    _, obligations = _derive(fixture)
    assert not any(o.boundary_id for o in obligations)
    assert sum(o.mandatory for o in obligations) == 2


def test_explicit_evidence_requirements_stay_grouped_with_owner():
    fixture = load_component_fixture()
    recipe = fixture["policy"]["recipes"][0]
    recipe["match"] = {"component_ids_any": ["java-orders"]}
    recipe["evidence_requirements"] = [
        {"id": "producer", "category": "implementation", "mode": "required"},
        {"id": "test", "category": "tests", "mode": "one_of:verification"},
        {"id": "trace", "category": "consumer", "mode": "one_of:verification"},
    ]
    _, obligations = _derive(fixture)
    owner = next(o for o in obligations if o.origin == "component" and o.owner_component_id == "java-orders")
    assert len(owner.evidence_requirements) == 3
    assert owner.recipe_objective == recipe["objective"]
    assert owner.evidence_hints == tuple(recipe["expected_evidence"])
    assert sum(o.origin == "component" and o.owner_component_id == "java-orders" for o in obligations) == 1


def test_independent_recipe_has_separate_intentionally_overlapping_assignment():
    from pr_reviewer.specialist_runtime.assignments import component_assignment_plan
    from pr_reviewer.specialist_runtime.policy import RuntimeConfig
    topology, obligations = _derive()
    plan = component_assignment_plan(obligations, topology, RuntimeConfig())
    independent = [a for a in plan.assignments if a.overlap_justification]
    assert len(independent) == 1
    assert independent[0].recipe_ids == ("deployment-rollout-review",)
    assert "deploy/fulfillment/worker.yaml" in independent[0].owned_changed_paths
    worker = next(a for a in plan.assignments if a.owner_component_id == "python-worker")
    assert "deploy/fulfillment/worker.yaml" in worker.owned_changed_paths


def test_independent_component_recipe_does_not_claim_other_owner_files():
    fixture = load_component_fixture()
    fixture["policy"]["recipes"][1]["match"] = {"component_ids_any": ["java-billing"]}
    _, obligations = _derive(fixture)
    independent = next(o for o in obligations if o.requires_independent_verification)
    assert independent.scope == ("services/billing/src/main/java/example/billing/InvoiceService.java",)


def test_boundary_local_scope_excludes_unrelated_changed_owner_behavior():
    fixture = load_component_fixture()
    endpoint = "services/orders/src/main/java/messaging/OrderEvents.java"
    unrelated = "services/orders/src/main/java/OrderSearch.java"
    contract = "contracts/events/order-created.json"
    fixture["changed_files"] = [endpoint, unrelated, contract]
    _, obligations = _derive(fixture)
    owner = next(o for o in obligations if o.origin == "component")
    local = next(o for o in obligations if o.boundary_id == "order-events" and o.participant_id == "java-orders")
    assert set(local.scope) == {endpoint, contract}
    assert unrelated in owner.scope
    assert "services/orders/**" in local.seed_hints


def test_shared_boundary_references_do_not_duplicate_changed_path_ownership():
    from pr_reviewer.specialist_runtime.assignments import component_assignment_plan
    from pr_reviewer.specialist_runtime.policy import RuntimeConfig
    topology, obligations = _derive()
    plan = component_assignment_plan(obligations, topology, RuntimeConfig())
    contract = "contracts/events/order-created.json"
    ordinary = [assignment for assignment in plan.assignments if not assignment.overlap_justification]
    assert [a.owner_component_id for a in ordinary if contract in a.owned_changed_paths] == ["java-orders"]
    worker = next(a for a in ordinary if a.owner_component_id == "python-worker")
    assert contract in worker.boundary_paths
    assert contract in worker.seed_paths
