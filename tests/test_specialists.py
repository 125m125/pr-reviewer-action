import subprocess

import pytest

from pr_reviewer import specialists
from pr_reviewer.specialists import build_topology, classify_file_roles


def _git(root, *args):
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_local_diff_facts_do_not_depend_on_github_patch_text(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "review@example.test")
    _git(tmp_path, "config", "user.name", "Review Test")
    (tmp_path / "src").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "src" / "worker.py").write_text(
        "def deliver(message):\n    return message\n",
        encoding="utf-8",
    )
    (tmp_path / ".github" / "workflows" / "review.yml").write_text(
        "jobs:\n  review:\n    steps: []\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "guide.md").write_text(
        "# Guide\n\nOld text.\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "operations.adoc").write_text(
        "= Operations\n\nOld text.\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    base_sha = _git(tmp_path, "rev-parse", "HEAD")

    (tmp_path / "src" / "worker.py").write_text(
        "def deliver(message):\n    return message\n\n"
        "def retry_delivery(message):\n    return deliver(message)\n",
        encoding="utf-8",
    )
    (tmp_path / ".github" / "workflows" / "review.yml").write_text(
        "jobs:\n  review:\n    steps:\n      - name: Verify immutable diff\n"
        "        run: pytest\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "guide.md").write_text(
        "# Guide\n\n## Immutable changes\n\nUses the local comparison.\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "operations.adoc").write_text(
        "= Operations\n\n== Review range\n\nUses base to head.\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "head")
    head_sha = _git(tmp_path, "rev-parse", "HEAD")
    paths = (
        "src/worker.py",
        ".github/workflows/review.yml",
        "docs/guide.md",
        "docs/operations.adoc",
    )

    change_facts = specialists.build_change_facts(
        tmp_path, base_sha, head_sha, paths,
    )
    topology = build_topology(
        [{"filename": path, "status": "modified"} for path in paths],
        {},
        paths,
        change_facts=change_facts,
    )

    assert change_facts["bounded"] is True
    assert change_facts["included_path_count"] == 4
    assert change_facts["omitted_path_count"] == 0
    facts = change_facts["facts"]
    assert topology["changed_contract_facts"]["src/worker.py"]["symbols"] == [
        "retry_delivery"
    ]
    workflow = topology["changed_contract_facts"][
        ".github/workflows/review.yml"
    ]
    assert "Verify immutable diff" in workflow["workflow_steps"]
    assert "run" in workflow["workflow_keys"]
    assert topology["changed_contract_facts"]["docs/guide.md"]["headings"] == [
        "Immutable changes"
    ]
    assert topology["changed_contract_facts"]["docs/operations.adoc"][
        "headings"
    ] == ["Review range"]
    assert "Uses the local comparison." in topology["changed_contract_facts"][
        "docs/guide.md"
    ]["change_excerpts"]


def test_authoritative_change_facts_stay_capped_when_api_patches_are_missing(
    monkeypatch,
    tmp_path,
):
    paths = tuple(f"src/module_{index}.py" for index in range(501))

    def fake_run(arguments, **_kwargs):
        if "--name-status" in arguments:
            stdout = "".join(f"M\t{path}\n" for path in paths)
        else:
            path = arguments[-1]
            stdout = (
                f"@@ -0,0 +1,2 @@\n"
                f"+def changed_{path.split('_')[-1].split('.')[0]}():\n"
                "+    pass\n"
            )
        return subprocess.CompletedProcess(arguments, 0, stdout, "")

    monkeypatch.setattr(specialists.subprocess, "run", fake_run)
    change_facts = specialists.build_change_facts(
        tmp_path, "a" * 40, "b" * 40, paths,
    )
    topology = build_topology(
        [{"filename": path, "status": "modified"} for path in paths],
        {},
        paths,
        change_facts=change_facts,
    )

    assert change_facts["included_path_count"] == 500
    assert change_facts["omitted_path_count"] == 1
    assert len(change_facts["facts"]) == 500
    assert paths[-1] not in change_facts["facts"]
    assert paths[-1] in topology["changed_contract_facts"]
    assert topology["changed_contract_facts"][paths[-1]]["symbols"] == []
    assert paths[-1] not in topology["change_facts"]["facts"]


def test_failed_per_path_diff_is_explicit_and_not_an_empty_authoritative_fact(
    monkeypatch,
    tmp_path,
):
    path = "src/app.py"

    def fake_run(arguments, **_kwargs):
        if "--name-status" in arguments:
            return subprocess.CompletedProcess(
                arguments, 0, f"M\t{path}\n", "",
            )
        return subprocess.CompletedProcess(
            arguments, 128, "", "fatal: diff failed",
        )

    monkeypatch.setattr(specialists.subprocess, "run", fake_run)

    change_facts = specialists.build_change_facts(
        tmp_path, "a" * 40, "b" * 40, (path,),
    )

    assert change_facts["status"] == "degraded"
    assert change_facts["facts"] == {}
    assert change_facts["failed_path_count"] == 1
    assert change_facts["omitted_path_count"] == 1
    assert change_facts["failures"] == [{
        "scope": "path",
        "path": path,
        "reason": "immutable diff command failed",
    }]


def test_failed_range_diff_is_explicit_and_skips_per_path_commands(
    monkeypatch,
    tmp_path,
):
    calls = []

    def fake_run(arguments, **_kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(
            arguments, 128, "", "fatal: bad range",
        )

    monkeypatch.setattr(specialists.subprocess, "run", fake_run)

    change_facts = specialists.build_change_facts(
        tmp_path, "a" * 40, "b" * 40, ("src/app.py",),
    )

    assert len(calls) == 1
    assert change_facts["status"] == "degraded"
    assert change_facts["facts"] == {}
    assert change_facts["omitted_path_count"] == 1
    assert change_facts["failures"] == [{
        "scope": "range",
        "reason": "immutable diff range unavailable",
    }]


def test_missing_git_executable_is_explicit_range_degradation(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        specialists.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            FileNotFoundError("git executable not found")
        ),
    )

    change_facts = specialists.build_change_facts(
        tmp_path, "a" * 40, "b" * 40, ("src/app.py",),
    )

    assert change_facts["status"] == "degraded"
    assert change_facts["facts"] == {}
    assert change_facts["failures"] == [{
        "scope": "range",
        "reason": "immutable diff command unavailable",
    }]


def test_per_path_oserror_is_explicit_path_degradation(monkeypatch, tmp_path):
    path = "src/app.py"
    calls = 0

    def fake_run(arguments, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                arguments, 0, f"M\t{path}\n", "",
            )
        raise OSError("cannot launch git")

    monkeypatch.setattr(specialists.subprocess, "run", fake_run)

    change_facts = specialists.build_change_facts(
        tmp_path, "a" * 40, "b" * 40, (path,),
    )

    assert change_facts["status"] == "degraded"
    assert change_facts["facts"] == {}
    assert change_facts["failures"] == [{
        "scope": "path",
        "path": path,
        "reason": "immutable diff command unavailable",
    }]


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


def test_modified_python_function_is_a_changed_contract_fact():
    """A changed body is attributed using its diff hunk/function context."""
    topology = build_topology(
        [{
            "filename": "pr_reviewer/specialist_runtime/controller.py",
            "status": "modified",
            "patch": (
                "@@ -2100,4 +2110,7 @@ def _handoff_context(self, state, status):\n"
                "         reviewed_obligations = tuple(\n"
                "+            item for item in state.obligations\n"
                "+            if evidence_by_obligation.get(item.id)\n"
            ),
        }],
        {},
        ("pr_reviewer/specialist_runtime/controller.py",),
    )

    facts = topology["changed_contract_facts"][
        "pr_reviewer/specialist_runtime/controller.py"
    ]
    assert facts["symbols"] == ["_handoff_context"]
    assert facts["hunk_summaries"] == [
        "new lines 2110-2116: def _handoff_context(self, state, status):",
    ]


def test_topology_labels_zero_count_new_range_as_deletion_only():
    topology = build_topology(
        [{
            "filename": "worker/legacy.py",
            "status": "modified",
            "patch": (
                "@@ -18,4 +18,0 @@ def legacy_delivery(message):\n"
                "-    send(message)"
            ),
        }],
        {},
        ("worker/legacy.py",),
    )

    assert topology["changed_contract_facts"]["worker/legacy.py"][
        "hunk_summaries"
    ] == [
        "deletion-only hunk near new-file line 18 (no new lines): "
        "def legacy_delivery(message):",
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
