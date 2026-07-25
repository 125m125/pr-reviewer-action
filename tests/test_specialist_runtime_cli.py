from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from pr_reviewer.specialist_runtime import cli
from pr_reviewer.specialist_runtime.controller import ReviewResult
from pr_reviewer.specialist_runtime.types import ReviewHandoff, ReviewNote, ReviewNoteKind


def write_review_workspace(root: Path) -> None:
    (root / "pr.json").write_text(json.dumps({
        "number": 17,
        "baseRefOid": "b" * 40,
        "headRefOid": "h" * 40,
        "changedFiles": 1,
        "title": "Wire runtime",
        "body": "",
    }), encoding="utf-8")
    (root / "pr-files.raw.json").write_text(
        '[{"filename":"src/app.py","status":"modified"}]', encoding="utf-8"
    )
    (root / "classification.json").write_text(
        '{"pr_kind":"app_code","risk_flags":[]}', encoding="utf-8"
    )
    (root / "pr.diff").write_text(
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n",
        encoding="utf-8",
    )
    (root / "review-corpus.truncated.md").write_text("# corpus\n", encoding="utf-8")
    (root / "standards-context.md").write_text("# standards\n", encoding="utf-8")


class ScriptedController:
    def __init__(self, root: Path):
        self.root = root
        self.inputs = None

    def run(self, inputs):
        self.inputs = inputs
        artifact = {
            "schema_version": 2,
            "evaluation_status": "degraded",
            "publishing": {"ready": True, "mode": "review_comment", "allow_approve": False},
            "verdict": {"value": "request_changes", "source": "runtime-policy"},
        }
        (self.root / "specialist-review-artifact.json").write_text(
            json.dumps(artifact), encoding="utf-8"
        )
        return ReviewResult(
            artifact=MappingProxyType(artifact),
            handoff=ReviewHandoff(
                markdown="## AI review handoff\n\nReview the complete change.",
                recommendation="request_changes",
            ),
            notes=(ReviewNote(
                kind=ReviewNoteKind.FINDING,
                fingerprint="f" * 64,
                markdown="A detailed note",
                file="src/app.py",
                line=1,
                severity="major",
            ),),
            verdict="request_changes",
            verdict_source="runtime-policy",
            artifact_path=self.root / "specialist-review-artifact.json",
            publishing_ready=True,
        )


def test_cli_writes_structured_handoff_notes_artifact_and_compatibility_output(
    monkeypatch, tmp_path
):
    write_review_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REVIEW_STRATEGY", "specialists")
    monkeypatch.setenv("REPO", "owner/repo")
    monkeypatch.setenv("PUBLISH_MODE", "review_comment")
    monkeypatch.setattr(cli, "_git_changed_files", lambda *_: ("src/app.py",))
    controller = ScriptedController(tmp_path)
    monkeypatch.setattr(cli, "build_controller", lambda config: controller)

    assert cli.main() == 0

    compatibility = json.loads((tmp_path / "specialist-ai-output.json").read_text())
    assert compatibility == {
        "verdict": "request_changes",
        "review_markdown": "## AI review handoff\n\nReview the complete change.",
        "findings": [],
        "verdict_source": "runtime-policy",
    }
    assert (tmp_path / "review-handoff.md").read_text().startswith("## AI review handoff")
    assert json.loads((tmp_path / "review-handoff.json").read_text())["markdown"].startswith(
        "## AI review handoff"
    )
    assert isinstance(json.loads((tmp_path / "review-notes.json").read_text()), list)
    assert json.loads((tmp_path / "specialist-review-artifact.json").read_text())["schema_version"] == 2
    assert json.loads((tmp_path / "specialist-policy-result.json").read_text()) == {
        "verdict": "request_changes",
        "source": "runtime-policy",
        "blocking_finding_ids": [],
        "blocking_obligation_ids": [],
        "unknown_obligation_ids": [],
    }
    snapshot = json.loads((tmp_path / "specialist-changed-files.json").read_text())
    assert snapshot == ["src/app.py"]
    assert controller.inputs.head_sha == "h" * 40
    assert controller.inputs.changed_files == ("src/app.py",)


def test_cli_rejects_incomplete_or_wrong_current_head_snapshot(monkeypatch, tmp_path):
    write_review_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REVIEW_STRATEGY", "specialists")
    monkeypatch.setenv("REPO", "owner/repo")
    monkeypatch.setattr(cli, "_git_changed_files", lambda *_: ())

    with pytest.raises(ValueError, match="complete changed-file snapshot"):
        cli.load_workspace(cli.CliConfig.from_env())


