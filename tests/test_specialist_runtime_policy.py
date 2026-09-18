import json
import re

import pytest

from pr_reviewer.specialist_runtime.policy import (
    RuntimeConfig,
    authorize_policy_change,
    load_review_policy,
    parse_review_policy,
)


def test_v3_policy_parses_component_boundaries_and_precedence():
    policy = parse_review_policy({
        "version": 3,
        "components": [
            {"id": "backend", "paths": ["backend/**"]},
            {"id": "worker", "paths": ["worker/**"]},
        ],
        "ownership_precedence": ["backend", "worker"],
        "boundaries": [{
            "id": "backend-worker-messages",
            "contract_paths": ["contracts/**"],
            "participants": ["backend", "worker"],
            "contract_change_owner": "backend",
            "endpoint_paths": {"worker": ["worker/**/messaging/**"]},
            "objective": "Compare message producers and consumers.",
        }],
        "recipes": [{
            "id": "delivery",
            "objective": "Trace delivery semantics.",
            "execution": "integrated",
        }],
    })

    assert policy.version == 3
    assert policy.ownership_precedence == ("backend", "worker")
    assert policy.recipes[0].execution == "integrated"
    assert policy.boundaries[0].id == "backend-worker-messages"
    assert policy.boundaries[0].contract_paths == ("contracts/**",)
    assert policy.boundaries[0].participants == ("backend", "worker")
    assert policy.boundaries[0].contract_change_owner == "backend"
    assert policy.boundaries[0].endpoint_paths == {
        "worker": ("worker/**/messaging/**",),
    }
    assert policy.boundaries[0].objective == "Compare message producers and consumers."


def test_v3_policy_rejects_duplicate_precedence_ids():
    with pytest.raises(ValueError, match="ownership_precedence.*unique"):
        parse_review_policy({
            "version": 3,
            "components": [{"id": "backend", "paths": ["backend/**"]}],
            "ownership_precedence": ["backend", "backend"],
        })


def test_v3_policy_rejects_duplicate_boundary_participants():
    with pytest.raises(ValueError, match="boundary participants.*unique"):
        parse_review_policy({
            "version": 3,
            "components": [{"id": "backend", "paths": ["backend/**"]}],
            "boundaries": [{
                "id": "api",
                "participants": ["backend", "BACKEND"],
                "contract_change_owner": "backend",
                "objective": "Check API",
            }],
        })


def test_v3_policy_rejects_duplicate_normalized_endpoint_participants():
    with pytest.raises(ValueError, match="endpoint_paths keys.*unique"):
        parse_review_policy({
            "version": 3,
            "components": [{"id": "backend", "paths": ["backend/**"]}],
            "boundaries": [{
                "id": "api",
                "participants": ["backend"],
                "contract_change_owner": "backend",
                "endpoint_paths": {
                    "backend": ["backend/api/**"],
                    "BACKEND": ["backend/other/**"],
                },
                "objective": "Check API",
            }],
        })


@pytest.mark.parametrize("execution", ["coverage", "dedicated"])
def test_v3_policy_rejects_old_recipe_execution_with_migration_guidance(execution):
    with pytest.raises(ValueError, match="migrate"):
        parse_review_policy({
            "version": 3,
            "recipes": [{"id": "delivery", "execution": execution}],
        })


def test_v3_recipe_defaults_to_integrated():
    policy = parse_review_policy({
        "version": 3,
        "recipes": [{"id": "delivery", "objective": "Trace retries"}],
    })

    assert policy.recipes[0].execution == "integrated"


@pytest.mark.parametrize(
    "fragment, message",
    [
        (
            {"components": [
                {"id": "backend", "paths": ["backend/**"]},
                {"id": "BACKEND", "paths": ["other/**"]},
            ]},
            "component ids must be unique",
        ),
        (
            {
                "components": [{"id": "backend", "paths": ["backend/**"]}],
                "boundaries": [
                    {
                        "id": "api",
                        "participants": ["backend"],
                        "contract_change_owner": "backend",
                        "objective": "Check API",
                    },
                    {
                        "id": "API",
                        "participants": ["backend"],
                        "contract_change_owner": "backend",
                        "objective": "Check API again",
                    },
                ],
            },
            "boundary ids must be unique",
        ),
    ],
)
def test_v3_policy_rejects_duplicate_normalized_ids(fragment, message):
    with pytest.raises(ValueError, match=message):
        parse_review_policy({"version": 3, **fragment})


