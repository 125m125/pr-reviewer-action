#!/usr/bin/env python3
"""Tests for pr_reviewer.escalation — fast→smart escalation triggers (#160)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import pytest

from pr_reviewer.escalation import is_low_confidence, should_escalate


GOOD_REVIEW = (
    "## Recommendation\nApprove.\n\n"
    "The auth flow is unchanged for existing sessions and token rotation is "
    "preserved. Path sanitization uses realpath with containment checks, so "
    "directory traversal via ../ or symlinks is rejected. Tests cover the new "
    "edge cases and the migration carries no destructive statements.\n\n"
    "## Standards Compliance\nFollows repository conventions throughout."
)


def _write_fast_output(tmp_path, verdict="approve", review=GOOD_REVIEW):
    (tmp_path / "ai-output.json").write_text(
        json.dumps({"verdict": verdict, "review_markdown": review})
    )


def _write_classification(tmp_path, must_check=None):
    (tmp_path / "classification.json").write_text(
        json.dumps({"pr_kind": "app_code", "must_check": must_check or []})
    )


class TestIsLowConfidence:
    def test_substantial_review_is_confident(self):
        assert is_low_confidence(GOOD_REVIEW) is False

    def test_tiny_review_is_low_confidence(self):
        assert is_low_confidence("LGTM, approve.") is True

    def test_populated_unknowns_section_is_low_confidence(self):
        review = GOOD_REVIEW + (
            "\n\n## Unknowns or Needs Verification\n"
            "Could not verify the upstream release notes; the changelog fetch "
            "failed and the compare endpoint returned an error."
        )
        assert is_low_confidence(review) is True

    def test_empty_unknowns_section_is_confident(self):
        review = GOOD_REVIEW + "\n\n## Unknowns or Needs Verification\nNone."
        assert is_low_confidence(review) is False

    def test_unknowns_followed_by_next_header_only(self):
        review = GOOD_REVIEW + "\n\n## Unknowns\nN/A\n\n## Sources\n- corpus"
        assert is_low_confidence(review) is False


class TestShouldEscalate:
    def test_clean_confident_review_does_not_escalate(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fast_output(tmp_path)
        _write_classification(tmp_path)
        escalate, reasons = should_escalate()
        assert escalate is False and reasons == []

    def test_request_changes_triggers(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fast_output(tmp_path, verdict="request_changes")
        _write_classification(tmp_path)
        escalate, reasons = should_escalate()
        assert escalate is True
        assert "fast_request_changes" in reasons

    def test_request_changes_trigger_can_be_disabled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fast_output(tmp_path, verdict="request_changes")
        _write_classification(tmp_path)
        escalate, reasons = should_escalate(on_request_changes=False)
        assert escalate is False

    def test_incomplete_required_checks_trigger(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fast_output(
            tmp_path,
            review="A long enough review that talks about code style and naming "
            "conventions in detail but never the required security topics. "
            "It rambles for a while to clear the low-confidence length bar "
            "and looks plausible without addressing what matters here.",
        )
        _write_classification(
            tmp_path, must_check=["verify file path sanitization"]
        )
        escalate, reasons = should_escalate()
        assert "incomplete_required_checks" in reasons

    def test_low_confidence_trigger(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fast_output(tmp_path, review="Approve. Short.")
        _write_classification(tmp_path)
        escalate, reasons = should_escalate()
        assert "fast_low_confidence" in reasons

    def test_evidence_blocker_trigger(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fast_output(tmp_path)
        _write_classification(tmp_path)
        (tmp_path / "evidence-providers.json").write_text(
            json.dumps({"has_blocker": True, "providers": []})
        )
        escalate, reasons = should_escalate()
        assert reasons == ["tool_or_evidence_blockers"]

    def test_tool_harness_failure_trigger(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fast_output(tmp_path)
        _write_classification(tmp_path)
        (tmp_path / "tool-harness.json").write_text(
            json.dumps({"planning_error": "planner timed out", "tool_results": []})
        )
        escalate, reasons = should_escalate()
        assert reasons == ["tool_or_evidence_blockers"]

    def test_all_tool_requests_failed_trigger(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fast_output(tmp_path)
        _write_classification(tmp_path)
        (tmp_path / "tool-harness.json").write_text(
            json.dumps({
                "executed_request_count": 2,
                "tool_results": [{"status": "error"}, {"status": "error"}],
            })
        )
        escalate, reasons = should_escalate()
        assert reasons == ["tool_or_evidence_blockers"]

    def test_multiple_reasons_accumulate(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fast_output(tmp_path, verdict="request_changes", review="Too short.")
        _write_classification(tmp_path)
        escalate, reasons = should_escalate()
        assert escalate is True
        assert set(reasons) >= {"fast_request_changes", "fast_low_confidence"}

    def test_all_triggers_disabled(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fast_output(tmp_path, verdict="request_changes", review="Short.")
        _write_classification(tmp_path, must_check=["verify file path sanitization"])
        escalate, reasons = should_escalate(
            on_incomplete=False,
            on_request_changes=False,
            on_low_confidence=False,
            on_blockers=False,
        )
        assert escalate is False and reasons == []

    def test_missing_files_do_not_escalate(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        _write_fast_output(tmp_path)
        escalate, reasons = should_escalate()
        assert escalate is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
