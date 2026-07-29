import io
import subprocess
from pathlib import Path

from pr_reviewer import tool_executors
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
        ["git", "diff", "--stat", "--find-renames", f"{base_sha}...{head_sha}", "--"],
        ["git", "diff", "--name-only", "--find-renames", f"{base_sha}...{head_sha}", "--"],
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


def test_read_pr_diff_uses_bounded_file_scoped_merge_base_argv(
    monkeypatch, tmp_path,
):
    seen = {}
    base_sha = "1" * 40
    head_sha = "2" * 40
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("head\n", encoding="utf-8")

    class FakeProcess:
        def __init__(self, args, cwd, stdout, stderr):
            seen["args"] = args
            self.stdout = io.BytesIO(
                b"diff --git a/src/app.py b/src/app.py\n"
                b"@@ -1 +1 @@\n-old\n+new\n"
            )
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

        def kill(self):
            self.returncode = -9

    def fake_popen(args, cwd, stdout, stderr):
        seen["args"] = args
        return FakeProcess(args, cwd, stdout, stderr)

    monkeypatch.setattr(tool_executors.subprocess, "Popen", fake_popen)

    result = tool_executors.execute_tool_request(
        "read_pr_diff",
        {
            "path": "src/app.py",
            "context_lines": 999,
            "offset": -5,
            "limit": 99999,
        },
        str(tmp_path),
        {"owner/repo"},
        "owner/repo",
        (),
        12000,
        15,
        base_sha=base_sha,
        head_sha=head_sha,
        allowed_diff_paths=("src/app.py",),
    )

    assert result["status"] == "ok"
    assert result["result"]["path"] == "src/app.py"
    assert "diff --git a/src/app.py b/src/app.py" in result["result"]["patch"]
    assert result["result"]["range"]["offset"] == 1
    assert seen["args"] == [
        "git",
        "--literal-pathspecs",
        "diff",
        "--no-ext-diff",
        "--no-color",
        "--find-renames",
        "--unified=20",
        f"{base_sha}...{head_sha}",
        "--",
        "src/app.py",
    ]


def test_read_pr_diff_treats_magic_looking_assigned_filename_literally(
    monkeypatch, tmp_path,
):
    magic_path = ":(top)**"
    base_sha = "1" * 40
    head_sha = "2" * 40

    class FakeProcess:
        def __init__(self, args):
            literal = args[:2] == ["git", "--literal-pathspecs"]
            patch = (
                b"diff --git a/:(top)** b/:(top)**\n-head\n+head magic\n"
                if literal
                else b"diff --git a/outside.txt b/outside.txt\n"
                b"+SHOULD-NOT-BE-EXPOSED\n"
            )
            self.stdout = io.BytesIO(patch)
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(
        tool_executors.subprocess, "Popen",
        lambda args, **kwargs: FakeProcess(args),
    )

    result = tool_executors.execute_tool_request(
        "read_pr_diff", {"path": magic_path}, str(tmp_path),
        set(), "", (), 12000, 15,
        base_sha=base_sha, head_sha=head_sha,
        allowed_diff_paths=(magic_path,),
    )

    assert result["status"] == "ok"
    assert "head magic" in result["result"]["patch"]
    assert "SHOULD-NOT-BE-EXPOSED" not in result["result"]["patch"]
    assert "outside.txt" not in result["result"]["patch"]


def test_read_pr_diff_bounds_capture_before_large_single_line_is_materialized(
    monkeypatch, tmp_path,
):
    base_sha = "1" * 40
    head_sha = "2" * 40
    path = "large.min.js"
    (tmp_path / path).write_text("head\n", encoding="utf-8")
    observed = {}

    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            observed["read_size"] = size
            assert 0 < size <= 12001
            return super().read(size)

    class FakeProcess:
        def __init__(self):
            self.stdout = BoundedStream(b"+" + b"x" * (5 * 1024 * 1024))
            self.returncode = None
            self.killed = False

        def kill(self):
            self.killed = True
            self.returncode = -9

        def wait(self, timeout):
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    process = FakeProcess()
    monkeypatch.setattr(
        tool_executors.subprocess, "Popen",
        lambda *args, **kwargs: process,
    )
    result = tool_executors.execute_tool_request(
        "read_pr_diff", {"path": path}, str(tmp_path),
        set(), "", (), 12000, 15,
        base_sha=base_sha, head_sha=head_sha,
        allowed_diff_paths=(path,),
    )

    assert result["status"] == "ok"
    assert observed["read_size"] == 12001
    assert process.killed is True
    assert len(result["result"]["patch"].encode("utf-8")) <= 12000