@pytest.mark.parametrize(
    "boundary, message",
    [
        (
            {
                "id": "api",
                "participants": ["backend", "missing"],
                "contract_change_owner": "backend",
                "objective": "Check API",
            },
            "unknown components",
        ),
        (
            {
                "id": "api",
                "participants": ["backend"],
                "contract_change_owner": "worker",
                "objective": "Check API",
            },
            "contract_change_owner.*participant",
        ),
        (
            {
                "id": "api",
                "participants": ["backend"],
                "contract_change_owner": "backend",
                "endpoint_paths": {"worker": ["worker/api/**"]},
                "objective": "Check API",
            },
            "endpoint_paths keys.*participants",
        ),
    ],
)
def test_v3_policy_rejects_unknown_boundary_participant_or_owner(boundary, message):
    with pytest.raises(ValueError, match=message):
        parse_review_policy({
            "version": 3,
            "components": [
                {"id": "backend", "paths": ["backend/**"]},
                {"id": "worker", "paths": ["worker/**"]},
            ],
            "boundaries": [boundary],
        })


@pytest.mark.parametrize(
    "boundary",
    [
        {
            "id": "api",
            "contract_paths": ["../contracts/**"],
            "participants": ["backend"],
            "contract_change_owner": "backend",
            "objective": "Check API",
        },
        {
            "id": "api",
            "participants": ["backend"],
            "contract_change_owner": "backend",
            "endpoint_paths": {"backend": [r"C:\outside\**"]},
            "objective": "Check API",
        },
    ],
)
def test_v3_policy_rejects_unsafe_boundary_paths(boundary):
    with pytest.raises(ValueError, match="repository-relative"):
        parse_review_policy({
            "version": 3,
            "components": [{"id": "backend", "paths": ["backend/**"]}],
            "boundaries": [boundary],
        })


def test_v3_policy_rejects_unknown_ownership_precedence_id():
    with pytest.raises(ValueError, match="ownership_precedence.*unknown"):
        parse_review_policy({
            "version": 3,
            "components": [{"id": "backend", "paths": ["backend/**"]}],
            "ownership_precedence": ["worker", "backend"],
        })


def test_missing_policy_names_expected_path_and_quick_start(tmp_path):
    path = tmp_path / ".github" / "ai-review-policy.json"

    with pytest.raises(
        ValueError,
        match=rf"{re.escape(str(path))}.*docs/review-policy-authoring.md#quick-start",
    ):
        load_review_policy(path)


def test_legacy_only_policy_requires_explicit_migration(tmp_path):
    path = tmp_path / "policy.json"
    legacy = tmp_path / "specialists.json"
    legacy.write_text('{"version": 1}', encoding="utf-8")

    with pytest.raises(ValueError, match=rf"{re.escape(str(legacy))}.*migration"):
        load_review_policy(path, legacy)


@pytest.mark.parametrize("version", [1, 2])
def test_old_policy_requires_explicit_migration(version):
    with pytest.raises(ValueError, match="migration"):
        parse_review_policy({"version": version, "components": [], "recipes": []})


def test_source_rules_reject_global_wildcard_and_http(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 3,
        "sources": [{"host": "*", "schemes": ["http"]}],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="source rule"):
        load_review_policy(path)


@pytest.mark.parametrize("execution", ["integrated", "independent"])
def test_v3_recipe_accepts_each_supported_execution_mode(tmp_path, execution):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 3,
        "recipes": [{"id": "delivery", "title": "Delivery", "objective": "Trace", "execution": execution}],
    }), encoding="utf-8")

    assert load_review_policy(path).recipes[0].execution == execution


