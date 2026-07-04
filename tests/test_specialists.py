import json

import pytest

from pr_reviewer.specialists import (
    apply_exclusions,
    build_topology,
    candidate_key,
    classify_file_roles,
    coverage_gaps,
    deterministic_focuses,
    empty_config,
    load_specialist_config,
    normalize_focus,
    normalize_specialist_report,
    parse_diff_changed_lines,
    policy_notice,
    recipe_focuses,
    schedule_focuses,
    validate_candidates,
    validate_planner_plan,
)


def files(*paths):
    return [{"filename": path} for path in paths]


def test_topology_discovers_monorepo_components_and_generic_roles():
    tracked = [
        "pom.xml", "service-a/pom.xml", "worker/pyproject.toml",
        "service-a/src/Main.java", "worker/jobs/process.py", "contracts/events.proto",
    ]
    topology = build_topology(
        files("service-a/src/Main.java", "worker/jobs/process.py", "contracts/events.proto"),
        {"pr_kind": "app_code", "risk_flags": []}, tracked,
    )
    ids = {item["id"] for item in topology["components"]}
    assert {"service-a", "worker", "contracts"}.issubset(ids)
    assert "messaging" in topology["file_roles"]
    assert "schema-contract" in topology["file_roles"]
    assert topology["relationships"]


def test_single_component_repository_uses_repository_fallback():
    topology = build_topology(
        files("src/main.py", "tests/test_main.py"), {}, ["pyproject.toml", "src/main.py"]
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


def test_config_components_recipes_and_all_match_groups(tmp_path):
    config_path = tmp_path / "specialists.json"
    config_path.write_text(json.dumps({
        "version": 1,
        "components": [{
            "id": "worker", "paths": ["worker/**"],
            "responsibilities": ["background jobs"], "related_components": ["contracts"],
            "contracts": ["events"], "invariants": ["delivery is idempotent"],
        }],
        "recipes": [{
            "id": "delivery", "match": {
                "component_ids_any": ["worker"], "file_roles_any": ["messaging"]
            }, "title": "Delivery", "objective": "Trace delivery",
            "lenses": ["background-work-retry-idempotency"], "priority": "high",
        }],
        "exclude": {"paths": [], "components": [], "lenses": [], "recipes": []},
    }), encoding="utf-8")
    config = load_specialist_config(config_path)
    topology = build_topology(
        files("worker/messaging/consumer.py"), {}, ["worker/pyproject.toml"], config
    )
    assert topology["components"][0]["id"] == "worker"
    assert [item["id"] for item in recipe_focuses(config, topology)] == ["delivery"]

    topology["file_roles"] = ["implementation"]
    assert recipe_focuses(config, topology) == []


def test_invalid_config_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"version":2}', encoding="utf-8")
    with pytest.raises(ValueError):
        load_specialist_config(path)


def test_planner_accepts_arbitrary_focus_names_and_rejects_empty_plan():
    plan = validate_planner_plan({
        "summary": "worker change", "focuses": [{
            "id": "subtitle-extraction-worker-lifecycle",
            "title": "Subtitle extraction worker lifecycle",
            "objective": "Trace acknowledgement and cleanup",
            "lenses": ["a-repository-specific-lens"],
        }],
    })
    assert plan["focuses"][0]["id"] == "subtitle-extraction-worker-lifecycle"
    with pytest.raises(ValueError):
        validate_planner_plan({"focuses": []})


def test_authoritative_exclusions_strip_lenses_and_drop_fully_excluded_focus():
    topology = {
        "changed_files": ["worker/a.py", "docs/a.md"],
        "path_components": {"worker/a.py": "worker", "docs/a.md": "docs"},
    }
    config = {
        "exclude": {
            "paths": ["docs/**"], "components": [],
            "lenses": ["documentation"], "recipes": [],
        }
    }
    focuses = [
        normalize_focus({"id": "docs", "title": "Docs", "objective": "Review docs", "lenses": ["documentation"], "seed_paths": ["docs/a.md"]}),
        normalize_focus({"id": "worker", "title": "Worker", "objective": "Review worker", "lenses": ["component-correctness"], "seed_paths": ["worker/a.py"]}),
    ]
    kept, applied = apply_exclusions(focuses, config, topology)
    assert [item["id"] for item in kept] == ["worker"]
    assert any(item.get("dropped") for item in applied)