def test_cli_accepts_only_complete_api_snapshot_bound_to_event_head(monkeypatch, tmp_path):
    write_review_workspace(tmp_path)
    (tmp_path / "pr-files-complete.json").write_text(
        '[{"filename":"src/app.py","status":"modified"}]', encoding="utf-8"
    )
    (tmp_path / "pr-files-head.txt").write_text("h" * 40, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO", "owner/repo")
    monkeypatch.setenv("PR_HEAD_SHA", "x" * 40)
    monkeypatch.setattr(
        cli, "_git_changed_files",
        lambda *_: pytest.fail("complete API snapshot must not depend on local git depth"),
    )

    with pytest.raises(ValueError, match="current PR head"):
        cli.load_workspace(cli.CliConfig.from_env())

    monkeypatch.setenv("PR_HEAD_SHA", "h" * 40)
    assert cli.load_workspace(cli.CliConfig.from_env()).inputs.changed_files == ("src/app.py",)


def test_build_controller_uses_openai_gateway_role_models_and_bounded_session(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_BASE_URL", "http://model.invalid/v1")
    monkeypatch.setenv("AI_API_KEY", "secret")
    monkeypatch.setenv("AI_API_FORMAT", "openai")
    monkeypatch.setenv("AI_MODEL", "default")
    monkeypatch.setenv("SPECIALIST_PLANNER_MODEL", "planner")
    monkeypatch.setenv("SPECIALIST_MODEL", "worker")
    monkeypatch.setenv("SPECIALIST_CRITIC_MODEL", "critic")
    monkeypatch.setenv("SPECIALIST_AGGREGATOR_MODEL", "finalizer")
    monkeypatch.setenv("SPECIALIST_PASS_TIMEOUT_SEC", "41")
    monkeypatch.setenv("SPECIALIST_MAX_TOKENS", "1234")
    monkeypatch.setenv("SPECIALIST_RECOVERY_MAX_TOKENS", "456")
    monkeypatch.setenv("MODEL_CONTEXT_TOKENS", "32000")
    monkeypatch.setenv("SPECIALIST_TEMPERATURE", "0.2")
    monkeypatch.setenv("SPECIALIST_STREAM_WATCHDOG", "false")
    monkeypatch.setenv("TOOL_MAX_RESPONSE_BYTES", "5432")
    monkeypatch.setenv("TOOL_REQUEST_TIMEOUT_SEC", "7")
    monkeypatch.setenv("SEARCH_URL", "https://search.example/search")
    config = cli.CliConfig.from_env(workspace=tmp_path)

    controller = cli.build_controller(config)

    gateway = controller.planner.gateway
    assert gateway.role_models == {
        "planner": "planner", "specialist": "worker", "negotiator": "critic",
        "critic": "critic", "finalizer": "finalizer",
    }
    assert gateway.stream_watchdog is False
    assert config.request_timeout_sec == 41
    assert config.max_tokens == 1234
    assert config.recovery_max_tokens == 456
    assert config.model_context_tokens == 32000
    assert config.temperature == 0.2
    assert gateway.tokens_param == "max_tokens"
    assert config.tool_response_bytes == 5432
    assert config.tool_request_timeout_sec == 7


def test_cli_ignores_legacy_source_hosts_and_warns_for_aliases(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ALLOWED_SOURCE_HOSTS", "example.com")
    monkeypatch.setenv("SPECIALIST_CONFIG_FILE", ".github/custom-specialists.json")
    monkeypatch.setenv("SPECIALIST_MAX_INITIAL_PASSES", "3")
    config = cli.CliConfig.from_env(workspace=tmp_path)
    cli.emit_deprecation_warnings(config)
    warning = capsys.readouterr().err
    assert "SPECIALIST_CONFIG_FILE" in warning
    assert "SPECIALIST_MAX_INITIAL_PASSES" in warning
    assert "ALLOWED_SOURCE_HOSTS" in warning


def test_invalid_current_policy_is_an_authoritative_controller_degradation(monkeypatch, tmp_path):
    write_review_workspace(tmp_path)
    policy = tmp_path / ".github" / "ai-review-policy.json"
    policy.parent.mkdir()
    policy.write_text('{"version":2,"sources":"not-an-array"}', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO", "owner/repo")
    monkeypatch.setattr(cli, "_git_changed_files", lambda *_: ("src/app.py",))

    workspace = cli.load_workspace(cli.CliConfig.from_env())

    assert workspace.policy_degraded is True
    assert workspace.inputs.configuration_warnings
    assert "locked minimal policy" in workspace.inputs.configuration_warnings[0]


def test_fork_sessions_do_not_advertise_tools_without_explicit_opt_in(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_BASE_URL", "http://model.invalid/v1")
    monkeypatch.setenv("AI_MODEL", "model")
    monkeypatch.setenv("IS_FORK_PR", "true")
    monkeypatch.setenv("TOOL_ENABLE_FOR_FORKS", "false")
    config = cli.CliConfig.from_env(workspace=tmp_path)
    controller = cli.build_controller(config)
    factory = controller._cli_session_factory
    factory.source_policy = cli.SourcePolicy(())
    from pr_reviewer.specialist_runtime.assignments import Assignment
    from pr_reviewer.specialist_runtime.budget import SessionLease
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger
    from pr_reviewer.specialist_runtime.evidence import EvidenceStore
    from pr_reviewer.specialist_runtime.types import RunPhase

    assignment = Assignment(
        id="a", title="A", objective="Review", obligation_ids=(), recipe_ids=(),
        lenses=(), seed_paths=(), boundary_paths=(), expected_evidence=(),
        estimated_turns=1, priority="normal",
    )
    session = factory(
        assignment, SessionLease(RunPhase.INITIAL, 10**20), None,
        EvidenceStore(), CoverageLedger(()), (), "session:test:g0",
    )
    assert session.conversation.tool_schemas == []