def test_v3_recipe_normalizes_conditional_evidence_requirements(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 3,
        "recipes": [{
            "id": "delivery", "title": "Delivery", "objective": "Trace",
            "evidence_requirements": [{
                "id": "build-manifest",
                "category": "build manifest",
                "when": {"paths_any": ["pom.xml", "**/pom.xml"]},
                "seed_paths": ["pom.xml", "**/pom.xml"],
                "mode": "required",
            }, {
                "id": "runtime-artifact",
                "category": "generated output",
                "when": {"file_roles_any": ["generated-artifact"]},
                "mode": "one_of:artifact-proof",
            }],
        }],
    }), encoding="utf-8")

    recipe = load_review_policy(path).recipes[0]

    assert tuple(item.id for item in recipe.evidence_requirements) == (
        "build-manifest", "runtime-artifact",
    )
    assert recipe.evidence_requirements[0].when == {
        "paths_any": ("pom.xml", "**/pom.xml"),
    }
    assert recipe.evidence_requirements[1].mode == "one_of:artifact-proof"


@pytest.mark.parametrize("mutation, message", [
    ({"unknown": True}, "unknown"),
    ({"mode": "sometimes"}, "mode"),
    ({"seed_paths": ["../pom.xml"]}, "repository-relative"),
])
def test_v3_recipe_rejects_invalid_evidence_requirement(tmp_path, mutation, message):
    requirement = {"id": "manifest", "category": "build manifest", **mutation}
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 3,
        "recipes": [{
            "id": "delivery", "title": "Delivery", "objective": "Trace",
            "evidence_requirements": [requirement],
        }],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_review_policy(path)


def test_v3_recipe_rejects_duplicate_evidence_requirement_ids(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 3,
        "recipes": [{
            "id": "delivery", "title": "Delivery", "objective": "Trace",
            "evidence_requirements": [
                {"id": "manifest", "category": "build manifest"},
                {"id": "manifest", "category": "workflow"},
            ],
        }],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate"):
        load_review_policy(path)


def test_topology_projection_retains_coverage_rules_for_relevant_seed_selection(
    tmp_path,
):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 3,
        "recipes": [{
            "id": "delivery", "title": "Delivery", "objective": "Trace",
            "related_paths": ["integration/tests/**"],
        }],
        "coverage_rules": [{
            "id": "delivery-risk",
            "paths_any": ["worker/**"],
            "required_recipe_ids": ["delivery"],
            "risk_tier": "high",
            "unresolved_policy": "block_when_unresolved",
        }],
    }), encoding="utf-8")

    projection = load_review_policy(path).legacy_projection()

    assert projection["coverage_rules"] == [{
        "id": "delivery-risk",
        "paths_any": ["worker/**"],
        "required_recipe_ids": ["delivery"],
        "risk_tier": "high",
        "unresolved_policy": "block_when_unresolved",
    }]


def test_policy_projection_retains_v3_boundary_ownership_data():
    policy = parse_review_policy({
        "version": 3,
        "components": [
            {"id": "backend", "paths": ["backend/**"]},
            {"id": "worker", "paths": ["worker/**"]},
        ],
        "ownership_precedence": ["backend", "worker"],
        "boundaries": [{
            "id": "messages",
            "contract_paths": ["contracts/**"],
            "participants": ["backend", "worker"],
            "contract_change_owner": "backend",
            "endpoint_paths": {"worker": ["worker/messaging/**"]},
            "objective": "Check both sides.",
        }],
    })

    projection = policy.legacy_projection()

    assert projection["version"] == 3
    assert projection["ownership_precedence"] == ["backend", "worker"]
    assert projection["boundaries"] == [{
        "id": "messages",
        "contract_paths": ["contracts/**"],
        "participants": ["backend", "worker"],
        "contract_change_owner": "backend",
        "endpoint_paths": {"worker": ["worker/messaging/**"]},
        "objective": "Check both sides.",
    }]


