import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "run_specialist_reviews.py"
spec = importlib.util.spec_from_file_location("run_specialist_reviews", SCRIPT)
runner_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(runner_module)


class ScriptedRunner:
    def __init__(self):
        self.calls = []

    def agent(self, role, system, user, max_tools, terminal_instruction):
        self.calls.append((role, max_tools, user))
        if len(self.calls) == 1:
            return {
                "completion_status": "complete",
                "inspected_files": ["invented.py"],
                "invariants_checked": [],
                "findings": [{
                    "severity": "major", "category": "bug", "file": "a/a.py", "line": 1,
                    "claim": "first", "evidence": ["proof"], "causal_chain": "x -> y",
                }],
            }, {"tool_calls_executed": 1, "inspected_files": ["a/a.py"]}
        return {
            "completion_status": "complete",
            "inspected_files": ["b/b.py"],
            "invariants_checked": ["identity"],
            "findings": [],
        }, {"tool_calls_executed": 1, "inspected_files": ["b/b.py"]}


def test_run_focus_uses_actual_reads_continues_and_preserves_prior_findings(monkeypatch):
    monkeypatch.setattr(runner_module, "focus_prompt", lambda *args, **kwargs: "prompt")
    focus = {
        "id": "flow", "title": "Flow", "objective": "Trace flow",
        "lenses": ["interaction-data-flow"], "seed_paths": ["a/**", "b/**"],
        "related_paths": [], "related_symbols": [], "invariants": ["identity"],
        "expected_evidence": [], "priority": "high", "rationale": "", "source": "planner",
    }
    topology = {
        "changed_files": ["a/a.py", "b/b.py"],
        "path_components": {"a/a.py": "a", "b/b.py": "b"},
    }
    scripted = ScriptedRunner()
    report, diagnostics = runner_module.run_focus(scripted, focus, topology, 5, "native_loop")
    assert len(scripted.calls) == 2
    assert scripted.calls[1][1] == 4
    assert report["inspected_files"] == ["a/a.py", "b/b.py"]
    assert [item["claim"] for item in report["findings"]] == ["first"]
    assert report["completion_status"] == "complete"
    assert len(diagnostics) == 2


def test_packet_mode_does_not_claim_tool_inspection(monkeypatch):
    class PacketRunner:
        def one_shot(self, *args, **kwargs):
            return {
                "completion_status": "complete", "inspected_files": ["a.py"],
                "invariants_checked": [], "findings": [],
            }, {"text_source": "content"}

    monkeypatch.setattr(runner_module, "focus_prompt", lambda *args, **kwargs: "packet")
    focus = {
        "id": "x", "title": "X", "objective": "Review", "lenses": [],
        "seed_paths": ["a.py"], "related_paths": [], "related_symbols": [],
        "invariants": [], "expected_evidence": [], "priority": "normal",
        "rationale": "", "source": "planner",
    }
    report, _ = runner_module.run_focus(
        PacketRunner(), focus, {"changed_files": ["a.py"], "path_components": {"a.py": "repository"}},
        5, "packet",
    )
    assert report["inspected_files"] == ["a.py"]


def test_final_critic_cannot_schedule_followups():
    result = runner_module.normalize_critic({
        "dispositions": [],
        "followup_focuses": [{"id": "more", "title": "More", "objective": "More"}],
    }, allow_followups=False)
    assert result["followup_focuses"] == []


def test_render_review_uses_only_validated_candidates_and_deterministic_verdict():
    candidates = [{
        "candidate_id": "C1", "severity": "major", "category": "bug",
        "file": "a.py", "line": 3, "claim": "A concrete defect",
        "evidence": ["a.py:3 demonstrates it"], "causal_chain": "x -> y",
        "inline_eligible": True,
    }]
    review = runner_module.render_review(candidates, ["UNKNOWN", "C1"], "")
    assert review["verdict"] == "request_changes"
    assert "A concrete defect" in review["review_markdown"]
    assert "UNKNOWN" not in review["review_markdown"]
    assert [item["message"] for item in review["findings"]] == ["A concrete defect"]


def test_action_evaluation_strategy_is_publish_gated():
    action = (Path(__file__).parents[1] / "action.yml").read_text(encoding="utf-8")
    assert "inputs.review_strategy != 'specialists_evaluate'" in action
    assert 'default: "single"' in action


