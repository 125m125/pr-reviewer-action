from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from types import MappingProxyType, SimpleNamespace

import pytest

from pr_reviewer.specialist_runtime import cli
from pr_reviewer.specialist_runtime.budget import SessionLease
from pr_reviewer.specialist_runtime.callbacks import freeze_callback_value
from pr_reviewer.specialist_runtime.controller import ReviewResult, RoleRequest
from pr_reviewer.specialist_runtime.model_gateway import ModelTurnRequest
from pr_reviewer.specialist_runtime.policy import ReviewPolicy
from pr_reviewer.specialist_runtime.types import (
    ReviewHandoff,
    ReviewNote,
    ReviewNoteKind,
    RunPhase,
)


def runtime_source_paths() -> tuple[Path, ...]:
    root = Path(__file__).resolve().parent.parent
    return (
        root / "pr_reviewer" / "specialists.py",
        root / "pr_reviewer" / "tool_loop.py",
        root / "scripts" / "build_review_comments.py",
        root / "scripts" / "resolve_finding_threads.py",
        root / "scripts" / "publish_helpers.sh",
        root / "scripts" / "run_specialist_reviews.py",
    )


def test_removed_specialist_architecture_is_not_present():
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in runtime_source_paths()
    )
    for forbidden in (
        "class SequentialModelRunner",
        "def run_focus(",
        "max_rounds=max(4, max_tools * 2 + 2)",
        "initial_fallback_focuses(",
        "def schedule_focuses(",
        "def normalize_specialist_report(",
        "def legacy_diff_positions(",
        "def extract_marker_fingerprint(",
        "publish_specialist_review() {",
        "rounds = max_rounds * 2",
    ):
        assert forbidden not in sources


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
            "assignment_plan": {
                "source": "deterministic_fallback",
                "planner_repaired": False,
            },
            "degradation": [
                {
                    "component": "planner",
                    "reason": "invalid | plan\n### injected heading",
                },
                {
                    "component": "negotiator",
                    "reason": "fallback after <timeout>",
                },
            ],
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
    monkeypatch.setattr(cli, "build_controller", lambda config, **_kwargs: controller)

    assert cli.main() == 0

    compatibility = json.loads((tmp_path / "specialist-ai-output.json").read_text())
    assert compatibility == {
        "verdict": "request_changes",
        "review_markdown": "## AI review handoff\n\nReview the complete change.",
        "findings": [{
            "severity": "major",
            "category": "other",
            "file": "src/app.py",
            "line": 1,
            "message": "A detailed note",
        }],
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
    summary = (tmp_path / "specialist-review-summary.md").read_text()
    assert "- Assignment plan: `deterministic_fallback` (repaired: `false`)" in summary
    assert "| planner | invalid \\| plan ### injected heading |" in summary
    assert "| negotiator | fallback after &lt;timeout&gt; |" in summary
    assert "\n### injected heading" not in summary


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
    monkeypatch.setenv("SPECIALIST_PLANNER_MAX_CONTEXT_BYTES", "6543")
    monkeypatch.setenv("SPECIALIST_PLANNER_MAX_TOOL_CALLS", "7")
    monkeypatch.setenv("SPECIALIST_MAX_TRUNCATION_CONTINUATIONS", "3")
    monkeypatch.setenv("SPECIALIST_PACKET_MAX_BYTES", "87654")
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
    assert config.planner_max_context_bytes == 6543
    assert config.model_context_tokens == 32000
    assert config.temperature == 0.2
    assert gateway.tokens_param == "max_tokens"
    assert config.tool_response_bytes == 5432
    assert config.tool_request_timeout_sec == 7
    assert {
        "SPECIALIST_PLANNER_MAX_TOOL_CALLS",
        "SPECIALIST_MAX_TRUNCATION_CONTINUATIONS",
        "SPECIALIST_PACKET_MAX_BYTES",
    }.issubset(config.deprecation_warnings)

    from pr_reviewer.specialist_runtime.assignments import Assignment
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger
    from pr_reviewer.specialist_runtime.evidence import EvidenceStore

    session = controller._cli_session_factory(
        Assignment(
            id="a", title="A", objective="Review", obligation_ids=(),
            recipe_ids=(), lenses=(), seed_paths=(), boundary_paths=(),
            expected_evidence=(), estimated_turns=1, priority="normal",
        ),
        SessionLease(RunPhase.INITIAL, 10**20),
        None,
        EvidenceStore(),
        CoverageLedger(()),
        (),
        "session:test:g0",
    )
    assert session.recovery_max_tokens == 456


def test_specialist_diff_command_uses_controller_owned_review_range(
    monkeypatch, tmp_path,
):
    subprocess.run(
        ["git", "init", "-q"], cwd=tmp_path, check=True,
        capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "review@example.test"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Review Test"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    )
    reviewed = tmp_path / "reviewed.txt"
    reviewed.write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "reviewed.txt"], cwd=tmp_path, check=True,
        capture_output=True, text=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "base"], cwd=tmp_path, check=True,
        capture_output=True, text=True,
    )
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    reviewed.write_text("head\n", encoding="utf-8")
    subprocess.run(
        ["git", "commit", "-qam", "head"], cwd=tmp_path, check=True,
        capture_output=True, text=True,
    )
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True,
        capture_output=True, text=True,
    ).stdout.strip()

    monkeypatch.setenv("AI_BASE_URL", "http://model.invalid/v1")
    monkeypatch.setenv("AI_MODEL", "model")
    monkeypatch.setenv("IS_FORK_PR", "false")
    controller = cli.build_controller(
        cli.CliConfig.from_env(workspace=tmp_path),
        immutable_diff_range=(base_sha, head_sha),
    )

    from pr_reviewer.specialist_runtime.assignments import Assignment
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger
    from pr_reviewer.specialist_runtime.evidence import EvidenceStore

    session = controller._cli_session_factory(
        Assignment(
            id="a", title="A", objective="Review", obligation_ids=(),
            recipe_ids=(), lenses=(), seed_paths=(), boundary_paths=(),
            expected_evidence=(), estimated_turns=1, priority="normal",
        ),
        SessionLease(RunPhase.INITIAL, 10**20),
        None,
        EvidenceStore(),
        CoverageLedger(()),
        (),
        "session:test:g0",
    )
    result = session.execute_tool(
        "run_command",
        {
            "command": "git_diff_name_only",
            "base_sha": "3" * 40,
            "head_sha": "4" * 40,
        },
    )

    assert result["status"] == "ok"
    assert result["result"]["stdout"] == "reviewed.txt"
    assert subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, check=True,
        capture_output=True, text=True,
    ).stdout == ""


