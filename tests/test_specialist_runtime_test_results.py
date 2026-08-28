import json

from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.test_results import (
    load_test_results,
    seed_test_results,
)


def test_load_test_results_accepts_reports_and_normalizes_cases(tmp_path):
    path = tmp_path / "test-results.json"
    path.write_text(json.dumps({
        "repository": "owner/repo",
        "head_sha": "a" * 40,
        "reports": [{
            "name": "python",
            "workflow": "validate",
            "job": "pytest",
            "tests": [{
                "name": "tests.test_notes::test_request_changes",
                "status": "failed",
                "file": "tests/test_notes.py",
                "line": 12,
                "message": "expected REQUEST_CHANGES",
            }],
        }],
    }), encoding="utf-8")

    results = load_test_results(path, repository="owner/repo", head_sha="a" * 40)

    assert results == ({
        "name": "tests.test_notes::test_request_changes",
        "status": "failed",
        "file": "tests/test_notes.py",
        "line": 12,
        "message": "expected REQUEST_CHANGES",
        "report": "python",
        "workflow": "validate",
        "job": "pytest",
    },)


def test_seed_test_results_creates_test_result_evidence(tmp_path):
    del tmp_path
    store = EvidenceStore()
    results = ({
        "name": "tests.test_notes::test_request_changes",
        "status": "failed",
        "file": "tests/test_notes.py",
        "line": 12,
        "message": "expected REQUEST_CHANGES",
        "report": "python",
        "workflow": "validate",
        "job": "pytest",
    },)

    seeded = seed_test_results(
        store, results, repository="owner/repo", head_sha="a" * 40,
    )

    assert len(seeded) == 1
    record = store.snapshot().records[0]
    assert record.category == "test-result"
    assert record.tool == "ci_test_results"
    assert record.source_path == "tests/test_notes.py"
    assert json.loads(record.content)["test"]["name"] == "tests.test_notes::test_request_changes"
    assert record.provenance.head_sha == "a" * 40


def test_load_test_results_rejects_traversal_source_paths(tmp_path):
    path = tmp_path / "test-results.json"
    path.write_text(json.dumps({
        "repository": "owner/repo",
        "head_sha": "a" * 40,
        "tests": [{"name": "unsafe", "status": "failed", "file": "../secret.txt"}],
    }), encoding="utf-8")

    results = load_test_results(path, repository="owner/repo", head_sha="a" * 40)

    assert results[0]["file"] == ""
