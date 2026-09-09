from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_specialist_runner_is_only_a_path_bootstrap_and_cli_wrapper():
    script = (ROOT / "scripts" / "run_specialist_reviews.py").read_text(encoding="utf-8")
    assert "from pr_reviewer.specialist_runtime.cli import main" in script
    assert "raise SystemExit(main())" in script
    assert "def main(" not in script
    assert len(script.splitlines()) <= 18


def test_specialist_result_bypasses_whole_pr_model_for_terminal_runtime_runs():
    review = (ROOT / "scripts" / "sections" / "review.sh").read_text(encoding="utf-8")
    assert '[[ "$SPECIALIST_EVALUATION_STATUS" == "complete" || "$SPECIALIST_EVALUATION_STATUS" == "degraded" || "$SPECIALIST_EVALUATION_STATUS" == "incomplete" ]]' in review
    assert "cp specialist-ai-output.json ai-output.json" in review


def test_corpus_runs_specialist_cli_and_keeps_single_tool_harness_path():
    corpus = (ROOT / "scripts" / "sections" / "corpus.sh").read_text(encoding="utf-8")
    assert 'python3 "$SCRIPT_DIR/run_specialist_reviews.py"' in corpus
    assert '"$SPECIALIST_PIPELINE_ENABLED" != "true"' in corpus
    assert "export REVIEW_STRATEGY REVIEW_POLICY_FILE SPECIALIST_REVIEW_DEADLINE_SEC" in corpus


def test_context_collects_complete_head_bound_snapshot_only_for_specialists():
    context = (ROOT / "scripts" / "sections" / "context.sh").read_text(encoding="utf-8")
    assert 'if [[ "$REVIEW_STRATEGY" != "single" ]]' in context
    assert 'platform_pr_files_all "$REPO" "$PR_NUMBER"' in context
    assert "pr-files-complete.json" in context
    assert "pr-files-head.txt" in context


def test_context_materializes_immutable_base_and_head_shas_for_specialists():
    context = (ROOT / "scripts" / "sections" / "context.sh").read_text(encoding="utf-8")
    assert "baseRefOid: .base.sha" in context
    assert "headRefOid: .head.sha" in context


def test_run_review_owns_specialist_artifact_workspace():
    script = (ROOT / "scripts" / "run_review.sh").read_text(encoding="utf-8")
    assert 'SPECIALIST_ARTIFACT_ROOT="$(pwd -P)"' in script
    assert "export SPECIALIST_ARTIFACT_ROOT" in script


def test_specialists_never_use_legacy_allowed_host_enrichment():
    enrichment = (ROOT / "scripts" / "sections" / "enrichment.sh").read_text(
        encoding="utf-8"
    )
    guard = enrichment.index('if [[ "$REVIEW_STRATEGY" != "single" ]]')
    legacy_fetch = enrichment.index('if [ -s urls.txt ]')
    assert guard < legacy_fetch
    assert "version-2 current-head source policy" in enrichment


def test_review_exports_structured_specialist_outputs():
    review = (ROOT / "scripts" / "sections" / "review.sh").read_text(encoding="utf-8")
    assert "review_handoff=$(pwd)/review-handoff.md" in review
    assert "review_notes=$(pwd)/review-notes.json" in review
    assert "specialist_artifact=$(pwd)/specialist-review-artifact.json" in review
