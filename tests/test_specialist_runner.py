import importlib.util
from pathlib import Path


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
    assert '[[ "$REVIEW_STRATEGY" != "single" ]] && [ -s specialist-ai-output.json ]' in review
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
    assert artifact["strategy"] == "specialists_evaluate"
    assert EndToEndRunner.last.roles == ["planner", "specialist", "critic", "critic", "aggregator"]


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