def test_specialist_config_must_stay_in_repository(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert runner_module.safe_repo_file(".github/review.json") == ".github/review.json"
    try:
        runner_module.safe_repo_file("../secret.json")
    except ValueError as exc:
        assert "inside the reviewed repository" in str(exc)
    else:
        raise AssertionError("path traversal was accepted")


def test_single_strategy_guard_remains_in_review_script():
    review = (Path(__file__).parents[1] / "scripts" / "sections" / "review.sh").read_text(encoding="utf-8")
    assert '[[ "$REVIEW_STRATEGY" == "single" ]] || return 0' in review
    assert 'SPECIALIST_EVALUATION_STATUS=' in review
    assert '"$SPECIALIST_EVALUATION_STATUS" == "complete"' in review
    assert '"$REVIEW_STRATEGY" == "specialists_evaluate" && "$SPECIALIST_EVALUATION_STATUS" == "failed"' in review
    corpus = (Path(__file__).parents[1] / "scripts" / "sections" / "corpus.sh").read_text(encoding="utf-8")
    assert 'IS_FORK_PR="$IS_FORK_PR" python3 "$SCRIPT_DIR/run_specialist_reviews.py"' in corpus


class EndToEndRunner:
    last = None

    def __init__(self):
        type(self).last = self
        self.requests = []
        self.roles = []

    def agent(self, role, system, user, max_tools, terminal_instruction):
        self.roles.append(role)
        if role == "planner":
            return {
                "summary": "one component",
                "focuses": [{
                    "id": "dynamic-worker-focus", "title": "Dynamic worker focus",
                    "objective": "Review the changed worker", "seed_paths": ["a.py"],
                    "lenses": ["component-correctness"], "priority": "high",
                }],
            }, {"tool_calls_executed": 0, "inspected_files": []}
        return {
            "completion_status": "complete", "inspected_files": ["a.py"],
            "invariants_checked": [], "unchecked_material_files": [],
            "findings": [{
                "severity": "major", "category": "bug", "file": "a.py", "line": 2,
                "claim": "Changed value is ignored", "evidence": ["a.py:2 ignores it"],
                "causal_chain": "new input -> ignored assignment -> stale result",
            }], "unknowns": [],
        }, {"tool_calls_executed": 1, "inspected_files": ["a.py"]}

    def one_shot(self, role, system, user, max_tokens=None):
        self.roles.append(role)
        if role == "critic":
            return {"dispositions": [], "followup_focuses": [], "coverage_notes": []}, {}
        assert role == "aggregator"
        return {"verdict": "request_changes", "ordered_finding_ids": ["C1"]}, {}


def _write_minimal_review_workspace(tmp_path):
    (tmp_path / "pr-files.json").write_text('[{"filename":"a.py"}]', encoding="utf-8")
    (tmp_path / "classification.json").write_text('{"pr_kind":"app_code","risk_flags":[]}', encoding="utf-8")
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,1 +1,2 @@\n old\n+ignored = value\n"
    (tmp_path / "pr.diff").write_text(diff, encoding="utf-8")
    (tmp_path / "pr.diff.truncated").write_text(diff, encoding="utf-8")
    (tmp_path / "review-corpus.truncated.md").write_text("# PR\nchanged a.py", encoding="utf-8")


def test_main_runs_dynamic_plan_specialists_two_critic_turns_and_aggregator(monkeypatch, tmp_path):
    _write_minimal_review_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REVIEW_STRATEGY", "specialists_evaluate")
    monkeypatch.setenv("AI_MODEL", "test-model")
    monkeypatch.setenv("AI_BASE_URL", "http://unused/v1")
    monkeypatch.setattr(runner_module, "SequentialModelRunner", EndToEndRunner)
    monkeypatch.setattr(runner_module, "tracked_paths", lambda: ["pyproject.toml", "a.py"])

    assert runner_module.main() == 0
    output = __import__("json").loads((tmp_path / "specialist-ai-output.json").read_text())
    artifact = __import__("json").loads((tmp_path / "specialist-review-artifact.json").read_text())
    assert output["verdict"] == "request_changes"
    assert output["findings"][0]["message"] == "Changed value is ignored"
    assert "Specialist coverage: 1 pass(es) succeeded, 0 failed" in output["review_markdown"]
    assert artifact["strategy"] == "specialists_evaluate"
    assert artifact["evaluation_status"] == "complete"
    assert all(
        "deterministic" not in item.get("sources", [item.get("source")])
        for item in artifact["schedule"]["selected"]
    )
    assert EndToEndRunner.last.roles == ["planner", "specialist", "critic", "critic", "aggregator"]


def test_planner_fallback_focuses_only_when_plan_is_degraded():
    topology = {
        "changed_files": ["a.py"],
        "components": [{"id": "repository", "changed_files": ["a.py"],
                        "file_roles": [], "invariants": []}],
        "path_components": {"a.py": "repository"},
        "relationships": [], "risk_flags": [],
    }
    planner_focuses = [{"id": "planned"}]

    assert runner_module.initial_fallback_focuses(
        planner_focuses, planner_degraded=False, topology=topology
    ) == []
    assert runner_module.initial_fallback_focuses(
        [], planner_degraded=True, topology=topology
    )[0]["source"] == "deterministic"


def test_main_planner_failure_uses_deterministic_fallback(monkeypatch, tmp_path):
    _write_minimal_review_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REVIEW_STRATEGY", "specialists_evaluate")
    monkeypatch.setenv("AI_MODEL", "test-model")
    monkeypatch.setenv("AI_BASE_URL", "http://unused/v1")

    class FailingPlanner(EndToEndRunner):
        def agent(self, role, system, user, max_tools, terminal_instruction):
            if role == "planner":
                self.roles.append(role)
                raise ValueError("invalid planner JSON")
            return super().agent(role, system, user, max_tools, terminal_instruction)

    monkeypatch.setattr(runner_module, "SequentialModelRunner", FailingPlanner)
    monkeypatch.setattr(runner_module, "tracked_paths", lambda: ["pyproject.toml", "a.py"])
    assert runner_module.main() == 0
    artifact = __import__("json").loads((tmp_path / "specialist-review-artifact.json").read_text())
    assert artifact["planner"]["degraded"] is True
    assert artifact["schedule"]["selected"][0]["source"] == "deterministic"


def _runner_env(monkeypatch, *, response_format="json_schema", api_format="openai"):
    monkeypatch.setenv("AI_BASE_URL", "http://model.test/v1")
    monkeypatch.setenv("AI_MODEL", "model")
    monkeypatch.setenv("AI_API_FORMAT", api_format)
    monkeypatch.setenv("AI_RESPONSE_FORMAT", response_format)
    monkeypatch.setenv("AI_STREAM", "false")


@pytest.mark.parametrize("role", ["planner", "specialist", "critic", "aggregator"])
def test_role_specific_json_schemas_are_used(monkeypatch, role):
    _runner_env(monkeypatch)
    captured = []

    def fake_request(_base, api_format, payload, _key, _timeout):
        captured.append(payload)
        assert api_format == "openai"
        return {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(runner_module, "run_chat_request", fake_request)
    runner_module.SequentialModelRunner().one_shot(role, "system", "user")
    formatter = captured[0]["response_format"]
    assert formatter["type"] == "json_schema"
    assert formatter["json_schema"]["name"] == f"specialist_{role}"
    assert formatter["json_schema"]["schema"] == runner_module.ROLE_SCHEMAS[role]


@pytest.mark.parametrize("configured,expected", [("json_object", "json_object"), ("off", None)])
def test_configured_openai_response_format(monkeypatch, configured, expected):
    _runner_env(monkeypatch, response_format=configured)
    captured = []
    monkeypatch.setattr(
        runner_module, "run_chat_request",
        lambda _b, _a, payload, _k, _t: captured.append(payload) or
        {"choices": [{"message": {"content": "{}"}}]},
    )
    runner_module.SequentialModelRunner().one_shot("aggregator", "system", "user")
    if expected:
        assert captured[0]["response_format"] == {"type": expected}
    else:
        assert "response_format" not in captured[0]


def test_anthropic_omits_openai_response_format(monkeypatch):
    _runner_env(monkeypatch, api_format="anthropic")
    captured = []
    monkeypatch.setattr(
        runner_module, "run_chat_request",
        lambda _b, _a, payload, _k, _t: captured.append(payload) or
        {"content": [{"type": "text", "text": "{}"}], "stop_reason": "end_turn"},
    )
    runner_module.SequentialModelRunner().one_shot("critic", "system", "user")
    assert "response_format" not in captured[0]


def test_structured_rejection_retries_once_without_format(monkeypatch):
    _runner_env(monkeypatch)
    calls = []

    def fake_request(_base, _api, payload, _key, _timeout):
        calls.append(payload)
        if len(calls) == 1:
            raise RuntimeError("HTTP 400: unsupported response_format Bearer abcdefghijklmnopqrstuvwxyz123456")
        return {"choices": [{"message": {"content": '{"ordered_finding_ids":[],"verdict":"approve"}'}}]}

    monkeypatch.setattr(runner_module, "run_chat_request", fake_request)
    raw, diagnostics = runner_module.SequentialModelRunner().one_shot(
        "aggregator", "system", "JSON only"
    )
    assert raw["verdict"] == "approve"
    assert len(calls) == 2
    assert "response_format" in calls[0] and "response_format" not in calls[1]
    assert diagnostics["request"]["structured_output_fallback"] is True
    assert "abcdefghijklmnopqrstuvwxyz123456" not in diagnostics["request"]["structured_output_error"]


def test_both_structured_and_unstructured_failures_keep_redacted_diagnostics(monkeypatch):
    _runner_env(monkeypatch)
    calls = []

    def fail(_base, _api, payload, _key, _timeout):
        calls.append(payload)
        if "response_format" in payload:
            raise RuntimeError("HTTP 400 Bearer abcdefghijklmnopqrstuvwxyz123456")
        raise RuntimeError("fallback parse transport failure")

    monkeypatch.setattr(runner_module, "run_chat_request", fail)
    with pytest.raises(RuntimeError) as raised:
        runner_module.SequentialModelRunner().one_shot("critic", "system", "user")
    assert len(calls) == 2
    assert "structured output request failed" in str(raised.value)
    assert "unstructured fallback failed" in str(raised.value)
    assert "abcdefghijklmnopqrstuvwxyz123456" not in str(raised.value)


def test_verdict_reasoning_effort_is_separate(monkeypatch):
    _runner_env(monkeypatch, response_format="off")
    monkeypatch.setenv("AI_REASONING_EFFORT", "high")
    monkeypatch.setenv("AI_VERDICT_REASONING_EFFORT", "none")
    captured = []
    monkeypatch.setattr(
        runner_module, "run_chat_request",
        lambda _b, _a, payload, _k, _t: captured.append(payload) or
        {"choices": [{"message": {"content": "{}"}}]},
    )
    runner = runner_module.SequentialModelRunner()
    runner.one_shot("planner", "system", "user")
    assert runner.reasoning_effort == "high"
    assert captured[0]["reasoning_effort"] == "none"


def test_truncated_specialist_analysis_continues_before_terminal_synthesis(monkeypatch):
    _runner_env(monkeypatch)
    captured = []
    report = {
        "domain": "focus", "completion_status": "complete",
        "inspected_files": [], "unchecked_material_files": [],
        "invariants_checked": ["identity"], "findings": [], "unknowns": [],
    }

    def fake_request(_base, _api, payload, _key, _timeout):
        captured.append(payload)
        if len(captured) == 1:
            return {"choices": [{"finish_reason": "length", "message": {
                "content": "Partial analysis found an identity mismatch but ended mid-sentence"
            }}]}
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(report)
        }}]}

    monkeypatch.setattr(runner_module, "run_chat_request", fake_request)
    value, diagnostics = runner_module.SequentialModelRunner().agent(
        "specialist", "system", "assigned focus", 2,
        terminal_instruction="Return strict JSON now.",
    )
    assert value == report
    assert len(captured) == 2
    assert captured[0]["max_tokens"] == captured[1]["max_tokens"]
    assert diagnostics["preserved_truncated_bytes"] > 0
    assert diagnostics["continuation_attempts"] == 1
    assert diagnostics["had_truncated_turn"] is True
    assert diagnostics["terminal_synthesis_attempted"] is False
    assert diagnostics["terminal_synthesis_recovered"] is False