def test_controller_rejects_symbolic_or_malformed_diff_revisions(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("AI_BASE_URL", "http://model.invalid/v1")
    monkeypatch.setenv("AI_MODEL", "model")

    with pytest.raises(ValueError, match="immutable diff range"):
        cli.build_controller(
            cli.CliConfig.from_env(workspace=tmp_path),
            immutable_diff_range=("HEAD", "2" * 40),
        )


def test_planner_context_byte_limit_stops_oversized_request_before_transport(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("AI_MODEL", "local-model")
    monkeypatch.setenv("SPECIALIST_PLANNER_MAX_CONTEXT_BYTES", "64")
    controller = cli.build_controller(cli.CliConfig.from_env(workspace=tmp_path))
    captured = []
    controller.planner.gateway.transport = _successful_transport(captured)

    with pytest.raises(ValueError, match="planner context"):
        controller.planner.complete(RoleRequest(
            role="planner",
            request_id="planner:oversized",
            phase=RunPhase.PLANNING,
            lease=SessionLease(RunPhase.PLANNING, 10**20),
            timeout_sec=30,
            max_tokens=512,
            context={"diff_context": "x" * 500},
        ))

    assert captured == []


def test_planner_serializes_frozen_policy_context_without_mappingproxy_copy(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("AI_MODEL", "local-model")
    controller = cli.build_controller(cli.CliConfig.from_env(workspace=tmp_path))
    captured = []
    controller.planner.gateway.transport = _successful_transport(captured)
    frozen_context = freeze_callback_value({"policy": ReviewPolicy.minimal()})

    controller.planner.complete(RoleRequest(
        role="planner",
        request_id="planner:frozen-policy",
        phase=RunPhase.PLANNING,
        lease=SessionLease(RunPhase.PLANNING, 10**20),
        timeout_sec=30,
        max_tokens=512,
        context=frozen_context,
    ))

    assert captured
    message = captured[0]["messages"][-1]["content"]
    assert json.loads(message)["policy"]["version"] == 2


def _role_request(role: str, phase: RunPhase) -> RoleRequest:
    return RoleRequest(
        role=role,
        request_id=f"{role}:test",
        phase=phase,
        lease=SessionLease(phase, 10**20),
        timeout_sec=30,
        max_tokens=512,
        context={},
    )


def _successful_transport(captured):
    def transport(_base_url, _api_format, payload, _api_key, _timeout, **_kwargs):
        captured.append(payload)
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "{}"},
            }],
            "usage": {},
        }

    return transport


