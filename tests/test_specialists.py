import pytest

from pr_reviewer.specialists import build_topology, classify_file_roles


def test_topology_extracts_bounded_changed_symbols_and_contract_names_from_patches():
    topology = build_topology(
        [
            {
                "filename": "action.yml",
                "status": "modified",
                "patch": (
                    "@@\n inputs:\n+  publish_mode:\n"
                    "+    description: Publish mode\n outputs:\n+  artifact_url:\n"
                    "+    description: Artifact URL"
                ),
            },
            {
                "filename": ".github/workflows/review.yml",
                "status": "added",
                "patch": (
                    "@@\n+      - name: Publish [review](https://evil.example) `now`\n"
                    "+        run: review"
                ),
            },
            {
                "filename": "src/planner.py",
                "status": "modified",
                "patch": "@@\n+def validate_assignment_plan():\n+    pass",
            },
        ],
        {},
        ("action.yml", ".github/workflows/review.yml", "src/planner.py"),
    )

    assert topology["changed_contract_facts"] == {
        "action.yml": {
            "symbols": [],
            "hunk_summaries": [],
            "action_inputs": ["publish_mode"],
            "workflow_steps": [],
            "change_type": "modifies",
        },
        ".github/workflows/review.yml": {
            "symbols": [],
            "hunk_summaries": [],
            "action_inputs": [],
            "workflow_steps": ["Publish review https://evil.example now"],
            "change_type": "adds",
        },
        "src/planner.py": {
            "symbols": ["validate_assignment_plan"],
            "hunk_summaries": [],
            "action_inputs": [],
            "workflow_steps": [],
            "change_type": "modifies",
        },
    }


def test_topology_retains_safe_change_type_when_patch_is_missing_or_truncated():
    topology = build_topology(
        [
            {"filename": "src/new.py", "status": "added"},
            {
                "filename": "docs/guide.md",
                "status": "modified",
                "patch": "@@\n Documentation body text only",
            },
        ],
        {},
        ("src/new.py", "docs/guide.md"),
    )

    assert topology["changed_contract_facts"] == {
        "src/new.py": {
            "symbols": [],
            "hunk_summaries": [],
            "action_inputs": [],
            "workflow_steps": [],
            "change_type": "adds",
        },
        "docs/guide.md": {
            "symbols": [],
            "hunk_summaries": [],
            "action_inputs": [],
            "workflow_steps": [],
            "change_type": "modifies",
        },
    }


def test_topology_extracts_bounded_changed_hunk_context():
    topology = build_topology(
        [{
            "filename": "worker/delivery.py",
            "status": "modified",
            "patch": (
                "@@ -15,4 +18,7 @@ def deliver(message):\n"
                " context\n"
                "+    acknowledge(message)\n"
                "@@ -70,2 +76,3 @@ class RetryQueue:\n"
                "+    attempts += 1"
            ),
        }],
        {},
        ("worker/delivery.py",),
    )

    assert topology["changed_contract_facts"]["worker/delivery.py"][
        "hunk_summaries"
    ] == [
        "new lines 18-24: def deliver(message):",
        "new lines 76-78: class RetryQueue:",
    ]


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
