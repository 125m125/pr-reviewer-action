import json
import zipfile

from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.test_results import (
    build_junit_manifest,
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


def test_build_junit_manifest_groups_multiple_language_reports(tmp_path):
    archive = tmp_path / "junit-test-results.zip"
    python = """<?xml version='1.0'?>
<testsuite name='pytest' tests='2' failures='1' skipped='0' errors='0'>
  <testcase classname='tests.test_notes' name='test_ok' file='tests/test_notes.py'/>
  <testcase classname='tests.test_notes' name='test_bad' file='tests/test_notes.py'>
    <failure message='expected COMMENT'>trace</failure>
  </testcase>
</testsuite>"""
    java = """<?xml version='1.0'?>
<testsuite name='maven' tests='1' failures='0' skipped='1' errors='0'>
  <testcase classname='com.example.ServiceTest' name='handlesRetry'>
    <skipped message='not supported'/>
  </testcase>
</testsuite>"""
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("python/pytest.xml", python)
        bundle.writestr("java/TEST-ServiceTest.xml", java)

    manifest = build_junit_manifest(
        (archive,), repository="owner/repo", head_sha="a" * 40,
    )

    assert manifest["repository"] == "owner/repo"
    assert manifest["head_sha"] == "a" * 40
    assert [item["name"] for item in manifest["reports"]] == [
        "junit-test-results.zip:java/TEST-ServiceTest.xml",
        "junit-test-results.zip:python/pytest.xml",
    ]
    assert manifest["statistics"] == {
        "source_reports": 2,
        "total": 3,
        "retained": 3,
        "passed": 1,
        "failed": 1,
        "skipped": 1,
        "errored": 0,
    }
    assert {test["status"] for report in manifest["reports"] for test in report["tests"]} == {
        "passed", "failed", "skipped",
    }


def test_build_junit_manifest_retains_failures_before_early_passing_overflow(tmp_path):
    archive = tmp_path / "junit.zip"
    cases = "".join(
        f"<testcase classname='suite' name='pass_{index}'/>"
        for index in range(2_000)
    )
    cases += "<testcase classname='suite' name='late_failure'><failure>boom</failure></testcase>"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("results.xml", f"<testsuite>{cases}</testsuite>")

    manifest = build_junit_manifest(
        (archive,), repository="owner/repo", head_sha="a" * 40,
    )

    retained = manifest["reports"][0]["tests"]
    assert len(retained) == 2_000
    assert any(test["name"].endswith("late_failure") for test in retained)