def test_default_lm_studio_requests_use_role_and_session_protocols_not_legacy_verdict_prompt(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("AI_MODEL", "local-model")
    monkeypatch.setenv("AI_RESPONSE_FORMAT", "off")
    monkeypatch.setenv("IS_FORK_PR", "false")
    config = cli.CliConfig.from_env(workspace=tmp_path)
    controller = cli.build_controller(config)
    captured = []
    gateway = controller.planner.gateway
    gateway.transport = _successful_transport(captured)

    controller.planner.complete(_role_request("planner", RunPhase.PLANNING))

    from pr_reviewer.specialist_runtime.assignments import Assignment
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger
    from pr_reviewer.specialist_runtime.evidence import EvidenceStore

    assignment = Assignment(
        id="a", title="A", objective="Review", obligation_ids=(), recipe_ids=(),
        lenses=(), seed_paths=(), boundary_paths=(), expected_evidence=(),
        estimated_turns=1, priority="normal",
    )
    session = controller._cli_session_factory(
        assignment, SessionLease(RunPhase.INITIAL, 10**20), None,
        EvidenceStore(), CoverageLedger(()), (), "session:test:g0",
    )
    gateway.complete(ModelTurnRequest(
        role="specialist",
        conversation=session.conversation,
        max_tokens=512,
        response_schema=None,
        tools_enabled=True,
        timeout_sec=30,
        stream=False,
    ))

    planner_payload, specialist_payload = captured
    planner_system = planner_payload["messages"][0]["content"]
    specialist_system = specialist_payload["messages"][0]["content"]
    forbidden = "Return STRICT JSON with keys verdict and review_markdown"
    assert forbidden not in planner_system
    assert forbidden not in specialist_system
    assert "bounded specialist assignment set" in planner_system
    assert "checkpoint" in specialist_system
    assert "final" in specialist_system
    assert "response_format" not in planner_payload
    assert "response_format" not in specialist_payload
    assert "tools" not in planner_payload
    assert specialist_payload["tools"]


def test_json_schema_mode_uses_json_object_for_each_controller_role(monkeypatch, tmp_path):
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("AI_MODEL", "local-model")
    monkeypatch.setenv("AI_RESPONSE_FORMAT", "json_schema")
    controller = cli.build_controller(cli.CliConfig.from_env(workspace=tmp_path))
    captured = []
    controller.planner.gateway.transport = _successful_transport(captured)

    roles = (
        ("planner", RunPhase.PLANNING, controller.planner),
        ("negotiator", RunPhase.FOLLOWUP, controller.negotiator),
        ("critic", RunPhase.FINALIZATION, controller.critic),
        ("finalizer", RunPhase.FINALIZATION, controller.finalizer),
    )
    for role, phase, adapter in roles:
        adapter.complete(_role_request(role, phase))

    assert len(captured) == len(roles)
    for payload in captured:
        assert payload["response_format"] == {"type": "json_object"}


def _shell_prompt_environment(
    tmp_path: Path, *, inline: str = "", file_name: str = "", mode: str = "replace"
) -> dict[str, str]:
    script_dir = Path(__file__).parents[1] / "scripts"
    config_source = (script_dir / "sections" / "config.sh").read_text(encoding="utf-8")
    functions = []
    for name in ("resolve_system_prompt", "apply_system_prompt_fragments"):
        match = re.search(
            rf"^{name}\(\) \{{\n(.*?)\n\}}", config_source, re.MULTILINE | re.DOTALL,
        )
        assert match is not None
        functions.append(f"{name}() {{\n{match.group(1)}\n}}\n")
    function_file = tmp_path / "prompt-functions.sh"
    function_file.write_text("\n".join(functions), encoding="utf-8")
    environment = os.environ.copy()
    environment.update({
        "ACTION_SCRIPT_DIR": script_dir.as_posix(),
        "PROMPT_FUNCTION_FILE": function_file.as_posix(),
        "SYSTEM_PROMPT": inline,
        "SYSTEM_PROMPT_FILE": file_name,
        "SYSTEM_PROMPT_MODE": mode,
        "REVIEW_STRATEGY": "specialists",
        "REPO": "owner/repo",
        "PR_NUMBER": "17",
        "AI_BASE_URL": "http://localhost:1234/v1",
        "AI_MODEL": "local-model",
        "GH_TOKEN": "test-token",
    })
    script = r'''
set -euo pipefail
SCRIPT_DIR="$ACTION_SCRIPT_DIR"
error() { printf '%s\n' "$*" >&2; }
source "$PROMPT_FUNCTION_FILE"
SYSTEM_PROMPT_ADDENDUM=""
SYSTEM_PROMPT_IS_DEFAULT=0
resolve_system_prompt
apply_system_prompt_fragments
printf '%s\0%s\0%s\0%s\0' \
  "$SYSTEM_PROMPT" "${SYSTEM_PROMPT_IS_DEFAULT:-0}" \
  "${SYSTEM_PROMPT_ADDENDUM:-}" "$SYSTEM_PROMPT_MODE"
'''
    completed = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    prompt, is_default, addendum, resolved_mode, _ = completed.stdout.split(b"\0")
    return {
        **environment,
        "SYSTEM_PROMPT": prompt.decode(),
        "SYSTEM_PROMPT_IS_DEFAULT": is_default.decode(),
        "SYSTEM_PROMPT_ADDENDUM": addendum.decode(),
        "SYSTEM_PROMPT_MODE": resolved_mode.decode(),
    }


def test_shell_prompt_provenance_gives_specialists_neutral_default_and_custom_semantics(
    tmp_path
):
    default_env = _shell_prompt_environment(tmp_path)
    assert "Return STRICT JSON with keys verdict" in default_env["SYSTEM_PROMPT"]
    default_config = cli.CliConfig.from_env(default_env, workspace=tmp_path)
    assert default_config.system_prompt == cli._REVIEW_GUIDANCE

    custom_file = tmp_path / "review-prompt.txt"
    custom_file.write_text("FILE CUSTOM PROMPT", encoding="utf-8")
    cases = (
        ("INLINE CUSTOM PROMPT", "", "replace", "INLINE CUSTOM PROMPT"),
        ("", "review-prompt.txt", "replace", "FILE CUSTOM PROMPT"),
        (
            "INLINE CUSTOM PROMPT", "", "append",
            cli._REVIEW_GUIDANCE + "\n\nINLINE CUSTOM PROMPT",
        ),
        (
            "", "review-prompt.txt", "append",
            cli._REVIEW_GUIDANCE + "\n\nFILE CUSTOM PROMPT",
        ),
    )
    for inline, file_name, mode, expected in cases:
        resolved = _shell_prompt_environment(
            tmp_path, inline=inline, file_name=file_name, mode=mode,
        )
        config = cli.CliConfig.from_env(resolved, workspace=tmp_path)
        assert config.system_prompt == expected
        assert "Return STRICT JSON with keys verdict" not in config.system_prompt


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


def test_automatic_policy_change_uses_base_head_non_widening_policy(
    monkeypatch, tmp_path,
):
    write_review_workspace(tmp_path)
    policy = tmp_path / ".github" / "ai-review-policy.json"
    policy.parent.mkdir()
    policy.write_text(json.dumps({
        "version": 2,
        "publishing": {
            "allowed_modes": ["review_verdict"], "allow_approve": True,
        },
    }), encoding="utf-8")
    base_policy = json.dumps({
        "version": 2,
        "publishing": {
            "allowed_modes": ["comment"], "allow_approve": False,
        },
    }).encode()

    class Result:
        returncode = 0
        stdout = base_policy

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO", "owner/repo")
    monkeypatch.setattr(cli, "_git_changed_files", lambda *_: ("src/app.py",))
    monkeypatch.setattr(cli, "_tracked_paths", lambda *_: ())
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: Result())

    workspace = cli.load_workspace(cli.CliConfig.from_env())

    assert workspace.inputs.policy.publishing["allowed_modes"] == ("comment",)
    assert workspace.inputs.policy.publishing["allow_approve"] is False
    authorization = workspace.inputs.pr_metadata["policy_authorization"]
    assert authorization["changed"] is True
    assert authorization["authorized"] is False
    assert "non-widening" in " ".join(workspace.inputs.configuration_warnings)


