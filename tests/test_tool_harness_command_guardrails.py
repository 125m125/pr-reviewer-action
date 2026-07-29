import subprocess
from pathlib import Path

from scripts import run_tool_harness


def test_run_command_rejects_raw_shell_text(tmp_path):
    result = run_tool_harness.run_command("echo unsafe", tmp_path)

    assert "error" in result
    assert "Command not allowlisted" in result["error"]


def test_run_command_rejects_shell_metacharacter_suffix(tmp_path):
    result = run_tool_harness.run_command("git_status_short; cat .env", tmp_path)

    assert "error" in result
    assert "Command not allowlisted" in result["error"]


def test_run_command_executes_named_argv_only_definition(monkeypatch, tmp_path):
    seen = {}

    def fake_run(args, cwd, capture_output, text, timeout):
        seen.update(
            {
                "args": args,
                "cwd": cwd,
                "capture_output": capture_output,
                "text": text,
                "timeout": timeout,
            }
        )
        return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(run_tool_harness.subprocess, "run", fake_run)

    result = run_tool_harness.run_command("git_status_short", tmp_path)

    assert result["exit_code"] == 0
    assert result["stdout"] == "ok"
    assert result["command"] == "git_status_short"
    assert seen["args"] == ["git", "status", "--short"]
    assert seen["cwd"] == tmp_path


def test_run_command_uses_immutable_review_range_for_diff_commands(
    monkeypatch, tmp_path,
):
    seen = []
    base_sha = "1" * 40
    head_sha = "2" * 40

    def fake_run(args, cwd, capture_output, text, timeout):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(run_tool_harness.subprocess, "run", fake_run)

    stat = run_tool_harness.run_command(
        "git_diff_stat", tmp_path, base_sha=base_sha, head_sha=head_sha,
    )
    names = run_tool_harness.run_command(
        "git_diff_name_only", tmp_path, base_sha=base_sha, head_sha=head_sha,
    )

    assert stat["exit_code"] == 0
    assert names["exit_code"] == 0
    assert seen == [
        ["git", "diff", "--stat", "--find-renames", base_sha, head_sha, "--"],
        ["git", "diff", "--name-only", "--find-renames", base_sha, head_sha, "--"],
    ]


def test_run_command_preserves_legacy_worktree_diff_without_review_range(
    monkeypatch, tmp_path,
):
    seen = []

    def fake_run(args, cwd, capture_output, text, timeout):
        seen.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(run_tool_harness.subprocess, "run", fake_run)

    run_tool_harness.run_command("git_diff_stat", tmp_path)
    run_tool_harness.run_command("git_diff_name_only", tmp_path)

    assert seen == [
        ["git", "diff", "--stat", "HEAD"],
        ["git", "diff", "--name-only", "HEAD"],
    ]
