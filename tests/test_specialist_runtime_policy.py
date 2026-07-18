import json

import pytest

from pr_reviewer.specialist_runtime.policy import RuntimeConfig, load_review_policy


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
            "include_subdomains": True, "path_prefixes": ["/api", "/guides"],
            "classification": "official", "max_age_hours": 48,
        }],
    }), encoding="utf-8")

    rule = load_review_policy(path).sources[0]

    assert rule.host == "docs.example.com"
    assert rule.include_subdomains is True
    assert rule.path_prefixes == ("/api", "/guides")
    assert rule.classification == "official"
    assert rule.max_age_hours == 48
    assert rule.schemes == ("https",)


def test_runtime_config_uses_direct_defaults_and_legacy_aliases():
    config = RuntimeConfig.from_env({
        "SPECIALIST_MAX_TOOL_CALLS_PER_PASS": "17",
        "SPECIALIST_PHASE_SHARES": '{"planning":10,"initial":60,"followup":20,"finalization":10}',
    })

    assert config.review_deadline_sec == 7200
    assert config.concurrency == 1
    assert config.session_limits.tool_calls == 17
    assert config.deprecation_warnings == ("specialist_max_tool_calls_per_pass",)


def test_runtime_config_rejects_invalid_phase_share_shape():
    with pytest.raises(ValueError, match="phase shares"):
        RuntimeConfig.from_env({"SPECIALIST_PHASE_SHARES": "[]"})