def test_manual_rereview_authorizes_validated_head_policy(monkeypatch, tmp_path):
    write_review_workspace(tmp_path)
    policy = tmp_path / ".github" / "ai-review-policy.json"
    policy.parent.mkdir()
    policy.write_text(json.dumps({
        "version": 2,
        "publishing": {
            "allowed_modes": ["review_comment"], "allow_approve": False,
        },
    }), encoding="utf-8")
    base_policy = json.dumps({
        "version": 2,
        "publishing": {
            "allowed_modes": ["comment"], "allow_approve": False,
        },
    }).encode()

    class Result:
        returncode = 0
        stdout = base_policy

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO", "owner/repo")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setattr(cli, "_git_changed_files", lambda *_: ("src/app.py",))
    monkeypatch.setattr(cli, "_tracked_paths", lambda *_: ())
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: Result())

    workspace = cli.load_workspace(cli.CliConfig.from_env())

    assert workspace.inputs.policy.publishing["allowed_modes"] == (
        "review_comment",
    )
    assert workspace.inputs.pr_metadata["policy_authorization"]["authorized"] is True


def test_deleting_v2_policy_cannot_switch_automatic_run_to_legacy_authority(
    monkeypatch, tmp_path,
):
    write_review_workspace(tmp_path)
    legacy = tmp_path / ".github" / "ai-review-specialists.json"
    legacy.parent.mkdir()
    legacy.write_text(json.dumps({
        "version": 1,
        "recipes": [{
            "id": "legacy", "objective": "Legacy review",
        }],
    }), encoding="utf-8")
    base_v2 = json.dumps({
        "version": 2,
        "publishing": {
            "allowed_modes": ["comment"], "allow_approve": False,
        },
    }).encode()
    base_legacy = legacy.read_bytes()

    class Result:
        def __init__(self, returncode, stdout=b""):
            self.returncode = returncode
            self.stdout = stdout

    def git_run(arguments, **_kwargs):
        revision_path = arguments[-1]
        if revision_path.endswith(".github/ai-review-policy.json"):
            return Result(0, base_v2)
        if revision_path.endswith(".github/ai-review-specialists.json"):
            return Result(0, base_legacy)
        raise AssertionError(arguments)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REPO", "owner/repo")
    monkeypatch.setattr(cli, "_git_changed_files", lambda *_: ("src/app.py",))
    monkeypatch.setattr(cli, "_tracked_paths", lambda *_: ())
    monkeypatch.setattr(cli.subprocess, "run", git_run)

    workspace = cli.load_workspace(cli.CliConfig.from_env())

    assert workspace.inputs.policy.publishing["allowed_modes"] == ("comment",)
    assert workspace.inputs.pr_metadata["policy_authorization"]["changed"] is True
    assert "non-widening" in " ".join(workspace.inputs.configuration_warnings)


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


