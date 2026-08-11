from dataclasses import FrozenInstanceError
import json

import pytest

from pr_reviewer.specialist_runtime.types import CoverageObligation, ObligationStatus


def test_coverage_snapshot_is_sorted_immutable_and_detached():
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger, CoverageSnapshot

    obligations = (
        CoverageObligation("OB-b", "test", "b.py"),
        CoverageObligation("OB-a", "test", "a.py"),
    )
    ledger = CoverageLedger(obligations)
    ledger.attach_evidence("OB-b", "E-2")
    ledger.attach_evidence("OB-b", "E-1")

    snapshot = ledger.snapshot()
    ledger.attach_evidence("OB-a", "E-later")

    assert isinstance(snapshot, CoverageSnapshot)
    assert snapshot.obligation_statuses == (
        ("OB-a", ObligationStatus.PENDING),
        ("OB-b", ObligationStatus.COVERED),
    )
    assert snapshot.recipe_statuses == ()
    assert snapshot.evidence_by_obligation == (
        ("OB-a", ()),
        ("OB-b", ("E-1", "E-2")),
    )
    with pytest.raises(FrozenInstanceError):
        snapshot.evidence_by_obligation = ()


def test_coverage_obligation_exposes_legacy_friendly_evidence_aliases():
    obligation = CoverageObligation(
        obligation_id="analyzer:worker:implementation",
        origin="analyzer",
        subject="worker",
        required_evidence_categories=("implementation",),
    )

    assert obligation.id == "analyzer:worker:implementation"
    assert obligation.required_evidence == ("implementation",)
    assert obligation.mandatory is True


def test_changed_components_without_configured_relationship_create_no_interaction():
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger, derive_obligations
    from pr_reviewer.specialist_runtime.policy import ReviewPolicy

    obligations = derive_obligations({
        "changed_files": ["api/main.py", "worker/main.py"],
        "components": [
            {"id": "api", "changed_files": ["api/main.py"]},
            {"id": "worker", "changed_files": ["worker/main.py"]},
        ],
        "relationships": [],
    }, {}, ReviewPolicy.minimal())

    assert not any(
        item.subject == "api-to-worker" and "interaction" in item.required_evidence
        for item in obligations
    )


def test_matching_recipe_becomes_named_mandatory_obligations():
    from pr_reviewer.specialist_runtime.coverage import derive_obligations
    from pr_reviewer.specialist_runtime.policy import RecipePolicy, ReviewPolicy

    policy = ReviewPolicy.minimal(recipes=(RecipePolicy(
        id="delivery", title="Delivery", objective="Trace retry",
        execution="coverage", match={"file_roles_any": ("messaging",)},
        expected_evidence=("producer", "consumer", "tests"),
    ),))
    topology = {
        "changed_files": ["worker/messaging/consumer.py"],
        "file_roles": ["messaging", "implementation"],
        "components": [{"id": "worker", "changed_files": ["worker/messaging/consumer.py"]}],
    }

    obligations = derive_obligations(topology, {}, policy)
    recipe_items = [
        item for item in obligations
        if item.recipe_id == "delivery" and item.required_evidence_categories
    ]

    assert {item.required_evidence for item in recipe_items} == {
        ("producer",), ("consumer",), ("tests",)
    }
    assert all(item.mandatory for item in recipe_items)
    assert [item.id for item in recipe_items] == sorted(item.id for item in recipe_items)


