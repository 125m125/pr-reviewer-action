from pathlib import Path

import yaml


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


def test_dogfood_workflow_always_uploads_only_structured_specialist_diagnostics():
    workflow = yaml.load(_WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["review"]["steps"]
    upload = next(
        item for item in steps
        if str(item.get("uses", "")).startswith("actions/upload-artifact@")
    )

    assert upload["uses"] == f"actions/upload-artifact@{_UPLOAD_SHA}"
    assert upload["if"] == "always()"
    assert upload["with"]["if-no-files-found"] == "warn"
    assert upload["with"]["retention-days"] == "14"
    uploaded_paths = {
        item.strip()
        for item in upload["with"]["path"].splitlines()
        if item.strip()
    }
    assert uploaded_paths == _STRUCTURED_DIAGNOSTICS
    assert not any(
        forbidden in path
        for path in uploaded_paths
        for forbidden in ("review-corpus", "ai-response", "tool-harness")
    )