def test_v3_policy_rejects_unknown_top_level_key(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 3, "sources": [], "unsafe": True}), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown"):
        load_review_policy(path)


def test_source_rule_normalizes_valid_https_policy(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 3,
        "sources": [{
            "host": "docs.example.com", "schemes": ["https"],
            "include_subdomains": False, "path_prefixes": ["/api", "/guides"],
            "classification": "official", "max_age_hours": 48,
        }],
    }), encoding="utf-8")

    rule = load_review_policy(path).sources[0]

    assert rule.host == "docs.example.com"
    assert rule.include_subdomains is False
    assert rule.path_prefixes == ("/api", "/guides")
    assert rule.classification == "official"
    assert rule.max_age_hours == 48
    assert rule.schemes == ("https",)


def test_subdomain_source_grants_are_rejected_until_registrable_domain_validation_exists(
    tmp_path,
):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 3,
        "sources": [{
            "host": "docs.example.com",
            "include_subdomains": True,
            "schemes": ["https"],
        }],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="include_subdomains"):
        load_review_policy(path)


@pytest.mark.parametrize(
    "fragment, message",
    [
        (
            {"coverage_rules": [{
                "id": "auth", "risk_flags_any": ["auth_changes"],
                "required_recipe_ids": ["api"], "unknown": True,
            }]},
            "coverage rule",
        ),
        (
            {"verdict_policy": {"blocking_severities": 42}},
            "blocking_severities",
        ),
        (
            {"verdict_policy": {"blocking_severities": ["minor"]}},
            "blocking_severities",
        ),
        (
            {"verdict_policy": {"high_risk_tiers": ["normal"]}},
            "high_risk_tiers",
        ),
        (
            {"verdict_policy": {"blocker_requires_request_changes": False}},
            "blocker_requires_request_changes",
        ),
        (
            {"verdict_policy": {"require_evidence_for_findings": False}},
            "require_evidence_for_findings",
        ),
        (
            {"verdict_policy": {"unknown": True}},
            "verdict_policy",
        ),
        (
            {"publishing": {"allowed_modes": ["review_comment"], "unknown": True}},
            "publishing",
        ),
        (
            {"publishing": {"allowed_modes": ["invalid"]}},
            "allowed_modes",
        ),
    ],
)
def test_sensitive_nested_policy_schema_fails_closed(tmp_path, fragment, message):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 3, **fragment}), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_review_policy(path)


def test_v3_security_sections_are_normalized_to_secure_executable_defaults(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 3,
        "recipes": [{
            "id": "api", "title": "API", "objective": "Trace authorization",
            "match": {"component_ids_any": ["api"]},
            "expected_evidence": ["implementation"],
            "priority": "high",
        }],
        "coverage_rules": [{
            "id": "auth", "risk_flags_any": ["auth_changes"],
            "required_recipe_ids": ["api"],
        }],
        "verdict_policy": {
            "blocker_requires_request_changes": True,
            "require_evidence_for_findings": True,
        },
        "publishing": {
            "allowed_modes": ["review_comment"],
            "allow_approve": False,
        },
    }), encoding="utf-8")

    policy = load_review_policy(path)

    assert policy.coverage_rules[0]["id"] == "auth"
    assert policy.coverage_rules[0]["risk_tier"] == "high"
    assert policy.coverage_rules[0]["unresolved_policy"] == "block_when_unresolved"
    assert policy.verdict_policy["blocking_severities"] == ("blocker", "major")
    assert policy.verdict_policy["high_risk_tiers"] == ("critical", "high")
    assert policy.publishing["allowed_modes"] == ("review_comment",)


