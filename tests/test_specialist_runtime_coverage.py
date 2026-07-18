import pytest

from pr_reviewer.specialist_runtime.types import CoverageObligation


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
    recipe_items = [item for item in obligations if item.recipe_id == "delivery"]

    assert {item.required_evidence for item in recipe_items} == {
        ("producer",), ("consumer",), ("tests",)
    }
    assert all(item.mandatory for item in recipe_items)
    assert [item.id for item in recipe_items] == sorted(item.id for item in recipe_items)


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
