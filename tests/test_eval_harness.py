#!/usr/bin/env python3
"""Tests for the A/B evaluation harness."""

import json
import sys
import tempfile
from pathlib import Path

# Ensure scripts directory is on path for imports
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import pytest
from eval_harness import (
    BenchmarkCorpus,
    BenchmarkResult,
    KnownFinding,
    ReviewRun,
    compute_precision_recall,
    extract_findings_from_review,
    generate_report,
    load_known_findings,
)


# ---------------------------------------------------------------------------
# KnownFinding tests
# ---------------------------------------------------------------------------

class TestKnownFinding:
    def test_to_dict(self):
        f = KnownFinding(
            category="security",
            severity="high",
            description="Hardcoded key found",
            file_path="config.py",
            line_range=(10, 20),
        )
        d = f.to_dict()
        assert d["category"] == "security"
        assert d["severity"] == "high"
        assert d["description"] == "Hardcoded key found"
        assert d["file_path"] == "config.py"
        assert d["line_range"] == [10, 20]

    def test_from_dict(self):
        d = {
            "category": "correctness",
            "severity": "medium",
            "description": "Unreachable code",
            "file_path": None,
            "line_range": None,
        }
        f = KnownFinding.from_dict(d)
        assert f.category == "correctness"
        assert f.severity == "medium"
        assert f.description == "Unreachable code"
        assert f.file_path is None
        assert f.line_range is None

    def test_from_dict_with_line_range(self):
        d = {
            "category": "style",
            "severity": "info",
            "description": "Missing docstring",
            "file_path": "main.py",
            "line_range": [5, 10],
        }
        f = KnownFinding.from_dict(d)
        assert f.line_range == (5, 10)


# ---------------------------------------------------------------------------
# Corpus loading tests
# ---------------------------------------------------------------------------