def test_parseable_length_limited_report_is_still_recovered(monkeypatch):
    _runner_env(monkeypatch)
    captured = []
    partial = {
        "domain": "focus", "completion_status": "complete",
        "inspected_files": ["a.py"], "unchecked_material_files": [],
        "invariants_checked": ["identity"], "findings": [], "unknowns": [],
    }
    recovered = {**partial, "unknowns": ["recovery retained the bounded analysis"]}

    def fake_request(_base, _api, payload, _key, _timeout):
        captured.append(payload)
        response = partial if len(captured) == 1 else recovered
        finish = "length" if len(captured) == 1 else "stop"
        return {"choices": [{"finish_reason": finish, "message": {
            "content": json.dumps(response)
        }}]}

    monkeypatch.setattr(runner_module, "run_chat_request", fake_request)
    value, diagnostics = runner_module.SequentialModelRunner().agent(
        "specialist", "system", "assigned focus", 2,
        terminal_instruction="Return strict JSON now.",
    )

    assert value == recovered
    assert len(captured) == 2
    continuation_messages = captured[1]["messages"]
    assert any(json.dumps(partial) in str(item.get("content"))
               for item in continuation_messages)
    assert captured[1]["tools"]
    assert diagnostics["had_truncated_turn"] is True
    assert diagnostics["turn_truncated"] is False
    assert diagnostics["terminal_synthesis_attempted"] is False
    assert diagnostics["terminal_synthesis_recovered"] is False


