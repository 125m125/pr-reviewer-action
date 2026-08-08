from __future__ import annotations

from pathlib import Path

from pr_reviewer.diff_prioritizer import manifest_from_diff, prioritize_diff
from scripts.prioritize_diff import _repo_relative_config


def _diff(path: str, body: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n+++ b/{path}\n"
        f"@@ -1 +1 @@\n{body}\n"
    )


def test_prioritizer_orders_orientation_and_contract_files_before_source() -> None:
    diff = "".join(
        (
            _diff("src/runtime.py", "+runtime change"),
            _diff("README.md", "+documented behavior"),
            _diff("application.properties", "+feature.enabled=true"),
        )
    )
    files = [
        {"filename": "src/runtime.py", "status": "modified", "additions": 1, "deletions": 0},
        {"filename": "README.md", "status": "modified", "additions": 1, "deletions": 0},
        {"filename": "application.properties", "status": "modified", "additions": 1, "deletions": 0},
    ]

    result = prioritize_diff(diff, files, max_bytes=len(diff) + 200)

    assert result.selected_paths == (
        "README.md",
        "application.properties",
        "src/runtime.py",
    )
    assert result.omitted_paths == ()
    assert result.text.index("README.md") < result.text.index("application.properties")


def test_lockfiles_are_deprioritized_after_normal_source_files() -> None:
    diff = "".join(
        (
            _diff("package-lock.json", "+\"left-pad\": \"1.0.0\""),
            _diff("src/runtime.py", "+runtime change"),
        )
    )
    files = [
        {"filename": "package-lock.json", "status": "modified"},
        {"filename": "src/runtime.py", "status": "modified"},
    ]

    result = prioritize_diff(diff, files, max_bytes=len(diff) + 100)

    assert result.selected_paths == ("src/runtime.py", "package-lock.json")


def test_prioritizer_respects_project_glob_priority_and_marks_omissions() -> None:
    diff = "".join(
        (
            _diff("README.md", "+documented behavior"),
            _diff("src/runtime.py", "+runtime change"),
        )
    )
    files = [
        {"filename": "README.md", "status": "modified", "additions": 1, "deletions": 0},
        {"filename": "src/runtime.py", "status": "modified", "additions": 1, "deletions": 0},
    ]

    result = prioritize_diff(
        diff,
        files,
        max_bytes=(
            len(_diff("src/runtime.py", "+runtime change"))
            + len("\n…[diff sections omitted; see changed-file index]\n".encode("utf-8"))
        ),
        config={"rules": [{"glob": "src/**/*.py", "priority": 1}]},
    )

    assert result.selected_paths == ("src/runtime.py",)
    assert result.omitted_paths == ("README.md",)
    assert "omitted" in result.text.lower()


def test_project_rule_can_deprioritize_a_default_category() -> None:
    diff = "".join(
        (_diff("README.md", "+documented behavior"), _diff("src/runtime.py", "+runtime change"))
    )
    files = [
        {"filename": "README.md", "status": "modified"},
        {"filename": "src/runtime.py", "status": "modified"},
    ]

    result = prioritize_diff(
        diff,
        files,
        max_bytes=len(_diff("src/runtime.py", "+runtime change")) + 80,
        config={"rules": [{"glob": "README.md", "priority": 90}]},
    )

    assert result.selected_paths[0] == "src/runtime.py"


def test_prioritizer_index_is_bounded_and_uses_authoritative_file_manifest() -> None:
    diff = _diff("src/runtime.py", "+runtime change")
    files = [
        {"filename": "src/runtime.py", "status": "modified", "additions": 4, "deletions": 2},
        {"filename": "unchanged-secret.env", "status": "modified", "additions": 99, "deletions": 0},
    ]

    result = prioritize_diff(diff, files, max_bytes=len(diff) + 200)

    assert "src/runtime.py" in result.index
    assert "| 4 | 2 |" in result.index
    assert "unchanged-secret.env" in result.index
    assert "unchanged-secret.env" not in result.text.split("…[diff sections omitted", 1)[0]


def test_prioritizer_keeps_notice_within_byte_budget() -> None:
    diff = "".join(
        (_diff("README.md", "+documented behavior"), _diff("src/runtime.py", "+runtime change"))
    )
    files = [
        {"filename": "README.md", "status": "modified", "additions": 1, "deletions": 0},
        {"filename": "src/runtime.py", "status": "modified", "additions": 1, "deletions": 0},
    ]

    result = prioritize_diff(diff, files, max_bytes=80)

    assert len(result.text.encode("utf-8")) <= 80


def test_manifest_from_diff_is_limited_to_diff_sections() -> None:
    diff = _diff("src/runtime.py", "+runtime change")

    assert manifest_from_diff(diff) == (
        {"filename": "src/runtime.py", "status": "modified"},
    )


def test_priority_config_must_be_repository_relative() -> None:
    assert _repo_relative_config(Path(".github/ai-review-diff-priorities.json")) is not None
    assert _repo_relative_config(Path("../outside.json")) is None
    assert _repo_relative_config(Path("/tmp/outside.json")) is None


def test_index_explains_when_api_manifest_page_is_bounded() -> None:
    result = prioritize_diff(
        _diff("src/runtime.py", "+runtime change"),
        [{"filename": "src/runtime.py", "status": "modified"}],
        max_bytes=1000,
        total_changed_files=101,
    )

    assert "first 1 of 101 changed files" in result.index