def test_automatic_sensitive_policy_change_uses_non_widening_intersection():
    base = load_review_policy_from_value({
        "version": 3,
        "recipes": [{
            "id": "base-check", "objective": "Base obligation",
            "expected_evidence": ["tests"], "priority": "high",
        }],
        "sources": [{
            "host": "docs.example.com", "path_prefixes": ["/api"],
            "schemes": ["https"],
        }],
        "publishing": {
            "allowed_modes": ["comment"], "allow_approve": False,
        },
    })
    head = load_review_policy_from_value({
        "version": 3,
        "recipes": [],
        "sources": [{
            "host": "attacker.example", "path_prefixes": ["/"],
            "schemes": ["https"],
        }],
        "publishing": {
            "allowed_modes": ["review_verdict"], "allow_approve": True,
        },
    })

    decision = authorize_policy_change(
        base_policy=base,
        head_policy=head,
        authorized=False,
        base_hash="base-hash",
        head_hash="head-hash",
    )

    assert decision.changed is True
    assert decision.authorized is False
    assert {recipe.id for recipe in decision.policy.recipes} == {"base-check"}
    assert decision.policy.sources == ()
    assert decision.policy.publishing["allowed_modes"] == ("comment",)
    assert decision.policy.publishing["allow_approve"] is False
    assert "sources" in decision.changed_sections
    assert decision.base_hash == "base-hash"
    assert decision.head_hash == "head-hash"


def test_automatic_publishing_policy_uses_each_side_maximum_capability():
    base = load_review_policy_from_value({
        "version": 3,
        "publishing": {
            "allowed_modes": ["comment", "review_comment"],
            "allow_approve": False,
        },
    })
    head = load_review_policy_from_value({
        "version": 3,
        "publishing": {
            "allowed_modes": ["review_comment", "review_verdict"],
            "allow_approve": False,
        },
    })

    decision = authorize_policy_change(
        base_policy=base, head_policy=head, authorized=False,
    )

    assert decision.policy.publishing["allowed_modes"] == ("review_comment",)


def test_manual_sensitive_policy_change_uses_validated_head_policy():
    base = load_review_policy_from_value({
        "version": 3,
        "publishing": {"allowed_modes": ["comment"], "allow_approve": False},
    })
    head = load_review_policy_from_value({
        "version": 3,
        "publishing": {
            "allowed_modes": ["review_comment"], "allow_approve": False,
        },
    })

    decision = authorize_policy_change(
        base_policy=base,
        head_policy=head,
        authorized=True,
        base_hash="base",
        head_hash="head",
    )

    assert decision.policy == head
    assert decision.authorized is True
    assert decision.changed_sections == ("publishing",)


def test_unauthorized_component_change_keeps_base_component_authority():
    base = load_review_policy_from_value({
        "version": 3,
        "components": [{
            "id": "payments",
            "paths": ["services/payments/**"],
            "responsibilities": ["charge settlement"],
        }],
    })
    head = load_review_policy_from_value({
        "version": 3,
        "components": [{
            "id": "payments",
            "paths": ["docs/**"],
            "responsibilities": ["documentation only"],
        }],
    })

    decision = authorize_policy_change(
        base_policy=base, head_policy=head, authorized=False,
    )

    assert decision.policy.components == base.components


def test_unauthorized_boundary_change_keeps_base_ownership_authority():
    components = [
        {"id": "backend", "paths": ["backend/**"]},
        {"id": "worker", "paths": ["worker/**"]},
    ]
    base = load_review_policy_from_value({
        "version": 3,
        "components": components,
        "ownership_precedence": ["backend", "worker"],
        "boundaries": [{
            "id": "messages",
            "participants": ["backend", "worker"],
            "contract_change_owner": "backend",
            "objective": "Base boundary question.",
        }],
    })
    head = load_review_policy_from_value({
        "version": 3,
        "components": components,
        "ownership_precedence": ["worker", "backend"],
        "boundaries": [{
            "id": "messages",
            "participants": ["backend", "worker"],
            "contract_change_owner": "worker",
            "objective": "Head boundary question.",
        }],
    })

    decision = authorize_policy_change(
        base_policy=base, head_policy=head, authorized=False,
    )

    assert decision.policy.ownership_precedence == base.ownership_precedence
    assert decision.policy.boundaries == base.boundaries
    assert "ownership_precedence" in decision.changed_sections
    assert "boundaries" in decision.changed_sections