def test_repeated_truncation_terminal_synthesis_keeps_conversation_history(monkeypatch):
    _runner_env(monkeypatch)
    captured = []
    report = {
        "domain": "focus", "completion_status": "complete",
        "inspected_files": [], "unchecked_material_files": [],
        "invariants_checked": [], "findings": [], "unknowns": [],
    }

    def fake_request(_base, _api, payload, _key, _timeout):
        captured.append(payload)
        if len(captured) <= 2:
            return {"choices": [{"finish_reason": "length", "message": {
                "content": f"partial reasoning turn {len(captured)}"
            }}]}
        return {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(report)
        }}]}

    monkeypatch.setattr(runner_module, "run_chat_request", fake_request)
    value, diagnostics = runner_module.SequentialModelRunner().agent(
        "specialist", "system", "assigned focus", 2,
        terminal_instruction="Return strict JSON now.",
    )

    assert value == report
    assert len(captured) == 3
    terminal_messages = captured[2]["messages"]
    assert any("partial reasoning turn 1" in str(item.get("content"))
               for item in terminal_messages)
    assert any("partial reasoning turn 2" in str(item.get("content"))
               for item in terminal_messages)
    assert captured[2]["response_format"]["type"] == "json_schema"
    assert diagnostics["continuation_attempts"] == 1
    assert diagnostics["terminal_synthesis_attempted"] is True