def test_forced_recipe_activates_only_matching_evidence_requirements():
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger, derive_obligations
    from pr_reviewer.specialist_runtime.policy import (
        EvidenceRequirementPolicy, RecipePolicy, ReviewPolicy,
    )

    policy = ReviewPolicy(
        recipes=(RecipePolicy(
            id="delivery", title="Delivery", objective="Trace runtime delivery",
            evidence_requirements=(
                EvidenceRequirementPolicy(
                    id="workflow", category="workflow or deployment",
                    when={"paths_any": (".github/workflows/**",)},
                ),
                EvidenceRequirementPolicy(
                    id="manifest", category="build manifest",
                    when={"paths_any": ("pom.xml", "**/pom.xml")},
                ),
            ),
        ),),
        coverage_rules=({
            "id": "delivery-risk", "paths_any": (".github/workflows/**",),
            "required_recipe_ids": ("delivery",), "risk_tier": "high",
            "unresolved_policy": "block_when_unresolved",
        },),
    )
    topology = {
        "changed_files": [".github/workflows/review.yml"],
        "file_roles": ["configuration"], "components": [],
    }

    obligations = derive_obligations(topology, {}, policy)
    recipe_items = [
        item for item in obligations
        if item.recipe_id == "delivery" and item.required_evidence_categories
    ]

    assert [item.requirement_id for item in recipe_items] == ["workflow"]
    assert recipe_items[0].risk_tier == "high"
    assert recipe_items[0].unresolved_policy == "block_when_unresolved"
    accounting = next(
        item for item in obligations
        if item.requirement_id == "manifest" and not item.required_evidence_categories
    )
    ledger = CoverageLedger(obligations)
    assert accounting.mandatory is False
    assert ledger.obligation_statuses()[accounting.id] is ObligationStatus.NOT_APPLICABLE


def test_optional_and_one_of_requirements_have_bounded_mandatory_shape():
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger, derive_obligations
    from pr_reviewer.specialist_runtime.policy import (
        EvidenceRequirementPolicy, RecipePolicy, ReviewPolicy,
    )

    policy = ReviewPolicy.minimal(recipes=(RecipePolicy(
        id="delivery", title="Delivery", objective="Trace",
        match={"file_roles_any": ("configuration",)},
        evidence_requirements=(
            EvidenceRequirementPolicy("optional-doc", "documentation", {}, mode="optional"),
            EvidenceRequirementPolicy("artifact", "generated output", {}, mode="one_of:proof"),
            EvidenceRequirementPolicy("test", "behavioral test", {}, mode="one_of:proof"),
        ),
    ),))

    obligations = derive_obligations({
        "changed_files": ["config.yml"], "file_roles": ["configuration"],
        "components": [],
    }, {}, policy)
    recipe_items = [item for item in obligations if item.recipe_id == "delivery"]

    optional = next(item for item in recipe_items if item.requirement_id == "optional-doc")
    group = next(item for item in recipe_items if item.requirement_id == "one_of:proof")
    assert optional.mandatory is False
    assert group.mandatory is True
    assert group.required_evidence == ("behavioral test", "generated output")

    ledger = CoverageLedger(obligations)
    ledger.attach_evidence(group.id, "E-proof")
    assert ledger.recipe_statuses()["delivery"] == "covered"


def test_recipe_is_partial_until_every_obligation_has_evidence():
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger, derive_obligations
    from pr_reviewer.specialist_runtime.policy import RecipePolicy, ReviewPolicy

    policy = ReviewPolicy.minimal(recipes=(RecipePolicy(
        id="delivery", title="Delivery", objective="Trace retry",
        match={"file_roles_any": ("messaging",)},
        expected_evidence=("producer", "consumer", "tests"),
    ),))
    topology = {
        "changed_files": ["worker/messaging/consumer.py"],
        "file_roles": ["messaging", "implementation"],
        "components": [{
            "id": "worker", "file_roles": ["messaging"],
            "changed_files": ["worker/messaging/consumer.py"],
        }],
    }
    obligations = derive_obligations(topology, {}, policy)
    recipe_items = [item for item in obligations if item.recipe_id == "delivery"]

    ledger = CoverageLedger(obligations)
    ledger.attach_evidence(recipe_items[0].id, "E1")

    assert ledger.recipe_statuses()["delivery"] == "partially_covered"

    for item in recipe_items[1:]:
        ledger.attach_evidence(item.id, f"E-{item.id}")

    assert ledger.recipe_statuses()["delivery"] == "covered"


