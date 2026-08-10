import json

import pytest

from pr_reviewer.specialist_runtime.policy import (
    RuntimeConfig,
    authorize_policy_change,
    load_review_policy,
)


def test_v1_recipe_defaults_to_coverage_and_remains_named(tmp_path):
    path = tmp_path / "specialists.json"
    path.write_text(json.dumps({
        "version": 1,
        "components": [{"id": "worker", "paths": ["worker/**"]}],
        "recipes": [{
            "id": "delivery", "match": {"file_roles_any": ["messaging"]},
            "title": "Delivery", "objective": "Trace retries",
        }],
        "exclude": {"paths": [], "components": [], "lenses": [], "recipes": []},
    }), encoding="utf-8")

    policy = load_review_policy(path)

    assert policy.version == 2
    assert policy.recipes[0].id == "delivery"
    assert policy.recipes[0].execution == "coverage"


def test_source_rules_reject_global_wildcard_and_http(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 2,
        "sources": [{"host": "*", "schemes": ["http"]}],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="source rule"):
        load_review_policy(path)


@pytest.mark.parametrize("execution", ["coverage", "dedicated", "independent"])
def test_v2_recipe_accepts_each_supported_execution_mode(tmp_path, execution):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 2,
        "recipes": [{"id": "delivery", "title": "Delivery", "objective": "Trace", "execution": execution}],
    }), encoding="utf-8")

    assert load_review_policy(path).recipes[0].execution == execution


def test_topology_projection_retains_coverage_rules_for_relevant_seed_selection(
    tmp_path,
):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 2,
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


def test_v2_policy_rejects_unknown_top_level_key(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"version": 2, "sources": [], "unsafe": True}), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown"):
        load_review_policy(path)


def test_source_rule_normalizes_valid_https_policy(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 2,
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
        "version": 2,
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
    path.write_text(json.dumps({"version": 2, **fragment}), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_review_policy(path)


def test_v2_security_sections_are_normalized_to_secure_executable_defaults(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 2,
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
        "version": 2,
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
        "version": 2,
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
        "version": 2,
        "publishing": {
            "allowed_modes": ["comment", "review_comment"],
            "allow_approve": False,
        },
    })
    head = load_review_policy_from_value({
        "version": 2,
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
        "version": 2,
        "publishing": {"allowed_modes": ["comment"], "allow_approve": False},
    })
    head = load_review_policy_from_value({
        "version": 2,
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
        "version": 2,
        "components": [{
            "id": "payments",
            "paths": ["services/payments/**"],
            "responsibilities": ["charge settlement"],
        }],
    })
    head = load_review_policy_from_value({
        "version": 2,
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
    assert config.deprecation_warnings == ("specialist_max_tool_calls_per_pass",)


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
    path.write_text(json.dumps({"version": 2, **fragment}), encoding="utf-8")

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
    path.write_text(json.dumps({"version": 2, **fragment_builder(unsafe_path)}), encoding="utf-8")

    with pytest.raises(ValueError, match="repository-relative paths"):
        load_review_policy(path)