@pytest.mark.parametrize(
    ("strategy", "fallback"),
    [("specialists", "standard_review"), ("specialists_evaluate", "publication_gated")],
)
def test_total_specialist_failure_never_writes_clean_output(monkeypatch, tmp_path, strategy, fallback):
    _write_minimal_review_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REVIEW_STRATEGY", strategy)
    monkeypatch.setenv("AI_MODEL", "test-model")
    monkeypatch.setenv("AI_BASE_URL", "http://unused/v1")

    class FailedPassRunner(EndToEndRunner):
        def agent(self, role, system, user, max_tools, terminal_instruction):
            if role == "planner":
                return super().agent(role, system, user, max_tools, terminal_instruction)
            raise RuntimeError("closing request failed")

    monkeypatch.setattr(runner_module, "SequentialModelRunner", FailedPassRunner)
    monkeypatch.setattr(runner_module, "tracked_paths", lambda: ["pyproject.toml", "a.py"])
    assert runner_module.main() == 0
    assert not (tmp_path / "specialist-ai-output.json").exists()
    artifact = json.loads((tmp_path / "specialist-review-artifact.json").read_text())
    assert artifact["evaluation_status"] == "failed"
    assert artifact["fallback_status"] == fallback
    assert artifact["pass_counts"]["succeeded"] == 0
    assert artifact["passes"][0]["status"] == "failed"


