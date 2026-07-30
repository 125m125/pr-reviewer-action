"""Offline representative-PR replay and adversarial acceptance tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import scripts.eval_harness as eval_harness_module
from pr_reviewer.specialist_runtime.adjudication import (
    AdjudicatedReview,
    ReviewHandoffContext,
    ReviewOrientationTopic,
    build_review_handoff,
)
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from scripts.eval_harness import (
    BenchmarkCorpus,
    evaluate_specialist_replay,
)
from pr_reviewer.specialist_runtime.replay import (
    replay_fixture,
    replay_web_policy_fixture,
)
from pr_reviewer.specialist_runtime.web_evidence import SourceAccessRequest


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
        adversarial_cases=result.failures,
    )["passed"] is True


def test_multilingual_fixture_exercises_cross_stack_and_v1_recipe_migration():
    result = replay_fixture(FIXTURES / "multilingual-pr")
    fixture = result.fixture

    assert fixture["policy_input_version"] == 1
    assert result.artifact["policy"]["version"] == 2
    assert result.artifact["assignment_plan"] == {
        "source": "deterministic_base_transformed",
        "planner_repaired": False,
        "ignored_transformations": [],
        "unassigned_obligation_ids": [],
        "unassigned_obligation_reasons": {},
    }
    assert [item["id"] for item in result.artifact["assignments"]] == [
        "fallback-combined-1",
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
    assert "`src/main/java/example/api/OrderController.java` changes " in handoff
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
    assert failures["planner_repair"]["repair_requests"] == 0
    assert failures["planner_repair"]["source"] == "deterministic_base_transformed"
    assert failures["failed_critic"]["terminal"] is True
    assert failures["failed_critic"]["fallback"] == "conservative"
    assert failures["deadline_cutoff"]["deadline_violation"] is False
    assert failures["deadline_cutoff"]["finalization_reserved"] is True
    assert failures["deadline_cutoff"]["cutoff_enforced"] is True
    assert failures["deadline_cutoff"]["terminal"] is True
    assert failures["deadline_cutoff"]["provider_turns_consumed"] == 2
    assert failures["completion_inversion"]["stable_projection"] is True
    assert failures["completion_inversion"]["coverage_stable"] is True
    assert failures["completion_inversion"]["evidence_stable"] is True
    assert failures["completion_inversion"]["orders_enforced"] is True
    assert failures["completion_inversion"]["terminal"] is True
    assert failures["completion_inversion"]["controller_runs"] == 2
    assert failures["note_anchor_race"] == {
        "stable": True,
        "anchor_types": ["file", "line"],
    }


def test_web_policy_replay_keeps_discovery_non_evidentiary_and_denies_redirect_escape():
    result = replay_web_policy_fixture(FIXTURES / "web-policy-pr")
    serialized = json.dumps({
        key: value for key, value in result.items() if key != "expected"
    }, sort_keys=True)

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


def test_provider_fixture_contains_only_explicit_openai_responses():
    provider = json.loads(
        (FIXTURES / "provider-turns.json").read_text(encoding="utf-8")
    )
    scenario = provider["scenarios"]["multilingual"]

    assert scenario["request_order"] == [
        "planner-initial",
        "specialist-tools",
        "specialist-checkpoint",
        "specialist-final",
        "critic",
        "finalizer",
    ]
    assert set(scenario["responses"]) == set(scenario["request_order"])
    for turn_id, turn in scenario["responses"].items():
        assert set(turn) == {"expect", "response"}, turn_id
        assert turn["expect"]["role"] in {
            "planner", "specialist", "critic", "finalizer",
        }
        assert isinstance(turn["expect"]["tools_enabled"], bool)
        assert "choices" in turn["response"] or "error" in turn["response"]
    for scenario_id in (
        "no_progress_resume", "reconstruction", "deadline_cutoff",
    ):
        for turn in provider["scenarios"][scenario_id]["responses"]:
            assert set(turn) == {"expect", "response"}, scenario_id
            assert "choices" in turn["response"] or "error" in turn["response"]
    completion = provider["scenarios"]["completion_inversion"]
    assert len(completion["assignments"]) == 2
    assert set(completion["session_responses"]) == {
        "assignment-a", "assignment-b",
    }


def test_recorded_candidate_is_collected_by_session_not_review_inputs(tmp_path):
    copied = tmp_path / "specialist_runtime"
    shutil.copytree(FIXTURES, copied)
    provider_path = copied / "provider-turns.json"
    provider = json.loads(provider_path.read_text(encoding="utf-8"))
    checkpoint = provider["scenarios"]["multilingual"]["responses"][
        "specialist-checkpoint"
    ]["response"]["choices"][0]["message"]
    payload = json.loads(checkpoint["content"])
    payload["candidate_findings"] = []
    payload["candidate_finding_ids"] = []
    checkpoint["content"] = json.dumps(payload, sort_keys=True)
    final = provider["scenarios"]["multilingual"]["responses"][
        "specialist-final"
    ]["response"]["choices"][0]["message"]
    final_payload = json.loads(final["content"])
    final_payload["candidate_finding_ids"] = []
    final["content"] = json.dumps(final_payload, sort_keys=True)
    provider["scenarios"]["multilingual"]["request_order"].remove("critic")
    provider["scenarios"]["multilingual"]["responses"].pop("critic")
    provider_path.write_text(json.dumps(provider), encoding="utf-8")

    replay = replay_fixture(copied / "multilingual-pr")
    metrics = evaluate_specialist_replay(
        replay.artifact,
        replay.expected,
        notes=replay.notes,
        observed=replay.observed,
        adversarial_cases=replay.failures,
    )

    assert replay.artifact["accepted_candidates"] == []
    assert "missing_expected_finding" not in metrics["failure_gates"]


def test_replay_rejects_wrong_recorded_request_shape(tmp_path):
    copied = tmp_path / "specialist_runtime"
    shutil.copytree(FIXTURES, copied)
    provider_path = copied / "provider-turns.json"
    provider = json.loads(provider_path.read_text(encoding="utf-8"))
    provider["scenarios"]["multilingual"]["responses"]["planner-initial"][
        "expect"
    ]["role"] = "critic"
    provider_path.write_text(json.dumps(provider), encoding="utf-8")

    with pytest.raises(AssertionError, match="recorded request"):
        replay_fixture(copied / "multilingual-pr")


def test_replay_rejects_wrong_recorded_turn_shape(tmp_path):
    copied = tmp_path / "specialist_runtime"
    shutil.copytree(FIXTURES, copied)
    provider_path = copied / "provider-turns.json"
    provider = json.loads(provider_path.read_text(encoding="utf-8"))
    provider["scenarios"]["multilingual"]["responses"]["specialist-checkpoint"][
        "response"
    ]["choices"][0]["message"]["content"] = "{}"
    provider_path.write_text(json.dumps(provider), encoding="utf-8")

    with pytest.raises(AssertionError, match="recorded request|recorded turns"):
        replay_fixture(copied / "multilingual-pr")


@pytest.mark.parametrize("surface", ["handoff", "note", "accepted_finding"])
def test_eval_derives_unsupported_claims_from_every_public_surface(surface):
    replay = replay_fixture(FIXTURES / "multilingual-pr")
    artifact = deepcopy(replay.artifact)
    notes = list(replay.notes)
    novel = "NOVEL-UNSUPPORTED-PUBLIC-CLAIM"
    if surface == "handoff":
        artifact["handoff"]["markdown"] += f"\n{novel}\n"
    elif surface == "note":
        notes[0] = replace(notes[0], markdown=notes[0].markdown + f"\n{novel}")
    else:
        artifact["accepted_candidates"].append({
            "candidate_id": "unsupported",
            "claim": novel,
            "supporting_evidence_ids": [],
            "supporting_citations": [],
            "related_obligation_ids": [],
        })

    metrics = evaluate_specialist_replay(
        artifact,
        replay.expected,
        notes=notes,
        observed={"unsupported_public_claims": []},
        adversarial_cases=replay.failures,
    )

    if surface == "note":
        # Verification notes are explicitly non-factual and are not evaluated
        # as published finding claims.
        assert "unsupported_public_claim" not in metrics["failure_gates"]
        assert metrics["unsupported_claims"] == []
    else:
        assert "unsupported_public_claim" in metrics["failure_gates"]
        assert novel in json.dumps(metrics["unsupported_claims"])


def test_eval_ignores_caller_supplied_unsupported_claim_flags():
    replay = replay_fixture(FIXTURES / "multilingual-pr")

    metrics = evaluate_specialist_replay(
        replay.artifact,
        replay.expected,
        notes=replay.notes,
        observed={"unsupported_public_claims": ["caller-controlled"]},
        adversarial_cases=replay.failures,
    )

    assert "unsupported_public_claim" not in metrics["failure_gates"]


@pytest.mark.parametrize(
    "mutation",
    [
        "source_request_value",
        "source_request_count",
        "aggregate_theme_value",
        "aggregate_theme_count",
        "thread_status",
        "coverage_warning",
        "change_map",
        "review_emphasis",
        "specialist_focus",
        "recipe_focus",
        "coverage_boundary",
    ],
)
def test_eval_rejects_forged_structured_handoff_lines(mutation):
    replay = replay_fixture(FIXTURES / "multilingual-pr")
    artifact = deepcopy(replay.artifact)
    handoff = artifact["handoff"]
    novel = "NOVEL-UNSUPPORTED-PUBLIC-CLAIM"

    if mutation == "source_request_value":
        handoff["access_request_count"] = 1
        handoff["markdown"] += f"\n**Source access requests:** {novel}\n"
    elif mutation == "source_request_count":
        handoff["access_request_count"] = 1
        handoff["markdown"] += "\n**Source access requests:** 1 open\n"
    elif mutation == "aggregate_theme_value":
        handoff["finding_theme"] = "database"
        handoff["markdown"] += f"\n**Aggregate finding theme:** {novel}\n"
    elif mutation == "aggregate_theme_count":
        handoff["finding_theme"] = "database"
        handoff["markdown"] += (
            "\n**Aggregate finding theme:** Database and persistence\n"
        )
    elif mutation == "thread_status":
        original = f"**Prepared detail notes:** {handoff['thread_status']}"
        handoff["thread_status"] = novel
        handoff["markdown"] = handoff["markdown"].replace(
            original, f"**Prepared detail notes:** {novel}",
        )
    elif mutation == "coverage_warning":
        original = f"**Material coverage warning:** {handoff['coverage_warning']}"
        handoff["coverage_warning"] = novel
        handoff["markdown"] = handoff["markdown"].replace(
            original, f"**Material coverage warning:** {novel}",
        )
    elif mutation == "change_map":
        handoff["change_map"].append(novel)
        handoff["markdown"] = handoff["markdown"].replace(
            "### Change map\n\n", f"### Change map\n\n- {novel}\n",
        )
    elif mutation == "review_emphasis":
        handoff["review_emphasis"].append(novel)
        handoff["markdown"] = handoff["markdown"].replace(
            "These focus suggestions",
            f"### Human review focus\n\n- {novel}\n\nThese focus suggestions",
        )
    elif mutation == "specialist_focus":
        handoff["reviewed_focuses"].append(novel)
        handoff["specialist_focuses"].append(novel)
        handoff["markdown"] = handoff["markdown"].replace(
            "### AI focus and coverage\n\n",
            f"### AI focus and coverage\n\n- Specialist focus: {novel}\n",
        )
    elif mutation == "recipe_focus":
        recipe = f"Repository recipe: {novel.casefold()}"
        original = "- Repository recipes: " + "; ".join(handoff["recipe_focuses"])
        handoff["reviewed_focuses"].append(recipe)
        handoff["recipe_focuses"].append(recipe)
        replacement = "- Repository recipes: " + "; ".join(
            sorted(handoff["recipe_focuses"])
        )
        handoff["markdown"] = handoff["markdown"].replace(original, replacement)
    else:
        handoff["reviewed_focuses"].append(novel)
        handoff["coverage_boundaries"].append(novel)
        handoff["markdown"] = handoff["markdown"].replace(
            "### AI focus and coverage\n\n",
            f"### AI focus and coverage\n\n- Coverage boundaries: {novel}\n",
        )

    metrics = evaluate_specialist_replay(
        artifact,
        replay.expected,
        notes=replay.notes,
        observed=replay.observed,
        adversarial_cases=replay.failures,
    )

    assert "unsupported_public_claim" in metrics["failure_gates"]


def test_eval_accepts_source_request_count_derived_from_authoritative_requests():
    replay = replay_fixture(FIXTURES / "multilingual-pr")
    artifact = deepcopy(replay.artifact)
    obligation_id = replay.expected["mandatory_obligation_ids"][0]
    artifact["source_access_requests"] = [{
        "host": "docs.example.org",
        "candidate_url": "https://docs.example.org/reference/runtime",
        "obligation_id": obligation_id,
        "purpose": "Confirm the documented runtime contract.",
        "authority_reason": "Repository policy requires human authorization.",
    }]
    artifact["handoff"]["access_request_count"] = 1
    artifact["handoff"]["markdown"] = (
        artifact["handoff"]["markdown"].rstrip()
        + "\n\n**Source access requests:** 1 open\n"
    )

    metrics = evaluate_specialist_replay(
        artifact,
        replay.expected,
        notes=replay.notes,
        observed=replay.observed,
        adversarial_cases=replay.failures,
    )

    assert "unsupported_public_claim" not in metrics["failure_gates"]


def test_eval_accepts_production_capped_normalized_and_filtered_handoff():
    source_request = SourceAccessRequest(
        host="docs.example.org",
        candidate_url="https://docs.example.org/reference/runtime",
        obligation_id="missing-obligation",
        purpose="Security-sensitive behavior",
        authority_reason="Deployment and runtime configuration",
    )
    topics = (
        ReviewOrientationTopic.DATABASE,
        ReviewOrientationTopic.AUTHORIZATION,
        ReviewOrientationTopic.CACHING,
        ReviewOrientationTopic.CONCURRENCY,
        ReviewOrientationTopic.API_CONTRACTS,
        ReviewOrientationTopic.FAILURE_RECOVERY,
        ReviewOrientationTopic.DEPLOYMENT,
        ReviewOrientationTopic.SECURITY,
    )
    context = ReviewHandoffContext(
        recommendation="approve",
        status="complete",
        change_topics=topics,
        component_ids=(
            "  ZETA  ",
            "alpha",
            "bravo",
            "charlie",
            "delta",
            "echo",
            "foxtrot",
            "docs.example.org",
        ),
        specialist_topics=topics,
        recipe_ids=(
            "  RECIPE-Z  ",
            "recipe-a",
            "recipe-b",
            "recipe-c",
            "recipe-d",
            "recipe-e",
            "recipe-f",
            "docs.example.org",
        ),
        coverage_boundary_topics=topics,
        review_emphasis_topics=topics,
        source_access_requests=(source_request,),
    )
    handoff = build_review_handoff(
        context,
        review=AdjudicatedReview(),
        evidence=EvidenceStore(),
        obligations={},
        changed_files=(),
    )
    artifact = {
        "accepted_candidates": [],
        "coverage": {},
        "degradation": [],
        "evaluation_status": "complete",
        "events": [{
            "kind": "finalizer_proposal_applied",
            "payload": {
                "change_topics": [item.value for item in context.change_topics],
                "component_ids": list(context.component_ids),
                "specialist_topics": [
                    item.value for item in context.specialist_topics
                ],
                "recipe_ids": list(context.recipe_ids),
                "coverage_boundary_topics": [
                    item.value for item in context.coverage_boundary_topics
                ],
                "review_emphasis_topics": [
                    item.value for item in context.review_emphasis_topics
                ],
            },
        }],
        "handoff": asdict(handoff),
        "source_access_requests": [asdict(source_request)],
        "verdict": {"value": "approve"},
    }

    assert len(handoff.change_map) == 12
    assert handoff.recipe_focuses == (
        "Repository recipe: recipe-a",
        "Repository recipe: recipe-b",
        "Repository recipe: recipe-c",
        "Repository recipe: recipe-d",
        "Repository recipe: recipe-e",
        "Repository recipe: recipe-f",
    )
    assert eval_harness_module._unsupported_handoff_lines(artifact) == []


def test_eval_validates_prepared_notes_without_inferring_thread_state():
    context = ReviewHandoffContext(
        recommendation="request_changes",
        status="complete",
        unresolved_thread_count=2,
        highest_thread_severity="major",
    )
    handoff = build_review_handoff(
        context,
        review=AdjudicatedReview(),
        evidence=EvidenceStore(),
        obligations={},
        changed_files=(),
    )
    artifact = {
        "accepted_candidates": [{"severity": "major"}],
        "coverage": {},
        "degradation": [],
        "evaluation_status": "complete",
        "events": [],
        "handoff": asdict(handoff),
        "notes": [
            {"kind": "finding", "fingerprint": "finding:one"},
            {
                "kind": "verification_request",
                "fingerprint": "verification_request:two",
            },
        ],
        "verdict": {"value": "request_changes"},
    }

    assert eval_harness_module._unsupported_handoff_lines(artifact) == []
    assert "unresolved" not in handoff.markdown.casefold()
    assert "thread status" not in handoff.markdown.casefold()


def test_false_adversarial_predicate_is_a_mandatory_gate():
    replay = replay_fixture(FIXTURES / "multilingual-pr")
    adversarial = deepcopy(replay.failures)
    adversarial["completion_inversion"]["coverage_stable"] = False

    metrics = evaluate_specialist_replay(
        replay.artifact,
        replay.expected,
        notes=replay.notes,
        observed=replay.observed,
        adversarial_cases=adversarial,
    )

    assert "adversarial_failure" in metrics["failure_gates"]
    assert metrics["adversarial"]["failed"] == [
        "completion_inversion.coverage_stable",
    ]


@pytest.mark.parametrize(
    ("mutation", "gate"),
    [
        ({"drop_mandatory_status": True}, "missing_mandatory_status"),
        ({"unsafe_fetch_attempts": 1}, "unsafe_fetch"),
        ({"budget_history": {"session:x": [3, 2]}}, "budget_reset"),
        ({"elapsed_simulated_sec": 301}, "deadline_violation"),
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
        adversarial_cases=replay.failures,
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

    assert corpus.offline_specialist_replays == [
        {
            "id": "multilingual-specialist-runtime",
            "kind": "runtime",
            "fixture": "../tests/fixtures/specialist_runtime/multilingual-pr",
        },
        {
            "id": "web-source-policy",
            "kind": "web_policy",
            "fixture": "../tests/fixtures/specialist_runtime/web-policy-pr",
        },
    ]


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
    web = report["offline_specialist_replays"][1]
    assert web["id"] == "web-source-policy"
    assert web["passed"] is True
    assert web["metrics"]["sources"] == {
        "approved_fetches": 1,
        "denials": 2,
        "requests": 1,
        "unsafe_fetch_attempts": 0,
    }


def test_offline_cli_exits_nonzero_for_false_adversarial_predicate(
    monkeypatch, tmp_path,
):
    original = eval_harness_module.replay_fixture

    def mutated_replay(path):
        replay = original(path)
        failures = deepcopy(replay.failures)
        failures["completion_inversion"]["evidence_stable"] = False
        return replace(replay, failures=failures)

    output = tmp_path / "adversarial-failure.json"
    monkeypatch.setattr(
        eval_harness_module, "replay_fixture", mutated_replay,
    )
    monkeypatch.setattr(sys, "argv", [
        "eval_harness.py",
        "--corpus", str(ROOT / "evals" / "corpus-agentic.json"),
        "--offline-specialist-only",
        "--output", str(output),
    ])

    assert eval_harness_module.main() == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    runtime = report["offline_specialist_replays"][0]
    assert runtime["passed"] is False
    assert "adversarial_failure" in runtime["failure_gates"]


def test_offline_cli_exits_nonzero_for_measured_unsafe_web_fetch(
    monkeypatch, tmp_path,
):
    original = eval_harness_module.replay_web_policy_fixture

    def mutated_web(path):
        result = original(path)
        return {**result, "unsafe_fetch_attempts": 1}

    output = tmp_path / "web-failure.json"
    monkeypatch.setattr(
        eval_harness_module, "replay_web_policy_fixture", mutated_web,
    )
    monkeypatch.setattr(sys, "argv", [
        "eval_harness.py",
        "--corpus", str(ROOT / "evals" / "corpus-agentic.json"),
        "--offline-specialist-only",
        "--output", str(output),
    ])

    assert eval_harness_module.main() == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    web = report["offline_specialist_replays"][1]
    assert web["passed"] is False
    assert "unsafe_fetch" in web["failure_gates"]