def test_runtime_config_uses_direct_defaults_and_legacy_aliases():
    config = RuntimeConfig.from_env({
        "SPECIALIST_MAX_TOOL_CALLS_PER_PASS": "17",
        "AI_REQUEST_TIMEOUT_SEC": "42",
        "SPECIALIST_PHASE_SHARES": '{"planning":10,"initial":60,"followup":20,"finalization":10}',
    })

    assert config.review_deadline_sec == 7200
    assert config.concurrency == 1
    assert config.model_request_timeout_sec == 42
    assert config.session_limits.tool_calls == 17
    assert config.max_total_model_turns == 320
    assert config.max_total_tool_calls == 640
    assert config.deprecation_warnings == ("specialist_max_tool_calls_per_pass",)


def test_runtime_config_accepts_controller_owned_global_leases():
    config = RuntimeConfig.from_env({
        "SPECIALIST_MAX_TOTAL_MODEL_TURNS": "111",
        "SPECIALIST_MAX_TOTAL_TOOL_CALLS": "333",
    })

    assert config.max_total_model_turns == 111
    assert config.max_total_tool_calls == 333


def test_runtime_config_controls_remediator_evidence_budget():
    assert RuntimeConfig.from_env({}).remediator_max_evidence_chars == 32_000
    config = RuntimeConfig.from_env({
        "SPECIALIST_REMEDIATOR_MAX_EVIDENCE_CHARS": "48000",
    })
    assert config.remediator_max_evidence_chars == 48_000


def load_review_policy_from_value(value):
    """Exercise the real file parser while keeping policy fixtures concise."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "policy.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return load_review_policy(path)


def test_runtime_config_rejects_invalid_phase_share_shape():
    with pytest.raises(ValueError, match="phase shares"):
        RuntimeConfig.from_env({"SPECIALIST_PHASE_SHARES": "[]"})


@pytest.mark.parametrize("fragment", [
    {"components": [{"id": "worker", "paths": ["safe/../../outside"]}]},
    {"recipes": [{"id": "recipe", "seed_paths": ["safe/../../outside"]}]},
    {"recipes": [{"id": "recipe", "related_paths": ["safe/../../outside"]}]},
    {"recipes": [{"id": "recipe", "match": {"paths_any": ["safe/../../outside"]}}]},
    {"generated_artifacts": [{"id": "generated", "source_of_truth": ["safe/../../outside"]}]},
    {"generated_artifacts": [{"id": "generated", "generator_config": ["safe/../../outside"]}]},
    {"generated_artifacts": [{"id": "generated", "output_paths": ["safe/../../outside"]}]},
    {"exclude": {"paths": ["safe/../../outside"]}},
])
def test_repository_policy_paths_reject_any_parent_segment(tmp_path, fragment):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 3, **fragment}), encoding="utf-8")

    with pytest.raises(ValueError, match="repository-relative paths"):
        load_review_policy(path)


@pytest.mark.parametrize("unsafe_path", [r"C:\outside\**", "/outside/**"])
@pytest.mark.parametrize("fragment_builder", [
    lambda path: {"components": [{"id": "worker", "paths": [path]}]},
    lambda path: {"recipes": [{"id": "recipe", "seed_paths": [path]}]},
    lambda path: {"recipes": [{"id": "recipe", "related_paths": [path]}]},
    lambda path: {"recipes": [{"id": "recipe", "match": {"paths_any": [path]}}]},
    lambda path: {"generated_artifacts": [{"id": "generated", "source_of_truth": [path]}]},
    lambda path: {"generated_artifacts": [{"id": "generated", "generator_config": [path]}]},
    lambda path: {"generated_artifacts": [{"id": "generated", "output_paths": [path]}]},
    lambda path: {"exclude": {"paths": [path]}},
])
def test_repository_policy_paths_reject_rooted_and_drive_qualified_forms(
    tmp_path, unsafe_path, fragment_builder
):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 3, **fragment_builder(unsafe_path)}), encoding="utf-8")

    with pytest.raises(ValueError, match="repository-relative paths"):
        load_review_policy(path)