def test_topology_rules_include_artifacts_risks_and_component_interactions():
    from pr_reviewer.specialist_runtime.coverage import derive_obligations
    from pr_reviewer.specialist_runtime.policy import ReviewPolicy

    topology = {
        "changed_files": [
            "contracts/events.proto", "worker/messaging/consumer.py",
            "db/migrations/V1.sql", "deploy/chart.yaml",
        ],
        "file_roles": [
            "schema-contract", "implementation", "messaging", "migration", "deployment",
        ],
        "components": [
            {"id": "contracts", "file_roles": ["schema-contract"], "changed_files": ["contracts/events.proto"]},
            {"id": "worker", "file_roles": ["implementation", "messaging"], "changed_files": ["worker/messaging/consumer.py"]},
            {"id": "db", "file_roles": ["migration"], "changed_files": ["db/migrations/V1.sql"]},
        ],
        "relationships": [{"source": "contracts", "target": "worker"}],
        "available_role_paths": {"test": ["tests/test_consumer.py"]},
        "generated_artifacts": [{
            "id": "event-client", "source_of_truth": ["contracts/events.proto"],
            "output_paths": ["generated/events.py"],
        }],
    }

    obligations = derive_obligations(topology, {"risk_flags": ["auth_changes"]}, ReviewPolicy.minimal())
    categories = {item.required_evidence for item in obligations}

    assert {("implementation",), ("tests",), ("producer",), ("consumer",)}.issubset(categories)
    assert {("delivery",), ("persistence",), ("migration",), ("interaction",)}.issubset(categories)
    assert any(item.subject == "event-client" and item.required_evidence == ("deployment-artifact",)
               for item in obligations)
    assert any(item.origin == "risk-rule" and item.subject == "auth_changes" for item in obligations)


def test_static_one_sided_relationship_does_not_create_interaction_coverage():
    from pr_reviewer.specialist_runtime.coverage import derive_obligations
    from pr_reviewer.specialist_runtime.policy import ReviewPolicy

    obligations = derive_obligations({
        "changed_files": [".github/workflows/ai-review.yml"],
        "file_roles": ["deployment", "implementation"],
        "components": [{
            "id": "deployment",
            "changed_files": [".github/workflows/ai-review.yml"],
        }],
        "relationships": [{
            "source": "deployment", "target": "database",
            "active": False, "activation_reason": "orientation-only",
        }],
    }, {}, ReviewPolicy.minimal())

    assert not any(
        item.required_evidence == ("interaction",) for item in obligations
    )


def test_unrelated_repository_test_sample_does_not_create_test_coverage():
    from pr_reviewer.specialist_runtime.coverage import derive_obligations
    from pr_reviewer.specialist_runtime.policy import ReviewPolicy

    obligations = derive_obligations({
        "changed_files": ["src/payment.py"],
        "file_roles": ["implementation"],
        "available_role_paths": {},
        "role_availability": {"test": {"count": 500}},
    }, {}, ReviewPolicy.minimal())

    assert not any(item.required_evidence == ("tests",) for item in obligations)


def test_recipe_accounting_preserves_suppressed_and_not_applicable_statuses():
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger, derive_obligations
    from pr_reviewer.specialist_runtime.policy import RecipePolicy, ReviewPolicy

    policy = ReviewPolicy(
        recipes=(
            RecipePolicy(id="disabled", title="Disabled", objective="No-op", expected_evidence=("tests",)),
            RecipePolicy(id="unmatched", title="Unmatched", objective="No-op",
                         match={"file_roles_any": ("messaging",)}, expected_evidence=("tests",)),
        ),
        exclude={"paths": (), "components": (), "lenses": (), "recipes": ("disabled",)},
    )

    obligations = derive_obligations({"changed_files": ["src/main.py"], "file_roles": ["implementation"]}, {}, policy)

    assert CoverageLedger(obligations).recipe_statuses() == {
        "disabled": "suppressed_by_policy", "unmatched": "not_applicable",
    }