def test_matching_excluded_recipe_is_recorded_as_applied():
    topology = {"changed_files": ["a.py"], "path_components": {"a.py": "repository"}}
    config = {"exclude": {"paths": [], "components": [], "lenses": [], "recipes": ["custom"]}}
    recipe = normalize_focus({
        "id": "custom", "title": "Custom", "objective": "Review",
        "lenses": ["component-correctness"], "seed_paths": ["a.py"],
    }, source="recipe")
    kept, applied = apply_exclusions([recipe], config, topology)
    assert kept == []
    assert applied == [{"focus": "custom", "recipe": "custom", "dropped": True}]


def test_schedule_is_bounded_deduplicated_and_priority_ordered():
    topology = {"changed_files": ["a.py"], "path_components": {"a.py": "repository"}}
    config = {"exclude": {"paths": [], "components": [], "lenses": [], "recipes": []}}
    same = {"title": "A", "objective": "Review A", "lenses": ["component-correctness"], "seed_paths": ["a.py"]}
    planner = [normalize_focus({**same, "id": "p", "priority": "normal"})]
    recipes = [normalize_focus({**same, "id": "r", "priority": "high"}, source="recipe")]
    fallback = [normalize_focus({"id": "other", "title": "Other", "objective": "Other", "lenses": ["test-observability"], "seed_paths": ["a.py"]}, source="deterministic")]
    schedule = schedule_focuses(planner, recipes, fallback, config, topology, 1)
    assert schedule["selected"][0]["id"] == "p"
    assert schedule["selected"][0]["source_ids"] == ["p", "r"]
    assert schedule["merge_decisions"]
    assert len(schedule["omitted"]) == 1


def test_authorization_variants_merge_but_lifecycle_and_identity_do_not():
    topology = {
        "changed_files": ["app/api.py", "app/cache.py"],
        "path_components": {"app/api.py": "app", "app/cache.py": "app"},
        "components": [{"id": "app"}], "relationships": [], "risk_flags": ["auth_changes"],
    }
    auth = [normalize_focus({
        "id": "backend-security", "title": "Backend authorization",
        "objective": "Verify endpoint authorization and access control",
        "lenses": ["trust-boundary-security"], "seed_paths": ["app/api.py"],
        "invariants": ["authorization is enforced at the endpoint"], "priority": "critical",
    })]
    recipe = [normalize_focus({
        "id": "entrypoint-auth", "title": "Entrypoint authorization",
        "objective": "Verify endpoint authorization for callers",
        "lenses": ["trust-boundary-security"], "seed_paths": ["app/api.py"],
        "invariants": ["authorization is enforced at the endpoint"], "priority": "high",
    }, source="recipe")]
    lifecycle = normalize_focus({
        "id": "frontend-lifecycle", "title": "Reactive request lifecycle",
        "objective": "Check cancellation and reset state",
        "lenses": ["state-lifecycle-concurrency"], "seed_paths": ["app/cache.py"],
    })
    identity = normalize_focus({
        "id": "persistence-identity", "title": "Persistence identity mapping",
        "objective": "Check season and episode cache identity",
        "lenses": ["data-integrity-persistence"], "seed_paths": ["app/cache.py"],
    })
    schedule = schedule_focuses(auth + [lifecycle, identity], recipe, [], empty_config(), topology, 6)
    assert any(set(item["source_ids"]) == {"backend-security", "entrypoint-auth"}
               for item in schedule["selected"])
    selected_ids = {item["id"] for item in schedule["selected"]}
    assert {"frontend-lifecycle", "persistence-identity"}.issubset(selected_ids)


