"""Offline representative-PR replay and adversarial acceptance tests."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.eval_harness import (
    BenchmarkCorpus,
    evaluate_specialist_replay,
)
from pr_reviewer.specialist_runtime.replay import (
    replay_fixture,
    replay_web_policy_fixture,
)


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "specialist_runtime"


def test_multilingual_replay_accounts_for_every_expected_obligation():
    result = replay_fixture(FIXTURES / "multilingual-pr")

    assert result.artifact["schema_version"] == 2
    assert set(result.artifact["coverage"]) == set(result.expected["obligation_ids"])
    assert result.unsupported_published_claims == ()
    assert result.elapsed_simulated_sec <= result.expected["deadline_sec"]
    assert evaluate_specialist_replay(
        result.artifact,
        result.expected,
        notes=result.notes,
        observed=result.observed,
    )["passed"] is True


def test_multilingual_fixture_exercises_cross_stack_and_v1_recipe_migration():
    result = replay_fixture(FIXTURES / "multilingual-pr")
    fixture = result.fixture

    assert fixture["policy_input_version"] == 1
    assert result.artifact["policy"]["version"] == 2
    assert result.artifact["assignment_plan"] == {
        "source": "model_repaired_validated",
        "planner_repaired": True,
        "unassigned_obligation_ids": [],
    }
    assert [item["id"] for item in result.artifact["assignments"]] == [
        "cross-stack-contract-audit",
    ]
    assert {
        item["language"] for item in fixture["representative_changes"]
    } >= {"Java", "TypeScript", "Python", "SQL", "YAML", "JSON"}
    assert {
        item["role"] for item in fixture["representative_changes"]
    } >= {
        "api",
        "consumer",
        "messaging",
        "schema",
        "migration",
        "deployment",
        "test",
    }
    assert all(
        value["status"] in {
            "covered", "partially_covered", "unresolved",
            "not_applicable", "suppressed_by_policy",
        }
        for value in result.artifact["recipes"].values()
    )


def test_sparse_handoff_keeps_detailed_findings_and_evidence_in_notes():
    result = replay_fixture(FIXTURES / "multilingual-pr")
    handoff = result.artifact["handoff"]["markdown"]

    assert "candidate-" not in handoff
    assert "evidence:" not in handoff
    assert "src/main/java" not in handoff
    assert result.artifact["notes"]
    assert all(note.file for note in result.notes if note.kind.value == "finding")
    assert all(note.evidence_ids for note in result.notes if note.kind.value == "finding")


def test_recorded_failure_injections_have_deterministic_terminal_behavior():
    result = replay_fixture(FIXTURES / "multilingual-pr")
    failures = result.failures

    assert failures["no_progress_resume"]["same_session"] is True
    assert failures["no_progress_resume"]["budget_reset"] is False
    assert failures["reconstruction"]["reason"] == "repetitive-transcript"
    assert failures["reconstruction"]["recoveries"] == 1
    assert failures["planner_repair"]["repair_requests"] == 1
    assert failures["planner_repair"]["source"] == "model_repaired_validated"
    assert failures["failed_critic"]["terminal"] is True
    assert failures["failed_critic"]["fallback"] == "conservative"
    assert failures["deadline_cutoff"]["deadline_violation"] is False
    assert failures["deadline_cutoff"]["finalization_reserved"] is True
    assert failures["completion_inversion"]["stable_projection"] is True
    assert failures["note_anchor_race"] == {
        "stable": True,
        "anchor_types": ["file", "line"],
    }


def test_web_policy_replay_keeps_discovery_non_evidentiary_and_denies_redirect_escape():
    result = replay_web_policy_fixture(FIXTURES / "web-policy-pr")
    serialized = json.dumps(result, sort_keys=True)

    assert result["approved_fetches"] == ["https://docs.example.org/reference/runtime"]
    assert result["source_denials"] == 2
    assert result["unsafe_fetch_attempts"] == 0
    assert result["source_access_requests"] == 1
    assert result["unapproved"][0] == {
        "url": "https://evil.example.net/leaked",
        "host": "evil.example.net",
        "path": "/leaked",
        "denial_reason": "source is not allowlisted by current policy",
    }
    assert "UNAPPROVED-SNIPPET-MUST-STAY-HIDDEN" not in serialized
    assert "REDIRECT-ESCAPE-BODY-MUST-STAY-HIDDEN" not in serialized
    assert result["request_note"]["kind"] == "source_access_request"


@pytest.mark.parametrize(
    ("mutation", "gate"),
    [
        ({"drop_mandatory_status": True}, "missing_mandatory_status"),
        ({"unsupported_public_claims": ["invented public claim"]}, "unsupported_public_claim"),
        ({"unsafe_fetch_attempts": 1}, "unsafe_fetch"),
        ({"budget_history": {"session:x": [3, 2]}}, "budget_reset"),
        ({"elapsed_simulated_sec": 301}, "deadline_violation"),
        ({"drop_expected_finding": True}, "missing_expected_finding"),
        ({"remove_retained_evidence": True}, "missing_evidence"),
        ({"head_sha": "3" * 40}, "head_mismatch"),
    ],
)
def test_eval_harness_fails_each_specialist_acceptance_gate(mutation, gate):
    replay = replay_fixture(FIXTURES / "multilingual-pr")
    artifact = json.loads(json.dumps(replay.artifact))
    observed = dict(replay.observed)
    observed.update(mutation)
    if observed.pop("drop_mandatory_status", False):
        artifact["coverage"].pop(replay.expected["mandatory_obligation_ids"][0])
    if observed.pop("drop_expected_finding", False):
        artifact["accepted_candidates"] = []
    if observed.pop("remove_retained_evidence", False):
        artifact["evidence"] = []
    if "head_sha" in observed:
        artifact["head_sha"] = observed.pop("head_sha")

    report = evaluate_specialist_replay(
        artifact,
        replay.expected,
        notes=replay.notes,
        observed=observed,
    )

    assert report["passed"] is False
    assert gate in report["failure_gates"]


def test_fixture_validation_fails_loudly_for_missing_expectations(tmp_path):
    fixture_dir = tmp_path / "broken"
    fixture_dir.mkdir()
    (fixture_dir / "fixture.json").write_text(
        json.dumps({"schema_version": 1, "id": "broken"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="fixture missing required fields"):
        replay_fixture(fixture_dir)


def test_agentic_corpus_exposes_an_offline_specialist_replay_entry():
    corpus = BenchmarkCorpus.from_file(ROOT / "evals" / "corpus-agentic.json")

    assert corpus.offline_specialist_replays == [{
        "id": "multilingual-specialist-runtime",
        "fixture": "../tests/fixtures/specialist_runtime/multilingual-pr",
    }]


def test_eval_harness_runs_offline_specialist_corpus_and_returns_acceptance_status(
    tmp_path,
):
    output = tmp_path / "offline-report.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "eval_harness.py"),
            "--corpus",
            str(ROOT / "evals" / "corpus-agentic.json"),
            "--offline-specialist-only",
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(output.read_text(encoding="utf-8"))
    replay = report["offline_specialist_replays"][0]
    assert replay["id"] == "multilingual-specialist-runtime"
    assert replay["passed"] is True
    assert replay["metrics"]["obligation_accounting"]["observed"] == 27
    assert replay["metrics"]["review_note_anchor_types"]["line"] == 1
    assert replay["metrics"]["finalization_reserve_seconds"] == 30