def test_stable_ids_do_not_merge_distinct_subjects_with_the_same_slug():
    from pr_reviewer.specialist_runtime.coverage import derive_obligations
    from pr_reviewer.specialist_runtime.policy import ReviewPolicy

    obligations = derive_obligations({
        "changed_files": ["src/foo-bar.py", "src/foo_bar.py"],
        "file_roles": ["implementation"],
    }, {}, ReviewPolicy.minimal())

    implementation_ids = [item.id for item in obligations if item.required_evidence == ("implementation",)]
    assert len(implementation_ids) == 2
    assert len(set(implementation_ids)) == 2


def test_independent_recipe_requires_independent_verification():
    from pr_reviewer.specialist_runtime.coverage import derive_obligations
    from pr_reviewer.specialist_runtime.policy import RecipePolicy, ReviewPolicy

    policy = ReviewPolicy.minimal(recipes=(RecipePolicy(
        id="security", title="Security", objective="Independently assess the boundary",
        execution="independent", match={"file_roles_any": ("implementation",)},
        expected_evidence=("boundary",),
    ),))

    obligations = derive_obligations(
        {"changed_files": ["src/main.py"], "file_roles": ["implementation"]}, {}, policy
    )

    recipe_obligation = next(item for item in obligations if item.recipe_id == "security")
    assert recipe_obligation.requires_independent_verification is True
    assert recipe_obligation.recipe_execution == "independent"


@pytest.mark.parametrize("execution", ["coverage", "dedicated", "independent"])
def test_matching_recipe_obligation_retains_execution_policy(execution):
    from pr_reviewer.specialist_runtime.coverage import derive_obligations
    from pr_reviewer.specialist_runtime.policy import RecipePolicy, ReviewPolicy

    policy = ReviewPolicy.minimal(recipes=(RecipePolicy(
        id="delivery", title="Delivery", objective="Trace delivery",
        execution=execution, match={"file_roles_any": ("implementation",)},
        expected_evidence=("consumer",),
    ),))

    obligations = derive_obligations(
        {"changed_files": ["src/main.py"], "file_roles": ["implementation"]}, {}, policy
    )

    recipe_obligation = next(item for item in obligations if item.recipe_id == "delivery")
    assert recipe_obligation.recipe_execution == execution


def test_recipe_lifecycle_statuses_survive_tuple_materialization():
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger, derive_obligations
    from pr_reviewer.specialist_runtime.policy import RecipePolicy, ReviewPolicy

    policy = ReviewPolicy(
        recipes=(
            RecipePolicy(id="disabled", title="Disabled", objective="No-op", expected_evidence=("tests",)),
            RecipePolicy(id="unmatched", title="Unmatched", objective="No-op",
                         match={"file_roles_any": ("messaging",)}, expected_evidence=("tests",)),
        ),
        exclude={"paths": (), "components": (), "lenses": (), "recipes": ("disabled",)},
    )

    materialized = tuple(derive_obligations(
        {"changed_files": ["src/main.py"], "file_roles": ["implementation"]}, {}, policy
    ))

    ledger = CoverageLedger(materialized)
    assert ledger.recipe_statuses() == {
        "disabled": "suppressed_by_policy", "unmatched": "not_applicable",
    }
    bookkeeping = [item for item in materialized if not item.mandatory and not item.required_evidence]
    assert all(not item.mandatory and not item.required_evidence for item in bookkeeping)
    with pytest.raises(KeyError, match="unknown coverage obligation"):
        ledger.attach_evidence(bookkeeping[0].id, "E-marker")


def test_public_coverage_obligation_has_no_lifecycle_field():
    obligation = CoverageObligation(
        obligation_id="topology:src-main:implementation",
        origin="topology",
        subject="src/main.py",
        required_evidence_categories=("implementation",),
    )

    assert obligation.mandatory is True
    assert obligation.id == obligation.obligation_id
    assert obligation.required_evidence == obligation.required_evidence_categories
    assert "recipe_status" not in CoverageObligation.__dataclass_fields__
    assert not hasattr(obligation, "recipe_status")