def test_marginal_selection_preserves_planner_component_and_lens_diversity():
    topology = {
        "changed_files": ["api/a.py", "ui/a.ts", "db/a.py"],
        "path_components": {"api/a.py": "api", "ui/a.ts": "ui", "db/a.py": "db"},
        "components": [{"id": item} for item in ("api", "ui", "db")],
        "relationships": [{"source": "api", "target": "ui", "reason": "contract"}],
        "risk_flags": [],
    }
    planner = [normalize_focus({
        "id": "ui-life", "title": "UI lifecycle", "objective": "Check request cancellation",
        "lenses": ["state-lifecycle-concurrency"], "seed_paths": ["ui/a.ts"], "priority": "normal",
    })]
    recipes = [normalize_focus({
        "id": f"api-{i}", "title": f"API recipe {i}", "objective": "Check API authorization",
        "lenses": ["trust-boundary-security"], "seed_paths": ["api/a.py"], "priority": "high",
    }, source="recipe") for i in range(3)]
    fallback = [normalize_focus({
        "id": "db-scout", "title": "DB scout", "objective": "Inspect persistence broadly",
        "lenses": ["component-correctness", "data-integrity-persistence"],
        "seed_paths": ["db/a.py"], "priority": "normal",
    }, source="deterministic")]
    selected = schedule_focuses(planner, recipes, fallback, empty_config(), topology, 3)["selected"]
    ids = {item["id"] for item in selected}
    assert "ui-life" in ids and "db-scout" in ids


def test_schedule_stops_when_remaining_focus_has_no_positive_marginal_coverage():
    topology = {
        "changed_files": ["app/a.py", "app/b.py"],
        "path_components": {"app/a.py": "app", "app/b.py": "app"},
        "components": [{"id": "app"}], "relationships": [], "risk_flags": [],
    }
    lenses = [
        "component-correctness", "test-observability", "interaction-data-flow",
        "state-lifecycle-concurrency", "protocol-contract-compatibility",
        "resource-boundary-numeric",
    ]
    focuses = [
        normalize_focus({
            "id": "primary", "title": "Alpha", "objective": "Investigate quasar",
            "lenses": lenses, "seed_paths": ["app/a.py"], "priority": "high",
        }),
        normalize_focus({
            "id": "redundant", "title": "Omega", "objective": "Examine nebula",
            "lenses": lenses, "seed_paths": ["app/b.py"], "priority": "low",
        }),
    ]

    schedule = schedule_focuses(focuses, [], [], empty_config(), topology, 6)

    assert [item["id"] for item in schedule["selected"]] == ["primary"]
    assert schedule["omitted"][0]["id"] == "redundant"
    assert schedule["omitted"][0]["omission_reason"] == "no positive marginal coverage"


def test_generated_artifact_availability_accounts_for_workspace_outputs():
    config = empty_config()
    config["generated_artifacts"] = [{
        "id": "client", "source_of_truth": ["api/openapi.yaml"],
        "generator_config": ["pom.xml"], "output_paths": ["target/generated-sources/**"],
    }]
    missing = build_topology(files("api/openapi.yaml"), {}, ["pom.xml", "api/openapi.yaml"], config)
    assert missing["generated_artifacts"][0]["availability"] == "not-generated-in-review-workspace"
    present = build_topology(
        files("api/openapi.yaml"), {}, ["pom.xml", "api/openapi.yaml"], config,
        workspace_paths=["target/generated-sources/Client.java"],
    )
    assert present["generated_artifacts"][0]["availability"] == "available-in-review-workspace"


def test_deterministic_fallback_is_generic_and_adds_interaction_focus():
    topology = build_topology(
        files("worker/jobs/a.py", "contracts/events.proto"),
        {"risk_flags": ["auth_changes"]},
        ["worker/pyproject.toml", "contracts/events.proto"],
    )
    ids = {item["id"] for item in deterministic_focuses(topology)}
    assert "risk-boundaries" in ids
    assert "component-interactions" in ids
    assert all("frontend" not in item and "backend" not in item for item in ids)