@pytest.mark.parametrize("fork_state", ["unknown", ""])
def test_unknown_fork_identity_disables_specialist_tools(
    monkeypatch, tmp_path, fork_state,
):
    monkeypatch.setenv("AI_BASE_URL", "http://model.invalid/v1")
    monkeypatch.setenv("AI_MODEL", "model")
    if fork_state:
        monkeypatch.setenv("IS_FORK_PR", fork_state)
    else:
        monkeypatch.delenv("IS_FORK_PR", raising=False)
    monkeypatch.setenv("TOOL_ENABLE_FOR_FORKS", "true")
    config = cli.CliConfig.from_env(workspace=tmp_path)
    controller = cli.build_controller(config)
    factory = controller._cli_session_factory
    factory.source_policy = cli.SourcePolicy(())
    from pr_reviewer.specialist_runtime.assignments import Assignment
    from pr_reviewer.specialist_runtime.coverage import CoverageLedger
    from pr_reviewer.specialist_runtime.evidence import EvidenceStore

    assignment = Assignment(
        id="a", title="A", objective="Review", obligation_ids=(),
        recipe_ids=(), lenses=(), seed_paths=(), boundary_paths=(),
        expected_evidence=(), estimated_turns=1, priority="normal",
    )
    session = factory(
        assignment, SessionLease(RunPhase.INITIAL, 10**20), None,
        EvidenceStore(), CoverageLedger(()), (), "session:test:g0",
    )

    assert session.conversation.tool_schemas == []