class TestBenchmarkCorpus:
    def test_from_file(self):
        corpus_data = {
            "benchmark_corpus": [
                {
                    "number": 1,
                    "repo_full_name": "test/repo",
                    "url": "https://github.com/test/repo/pull/1",
                    "title": "Test PR",
                    "known_findings": [
                        {"category": "security", "severity": "high", "description": "Bug"}
                    ],
                }
            ]
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(corpus_data, f)
            f.flush()
            path = Path(f.name)

        try:
            corpus = BenchmarkCorpus.from_file(path)
            assert len(corpus.prs) == 1
            assert corpus.prs[0]["number"] == 1
            findings = load_known_findings(corpus.prs[0])
            assert len(findings) == 1
            assert findings[0].category == "security"
        finally:
            path.unlink()

    def test_empty_corpus(self):
        corpus_data = {"benchmark_corpus": []}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(corpus_data, f)
            f.flush()
            path = Path(f.name)

        try:
            corpus = BenchmarkCorpus.from_file(path)
            assert len(corpus.prs) == 0
        finally:
            path.unlink()


# ---------------------------------------------------------------------------
# Findings extraction tests
# ---------------------------------------------------------------------------

class TestExtractFindingsFromReview:
    def test_empty_markdown(self):
        run = ReviewRun(mode="tools_off", pr_number=1, repo_full_name="test/repo")
        assert extract_findings_from_review(run) == []

    def test_bracketed_findings(self):
        markdown = """## Review Summary

- [security/high] Hardcoded API key found in config.py
- [correctness/medium] Unreachable code after return

No other issues detected."""
        run = ReviewRun(
            mode="tools_off", pr_number=1, repo_full_name="test/repo",
            review_markdown=markdown,
        )
        findings = extract_findings_from_review(run)
        assert len(findings) == 2
        assert findings[0]["category"] == "security"
        assert findings[0]["severity"] == "high"
        assert findings[1]["category"] == "correctness"
        assert findings[1]["severity"] == "medium"

    def test_no_findings(self):
        markdown = """## Review Summary

All looks good. No issues detected."""
        run = ReviewRun(
            mode="tools_off", pr_number=1, repo_full_name="test/repo",
            review_markdown=markdown,
        )
        assert extract_findings_from_review(run) == []


# ---------------------------------------------------------------------------
# Quality comparison tests
# ---------------------------------------------------------------------------

class TestComputePrecisionRecall:
    def test_perfect_match(self):
        known = [
            KnownFinding("security", "high", "Hardcoded key"),
            KnownFinding("correctness", "medium", "Unreachable code"),
        ]
        found = [
            {"category": "security", "severity": "high", "description": "Hardcoded key found in config"},
            {"category": "correctness", "severity": "medium", "description": "Unreachable code after return"},
        ]
        result = compute_precision_recall(found, known)
        assert result["precision"] == 1.0
        assert result["recall"] == 1.0
        assert result["f1"] == 1.0
        assert result["total_found"] == 2
        assert result["total_known"] == 2

    def test_no_known_findings(self):
        found = [{"category": "security", "severity": "high", "description": "Bug"}]
        result = compute_precision_recall(found, [])
        assert result["precision"] == 0.0
        assert result["recall"] == 0.0
        assert result["total_found"] == 1
        assert result["total_known"] == 0

    def test_no_found_findings(self):
        known = [KnownFinding("security", "high", "Bug")]
        result = compute_precision_recall([], known)
        assert result["precision"] == 0.0
        assert result["recall"] == 0.0
        assert result["total_found"] == 0
        assert result["total_known"] == 1

    def test_partial_match(self):
        known = [
            KnownFinding("security", "high", "Hardcoded API key in config"),
            KnownFinding("correctness", "medium", "Unreachable code"),
            KnownFinding("style", "low", "Missing docstring"),
        ]
        found = [
            {"category": "security", "severity": "high", "description": "Found hardcoded key"},  # matches
            {"category": "correctness", "severity": "medium", "description": "Dead code detected"},  # no match (no word overlap with "Unreachable code")
        ]
        result = compute_precision_recall(found, known)
        assert result["precision"] == 0.5  # 1 of 2 found matches a known finding
        assert abs(result["recall"] - (1/3)) < 0.001   # 1 of 3 known found

    def test_no_match(self):
        known = [KnownFinding("security", "high", "Hardcoded key")]
        found = [{"category": "style", "severity": "info", "description": "Formatting issue"}]
        result = compute_precision_recall(found, known)
        assert result["precision"] == 0.0
        assert result["recall"] == 0.0

    def test_extra_findings_reduces_precision(self):
        known = [KnownFinding("security", "high", "Hardcoded key")]
        found = [
            {"category": "security", "severity": "high", "description": "Found hardcoded key"},
            {"category": "security", "severity": "high", "description": "Another security issue not in known set"},
        ]
        result = compute_precision_recall(found, known)
        assert result["precision"] < 1.0


# ---------------------------------------------------------------------------
# Report generation tests
# ---------------------------------------------------------------------------

class TestGenerateReport:
    def test_basic_report(self):
        corpus = BenchmarkCorpus(prs=[{
            "number": 1,
            "repo_full_name": "test/repo",
            "url": "https://github.com/test/repo/pull/1",
            "known_findings": [
                {"category": "security", "severity": "high", "description": "Hardcoded key"},
            ],
        }])

        results = [BenchmarkResult(
            pr_number=1,
            repo_full_name="test/repo",
            runs=[
                ReviewRun(mode="tools_off", pr_number=1, repo_full_name="test/repo",
                          verdict="request_changes", tokens_input=1000, tokens_output=500,
                          wall_clock_sec=5.0,
                          review_markdown="- [security/high] Hardcoded API key found in config"),
                ReviewRun(mode="plan_execute", pr_number=1, repo_full_name="test/repo",
                          verdict="request_changes", tokens_input=2000, tokens_output=800,
                          wall_clock_sec=10.0,
                          review_markdown="- [security/high] Hardcoded API key found in config\n- [correctness/medium] Unreachable code"),
            ],
        )]

        report = generate_report(results, corpus)

        assert report["metadata"]["total_prs"] == 1
        assert len(report["per_pr_results"]) == 1
        assert "tools_off" in report["mode_summary"]
        assert "plan_execute" in report["mode_summary"]
        assert report["mode_summary"]["tools_off"]["runs"] == 1
        assert report["mode_summary"]["plan_execute"]["runs"] == 1

    def test_multiple_prs(self):
        corpus = BenchmarkCorpus(prs=[{
            "number": i,
            "repo_full_name": "test/repo",
            "url": f"https://github.com/test/repo/pull/{i}",
            "known_findings": [{"category": "security", "severity": "high", "description": f"Bug {i}"}],
        } for i in range(1, 4)])

        results = [BenchmarkResult(
            pr_number=i,
            repo_full_name="test/repo",
            runs=[
                ReviewRun(mode="tools_off", pr_number=i, repo_full_name="test/repo",
                          tokens_input=1000, tokens_output=500, wall_clock_sec=5.0),
            ],
        ) for i in range(1, 4)]

        report = generate_report(results, corpus)
        assert report["metadata"]["total_prs"] == 3
        assert len(report["per_pr_results"]) == 3
        assert report["mode_summary"]["tools_off"]["runs"] == 3


# ---------------------------------------------------------------------------
# Sample corpus validation
# ---------------------------------------------------------------------------

class TestSampleCorpus:
    def test_sample_corpus_loads(self):
        sample_path = Path(__file__).parent / "sample-benchmark-corpus.json"
        if not sample_path.exists():
            pytest.skip("sample-benchmark-corpus.json not found")
        corpus = BenchmarkCorpus.from_file(sample_path)
        assert len(corpus.prs) >= 3
        # Verify known findings are loadable
        for pr in corpus.prs:
            findings = load_known_findings(pr)
            assert len(findings) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
