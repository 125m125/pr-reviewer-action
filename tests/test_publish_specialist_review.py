import json

import pytest

from scripts import publish_specialist_review as cli


HEAD = "a" * 40


@pytest.fixture
def publication(tmp_path, monkeypatch):
    for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SERVER_URL", "GITHUB_REPOSITORY"):
        monkeypatch.delenv(key, raising=False)
    args = ["--mode", "comment", "--repo", "owner/repo", "--pr-number", "17", "--head-sha", HEAD]
    for option, value in (
        ("handoff", {"markdown": "Review"}), ("notes", []), ("files", []),
        ("policy-result", {"verdict": "approve", "source": "policy"}),
    ):
        path = tmp_path / (option + ".json")
        path.write_text(json.dumps(value), encoding="utf-8")
        args.extend(("--" + option, str(path)))
    diff = tmp_path / "review.diff"
    diff.write_text("", encoding="utf-8")
    args.extend(("--diff", str(diff)))
    captured = {}

    class Publisher:
        def __init__(self, *args, **kwargs):
            pass

        def publish(self, **kwargs):
            captured.update(kwargs)
            return {"review_completed": True, "publication_errors": []}

    monkeypatch.setattr(cli, "GitHubReviewPublisher", Publisher)
    monkeypatch.setattr(cli, "GhReviewClient", lambda **kwargs: object())
    return args, captured


def artifact():
    return {
        "schema_version": 2, "repository": "owner/repo", "head_sha": HEAD, "pr_number": 17,
        "policy": {"version": 3, "digest": "policy-digest", "config_digest": "config-digest"},
        "configuration": {"adapter": {"allowed_github_repositories": ["vendor/library"]}},
        "external_access": {"allowed_sources": [{
            "host": "docs.example.org", "path_prefixes": ["/reference"],
            "schemes": ["https"], "include_subdomains": False, "classification": "reference",
        }]},
    }


def test_cli_defaults_keep_cleanup_enabled_without_inventing_grants(publication):
    args, captured = publication
    assert cli.main(args) == 0
    assert captured["cleanup_previous_reviews"] is True
    assert captured["access_policy"] is None
    assert captured["allowed_github_repositories"] == ()
    assert not captured["run_url"]


def test_cli_uses_bound_effective_authorization_and_cleanup_switch(publication, tmp_path):
    args, captured = publication
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(artifact()), encoding="utf-8")
    run_url = "https://github.com/owner/repo/actions/runs/123"
    assert cli.main(args + [
        "--review-artifact", str(path), "--cleanup-previous-reviews", "false", "--run-url", run_url,
    ]) == 0
    assert captured["cleanup_previous_reviews"] is False
    assert captured["allowed_github_repositories"] == ("vendor/library",)
    assert captured["access_policy"].sources[0].host == "docs.example.org"
    assert captured["access_policy"].sources[0].path_prefixes == ("/reference",)
    assert captured["run_url"] == run_url


@pytest.mark.parametrize("key,value", [("repository", "other/repo"), ("head_sha", "b" * 40),
    ("pr_number", 18), ("head_sha", None), ("repository", None)])
def test_cli_rejects_unbound_artifact_before_publishing(publication, tmp_path, key, value):
    args, captured = publication
    data = artifact()
    data[key] = value
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        cli.main(args + ["--review-artifact", str(path)])
    assert not captured


def test_cli_does_not_read_grants_from_model_requests(publication, tmp_path):
    args, captured = publication
    data = artifact()
    data["configuration"] = {}
    data["external_access"] = {}
    data["source_access_requests"] = [{"host": "attacker.test"}]
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert cli.main(args + ["--review-artifact", str(path)]) == 0
    assert captured["access_policy"] is None
    assert captured["allowed_github_repositories"] == ()


def test_cli_builds_validated_run_url_from_github_environment(publication, monkeypatch):
    args, captured = publication
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.enterprise.example")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    assert cli.main(args) == 0
    assert captured["run_url"] == "https://github.enterprise.example/owner/repo/actions/runs/123"


def test_cli_workflow_rerun_identifies_distinct_attempt(publication, monkeypatch):
    args, captured = publication
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    assert cli.main(args) == 0
    first_url = captured["run_url"]
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    assert cli.main(args) == 0
    assert captured["run_url"] == "https://github.com/owner/repo/actions/runs/123/attempts/2"
    assert captured["run_url"] != first_url


def test_cli_accepts_explicit_attempt_url(publication):
    args, captured = publication
    url = "https://github.com/owner/repo/actions/runs/123/attempts/2"
    assert cli.main(args + ["--run-url", url]) == 0
    assert captured["run_url"] == url


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://github.com/other/repo/actions/runs/123",
    "https://user:secret@github.com/owner/repo/actions/runs/123"])
def test_cli_rejects_invalid_explicit_run_provenance(publication, url):
    args, captured = publication
    with pytest.raises(ValueError, match="run URL"):
        cli.main(args + ["--run-url", url])
    assert not captured


def test_cli_ignores_mismatched_environment_run_identity(publication, monkeypatch):
    args, captured = publication
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_REPOSITORY", "other/repo")
    assert cli.main(args) == 0
    assert not captured["run_url"]