def test_focus_prompt_slices_topology_and_marks_truncation(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SPECIALIST_PACKET_MAX_BYTES", "1000")
    (tmp_path / "review-corpus.truncated.md").write_text("corpus " * 1000)
    (tmp_path / "pr.diff.truncated").write_text(
        "diff --git a/api/a.py b/api/a.py\n--- a/api/a.py\n+++ b/api/a.py\n"
        + ("+relevant change\n" * 200)
        + "diff --git a/unrelated/b.py b/unrelated/b.py\n--- a/unrelated/b.py\n+++ b/unrelated/b.py\n+noise\n"
    )
    focus = {"id": "api", "seed_paths": ["api/**"], "related_paths": [], "related_symbols": []}
    topology = {
        "changed_files": ["api/a.py", "unrelated/b.py"],
        "path_components": {"api/a.py": "api", "unrelated/b.py": "unrelated"},
        "components": [
            {"id": "api", "changed_files": ["api/a.py"]},
            {"id": "unrelated", "changed_files": ["unrelated/b.py"]},
        ],
        "relationships": [], "risk_flags": [], "pr_kind": "app_code",
    }
    prompt = runner_module.focus_prompt(focus, topology)
    assert "api/a.py" in prompt and "unrelated/b.py" not in prompt and "+noise" not in prompt
    assert "[review corpus truncated for this specialist]" in prompt
    assert len(prompt.encode()) <= 1000


def test_roster_ledger_and_guidance_are_bounded_structured_context(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pr.diff.truncated").write_text("", encoding="utf-8")
    (tmp_path / "standards-context.capped.md").write_text(
        "Generated OpenAPI outputs are intentionally gitignored; inspect the specification.",
        encoding="utf-8",
    )
    topology = {
        "changed_files": ["ui/a.ts", "api/a.py"],
        "path_components": {"ui/a.ts": "ui", "api/a.py": "api"},
        "components": [{"id": "ui"}, {"id": "api"}], "relationships": [],
        "risk_flags": [], "pr_kind": "app_code",
    }
    focuses = [
        {"id": "ui", "title": "UI", "seed_paths": ["ui/**"], "related_paths": [],
         "lenses": ["state-lifecycle-concurrency"], "invariants": ["requests reset"]},
        {"id": "api", "title": "API", "seed_paths": ["api/**"], "related_paths": [],
         "lenses": ["protocol-contract-compatibility"], "invariants": ["arguments agree"]},
    ]
    roster = runner_module.specialist_roster(focuses, topology)
    assert {item["id"] for item in roster} == {"ui", "api"}
    reports = [{"domain": "ui", "inspected_files": ["ui/a.ts"], "coverage_gaps": [],
                "findings": [{"file": "ui/a.ts", "line": 1, "claim": "race"}]}]
    passes = [{"focus": focuses[0], "status": "valid", "calls": [{"raw_reasoning": "secret"}]}]
    ledger = runner_module.build_coverage_ledger(reports, passes, topology)
    assert "raw_reasoning" not in json.dumps(ledger)
    prompt = runner_module.focus_prompt(focuses[1], topology, roster=roster, ledger=ledger)
    assert "Generated OpenAPI outputs" in prompt
    assert "Coworker roster" in prompt and "Provisional coverage ledger" in prompt


def test_critic_followup_reconfirmation_rejected_but_omitted_gap_accepted():
    topology = {
        "changed_files": ["api/a.py", "ui/a.ts"],
        "path_components": {"api/a.py": "api", "ui/a.ts": "ui"},
    }
    finding = {"file": "api/a.py", "line": 2, "claim": "authorization missing",
               "evidence": ["line"], "causal_chain": "request -> access"}
    reports = [{"domain": "api-auth", "completion_status": "complete",
                "coverage_gaps": [], "inspected_files": ["api/a.py"], "findings": [finding]}]
    schedule = {"omitted": [{"id": "ui-life", "lenses": ["state-lifecycle-concurrency"],
                              "seed_paths": ["ui/a.ts"], "related_paths": []}]}
    confirm = {"id": "confirm", "objective": "Reconfirm authorization", "rationale": "confirm",
               "seed_paths": ["api/a.py"], "related_paths": [], "lenses": ["trust-boundary-security"]}
    gap = {"id": "ui-gap", "objective": "Inspect omitted lifecycle", "rationale": "uncovered component",
           "seed_paths": ["ui/a.ts"], "related_paths": [], "lenses": ["state-lifecycle-concurrency"]}
    accepted, rejected = runner_module.filter_critic_followups(
        [confirm, gap], schedule, reports,
        [{"candidate_key": runner_module.candidate_key(finding), "decision": "keep"}], topology,
    )
    assert [item["id"] for item in accepted] == ["ui-gap"]
    assert rejected[0]["reason"] == "rechecks an already-kept supported candidate"


def test_unavailable_generated_read_returns_non_retryable_guidance(monkeypatch):
    _runner_env(monkeypatch)
    runner = runner_module.SequentialModelRunner()
    runner.generated_artifacts = [{
        "id": "client", "availability": "not-generated-in-review-workspace",
        "output_paths": ["target/generated-sources/**"],
        "source_of_truth": ["api/openapi.yaml"], "generator_config": ["pom.xml"],
    }]
    result = runner._execute("read_file", {"path": "target/generated-sources/Client.java"})
    assert result["status"] == "error"
    assert result["result"]["non_retryable"] is True
    assert "Repeated searches" in result["result"]["guidance"]