def test_documented_v2_rule_forces_recipe_and_blocks_unresolved_high_risk(tmp_path):
    from pr_reviewer.specialist_runtime.adjudication import (
        AdjudicatedReview,
        apply_runtime_verdict_policy,
    )
    from pr_reviewer.specialist_runtime.coverage import derive_obligations
    from pr_reviewer.specialist_runtime.evidence import EvidenceStore
    from pr_reviewer.specialist_runtime.policy import load_review_policy

    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({
        "version": 2,
        "components": [{"id": "api", "paths": ["services/api/**"]}],
        "recipes": [{
            "id": "api-coverage",
            "title": "API compatibility",
            "objective": "Trace authorization and compatibility.",
            "execution": "coverage",
            "match": {"component_ids_any": ["api"]},
            "expected_evidence": ["implementation", "tests"],
            "priority": "high",
        }],
        "coverage_rules": [{
            "id": "auth-risk",
            "risk_flags_any": ["auth_changes"],
            "required_recipe_ids": ["api-coverage"],
        }],
        "exclude": {
            "paths": ["vendor/**"], "components": [], "lenses": [], "recipes": [],
        },
        "verdict_policy": {
            "blocker_requires_request_changes": True,
            "require_evidence_for_findings": True,
        },
        "publishing": {
            "allowed_modes": ["review_comment"], "allow_approve": False,
        },
    }), encoding="utf-8")
    policy = load_review_policy(policy_path)
    topology = {
        "changed_files": ["services/api/auth.py", "vendor/copied.py"],
        "file_roles": ["implementation"],
        "components": [{
            "id": "api",
            "file_roles": ["implementation"],
            "changed_files": ["services/api/auth.py", "vendor/copied.py"],
        }],
    }

    obligations = derive_obligations(
        topology, {"risk_flags": ["auth_changes"]}, policy,
    )
    recipe_obligations = tuple(
        item for item in obligations if item.recipe_id == "api-coverage"
    )

    assert recipe_obligations
    assert all(item.risk_tier == "high" for item in recipe_obligations)
    assert all(
        item.unresolved_policy == "block_when_unresolved"
        for item in recipe_obligations
    )
    assert not any(
        item.subject == "vendor/copied.py" and item.origin == "topology"
        for item in obligations
    )

    result = apply_runtime_verdict_policy(
        model_verdict="approve",
        review=AdjudicatedReview(),
        unresolved=recipe_obligations,
        allow_approve=True,
        evidence=EvidenceStore(),
        obligations={item.id: item for item in obligations},
        changed_files=tuple(topology["changed_files"]),
        policy=policy.verdict_policy,
    )

    assert result.verdict == "notice"
    assert result.source == "incomplete-high-risk-coverage"
    assert set(result.blocking_obligation_ids) == {
        item.id for item in recipe_obligations
    }


def test_component_and_lens_exclusions_are_materialized_as_recipe_suppression():
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger, derive_obligations
    from pr_reviewer.specialist_runtime.policy import RecipePolicy, ReviewPolicy

    policy = ReviewPolicy(
        recipes=(
            RecipePolicy(
                id="worker-check", title="Worker", objective="Trace worker",
                match={"component_ids_any": ("worker",)},
                lenses=("retry",), expected_evidence=("implementation",),
            ),
        ),
        exclude={
            "paths": (), "components": ("worker",),
            "lenses": ("retry",), "recipes": (),
        },
    )
    obligations = derive_obligations({
        "changed_files": ["worker/main.py"],
        "file_roles": ["implementation"],
        "components": [{
            "id": "worker", "changed_files": ["worker/main.py"],
            "file_roles": ["implementation"],
        }],
    }, {}, policy)

    assert CoverageLedger(obligations).recipe_statuses() == {
        "worker-check": "suppressed_by_policy",
    }