def test_coverage_requires_seed_and_interacting_components():
    focus = normalize_focus({
        "id": "flow", "title": "Flow", "objective": "Trace flow",
        "lenses": ["interaction-data-flow"],
        "seed_paths": ["a/**", "b/**"], "invariants": ["identity is preserved"],
    })
    topology = {
        "changed_files": ["a/a.py", "b/b.py"],
        "path_components": {"a/a.py": "a", "b/b.py": "b"},
    }
    report = {"inspected_files": ["a/a.py"], "invariants_checked": [], "findings": []}
    gaps = coverage_gaps(focus, report, topology)
    assert any("b/**" in gap for gap in gaps)
    assert any("two participating" in gap for gap in gaps)


def test_coverage_checks_expected_evidence_only_when_present():
    focus = normalize_focus({
        "id": "x", "title": "X", "objective": "Review",
        "expected_evidence": ["tests", "schema contract"],
    })
    topology = {"available_role_paths": {"test": ["tests/test_a.py"]}}
    gaps = coverage_gaps(focus, {"inspected_files": ["src/a.py"], "invariants_checked": []}, topology)
    assert gaps == ["inspect evidence category: tests"]


def test_report_requires_evidence_and_causal_chain():
    focus = normalize_focus({"id": "x", "title": "X", "objective": "Review X"})
    report = normalize_specialist_report({
        "completion_status": "complete",
        "findings": [
            {"claim": "unsupported"},
            {"claim": "real", "file": "a.py", "line": 3, "evidence": ["line 3"], "causal_chain": "input -> failure"},
        ],
    }, focus)
    assert [item["claim"] for item in report["findings"]] == ["real"]


def test_candidate_validation_rejects_out_of_scope_duplicates_and_critic_rejections():
    finding = {
        "severity": "major", "category": "bug", "file": "a.py", "line": 2,
        "claim": "bad", "evidence": ["proof"], "causal_chain": "x -> y",
    }
    outside = {**finding, "file": "old.py", "claim": "outside"}
    reports = [{"findings": [finding, dict(finding), outside]}]
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n x\n+bad\n"
    result = validate_candidates(reports, ["a.py"], diff)
    assert len(result["accepted"]) == 1
    assert result["accepted"][0]["inline_eligible"] is True
    assert {item["validation_reason"] for item in result["rejected"]} == {"duplicate", "outside-review-scope"}

    rejected = validate_candidates([{"findings": [finding]}], ["a.py"], diff, {candidate_key(finding)})
    assert rejected["accepted"] == []
    assert rejected["rejected"][0]["validation_reason"] == "critic-rejected"


def test_candidate_validation_groups_same_root_cause_and_merges_evidence():
    first = {
        "severity": "major", "category": "bug", "file": "a.py", "line": 5,
        "claim": "request remains loading", "evidence": ["effect cancels"],
        "causal_chain": "new request cancels old request leaving loading state active",
    }
    second = {
        **first, "line": 9, "claim": "retry is blocked", "evidence": ["guard sees loading"],
        "causal_chain": "old request cancellation leaves the loading state active",
    }
    result = validate_candidates([{"findings": [first, second]}], ["a.py"], "")
    assert len(result["accepted"]) == 1
    assert result["accepted"][0]["evidence"] == ["effect cancels", "guard sees loading"]
    assert result["rejected"][0]["validation_reason"] == "duplicate-root-cause"


def test_diff_line_parser_and_policy_notice():
    diff = "+++ b/a.py\n@@ -2,1 +2,2 @@\n old\n+new\n"
    assert parse_diff_changed_lines(diff)["a.py"] == {3}
    notice = policy_notice(".github/ai-review-specialists.json", True, [{"focus": "docs", "dropped": True}])
    assert "changed by this PR" in notice
    assert "`docs`" in notice
