import pytest

from pr_reviewer.specialists import build_topology, classify_file_roles


def files(*paths):
    return [{"filename": path} for path in paths]


def test_topology_discovers_monorepo_components_and_generic_roles():
    tracked = [
        "pom.xml",
        "service-a/pom.xml",
        "worker/pyproject.toml",
        "service-a/src/Main.java",
        "worker/jobs/process.py",
        "contracts/events.proto",
    ]
    topology = build_topology(
        files(
            "service-a/src/Main.java",
            "worker/jobs/process.py",
            "contracts/events.proto",
        ),
        {"pr_kind": "app_code", "risk_flags": []},
        tracked,
    )
    ids = {item["id"] for item in topology["components"]}
    assert {"service-a", "worker", "contracts"}.issubset(ids)
    assert "messaging" in topology["file_roles"]
    assert "schema-contract" in topology["file_roles"]
    assert topology["relationships"]


def test_single_component_repository_uses_repository_fallback():
    topology = build_topology(
        files("src/main.py", "tests/test_main.py"),
        {},
        ["pyproject.toml", "src/main.py"],
    )
    assert [item["id"] for item in topology["components"]] == ["repository"]
    assert {"implementation", "test"}.issubset(set(topology["file_roles"]))


@pytest.mark.parametrize(
    ("path", "role"),
    [
        ("worker/messaging/consumer.py", "messaging"),
        ("api/openapi.yaml", "schema-contract"),
        ("infra/helm/deployment.yaml", "deployment"),
        ("db/migrations/V1.sql", "migration"),
        ("generated/client.ts", "generated"),
        ("pnpm-lock.yaml", "build-manifest"),
    ],
)
def test_file_role_detection(path, role):
    assert role in classify_file_roles(path)


def test_pnpm_lock_is_not_implementation():
    assert "implementation" not in classify_file_roles("pnpm-lock.yaml")


def test_configured_topology_preserves_component_metadata():
    config = {
        "components": [{
            "id": "worker",
            "paths": ["worker/**"],
            "responsibilities": ["background jobs"],
            "related_components": ["contracts"],
            "contracts": ["events"],
            "invariants": ["delivery is idempotent"],
        }],
    }
    topology = build_topology(
        files("worker/messaging/consumer.py"),
        {},
        ["worker/pyproject.toml"],
        config,
    )
    assert topology["components"][0] == {
        "id": "worker",
        "root": "worker",
        "changed_files": ["worker/messaging/consumer.py"],
        "languages": ["python"],
        "file_roles": ["messaging", "implementation"],
        "responsibilities": ["background jobs"],
        "related_components": ["contracts"],
        "contracts": ["events"],
        "invariants": ["delivery is idempotent"],
        "configured": True,
    }


def test_generated_artifact_availability_accounts_for_workspace_outputs():
    config = {
        "generated_artifacts": [{
            "id": "client",
            "source_of_truth": ["api/openapi.yaml"],
            "generator_config": ["pom.xml"],
            "output_paths": ["target/generated-sources/**"],
        }],
    }
    missing = build_topology(
        files("api/openapi.yaml"),
        {},
        ["pom.xml", "api/openapi.yaml"],
        config,
    )
    assert (
        missing["generated_artifacts"][0]["availability"]
        == "not-generated-in-review-workspace"
    )
    present = build_topology(
        files("api/openapi.yaml"),
        {},
        ["pom.xml", "api/openapi.yaml"],
        config,
        workspace_paths=["target/generated-sources/Client.java"],
    )
    assert (
        present["generated_artifacts"][0]["availability"]
        == "available-in-review-workspace"
    )
