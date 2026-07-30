from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
_WORKFLOW = _ROOT / ".github" / "workflows" / "ai-pr-review.yaml"
_UPLOAD_SHA = "ea165f8d65b6e75b540449e92b4886f43607fa02"
_STRUCTURED_DIAGNOSTICS = {
    "specialist-review-artifact.json",
    "review-handoff.json",
    "review-notes.json",
    "specialist-policy-result.json",
    "specialist-run-status.json",
    "specialist-changed-files.json",
    "specialist-review-summary.md",
}


def _upload_step_lines() -> list[str]:
    lines = _WORKFLOW.read_text(encoding="utf-8").splitlines()
    uses_index = next(
        index for index, line in enumerate(lines)
        if line.strip().startswith("uses: actions/upload-artifact@")
    )
    start = max(
        index for index in range(uses_index + 1)
        if lines[index].startswith("      - ")
    )
    end = next(
        (
            index for index in range(uses_index + 1, len(lines))
            if lines[index].startswith("      - ")
        ),
        len(lines),
    )
    return lines[start:end]


def _scalar(lines: list[str], key: str, indent: int) -> str:
    prefix = (" " * indent) + key + ":"
    value = next(
        line[len(prefix):].strip()
        for line in lines
        if line.startswith(prefix)
    )
    return value.split(" #", 1)[0].rstrip()


def _literal_block(lines: list[str], key: str, indent: int) -> set[str]:
    prefix = (" " * indent) + key + ": |"
    start = next(index for index, line in enumerate(lines) if line == prefix)
    content_indent = " " * (indent + 2)
    values = []
    for line in lines[start + 1:]:
        if not line.startswith(content_indent):
            break
        if line.strip():
            values.append(line.strip())
    return set(values)


def test_dogfood_workflow_always_uploads_only_structured_specialist_diagnostics():
    upload = _upload_step_lines()

    assert _scalar(upload, "uses", 8) == f"actions/upload-artifact@{_UPLOAD_SHA}"
    assert _scalar(upload, "if", 8) == "always()"
    assert _scalar(upload, "if-no-files-found", 10) == "warn"
    assert _scalar(upload, "retention-days", 10) == "14"
    uploaded_paths = _literal_block(upload, "path", 10)
    assert uploaded_paths == _STRUCTURED_DIAGNOSTICS
    assert not any(
        forbidden in path
        for path in uploaded_paths
        for forbidden in ("review-corpus", "ai-response", "tool-harness")
    )


def test_dogfood_workflow_temporarily_allows_large_planner_preflight():
    workflow = _WORKFLOW.read_text(encoding="utf-8").splitlines()

    assert _scalar(
        workflow, "specialist_planner_max_context_bytes", 10,
    ) == '"400000"'


def test_dogfood_workflow_allows_multiple_tools_per_specialist_turn():
    workflow = _WORKFLOW.read_text(encoding="utf-8").splitlines()

    assert _scalar(
        workflow, "specialist_max_model_turns_per_session", 10,
    ) == '"64"'
    assert _scalar(
        workflow, "specialist_max_tool_calls_per_session", 10,
    ) == '"128"'
