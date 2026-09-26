"""Contract checks for the downstream specialist-runtime migration handoff."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pr_reviewer.specialist_runtime.policy import load_review_policy

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "docs" / "migrations" / "specialist-session-runtime.md"


def parse_action_inputs_with_defaults() -> dict[str, str]:
    """Read action input defaults without requiring a YAML dependency in CI."""
    defaults: dict[str, str] = {}
    current: str | None = None
    for line in (ROOT / "action.yml").read_text(encoding="utf-8").splitlines():
        input_match = re.match(r"^  (\w+):\s*$", line)
        if input_match:
            current = input_match.group(1)
            continue
        default_match = re.match(r'^    default:\s*"?(.*?)"?\s*$', line)
        if current and default_match:
            defaults[current] = default_match.group(1).strip("'\"")
    return defaults


def parse_migration_input_table() -> dict[str, dict[str, str]]:
    """Parse the deliberately machine-readable migration input table."""
    rows: dict[str, dict[str, str]] = {}
    in_table = False
    for line in MIGRATION.read_text(encoding="utf-8").splitlines():
        if line == "<!-- specialist-runtime-input-table -->":
            in_table = True
            continue
        if line == "<!-- /specialist-runtime-input-table -->":
            break
        if not in_table or not line.startswith("|") or "---" in line:
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] == "Input":
            continue
        name = cells[0].strip("`")
        rows[name] = {"status": cells[1].lower(), "default": cells[2].strip("`")}
    return rows


def documented_v3_policy() -> dict[str, object]:
    """Return the complete version-3 JSON fence from the authoring guide."""
    text = (ROOT / "docs" / "review-policy-authoring.md").read_text(encoding="utf-8")
    match = re.search(
        r"## Complete version-3 policy example.*?```json\s*(\{.*?\})\s*```",
        text,
        flags=re.DOTALL,
    )
    assert match, "the policy-authoring guide must contain one version-3 policy JSON fence"
    return json.loads(match.group(1))


def test_migration_document_covers_required_repository_files():
    text = MIGRATION.read_text(encoding="utf-8")
    for required in (
        ".github/ai-review-rules.md",
        ".github/ai-review-specialists.json",
        ".github/ai-review-prompt.md",
        ".github/ai-review-policy.json",
        ".github/ai-review-diff-priorities.json",
        "review_policy_file",
        "review_diff_priority_file",
        "specialist_review_deadline_sec",
        "publish_mode",
    ):
        assert required in text


def test_documented_runtime_inputs_exist_with_matching_defaults():
    action = parse_action_inputs_with_defaults()
    table = parse_migration_input_table()
    assert table, "the migration handoff must contain its marked input table"
    for name, row in table.items():
        if row["status"] in {"added", "changed", "retained", "deprecated"}:
            assert action[name] == row["default"]


def test_inert_legacy_limits_are_explicitly_deprecated_while_live_limits_remain_retained():
    table = parse_migration_input_table()

    assert table["specialist_planner_max_context_bytes"]["status"] == "retained"
    assert table["specialist_recovery_max_tokens"]["status"] == "retained"
    for name in (
        "specialist_planner_max_tool_calls",
        "specialist_max_truncation_continuations",
        "specialist_packet_max_bytes",
    ):
        assert table[name]["status"] == "deprecated"

    text = MIGRATION.read_text(encoding="utf-8").lower()
    assert "initial planner model call is removed" in text
    assert "durable sessions do not issue truncation-continuation turns" in text


def test_migration_recommends_tool_capacity_for_multi_call_turns():
    table = parse_migration_input_table()
    text = MIGRATION.read_text(encoding="utf-8")

    assert table["specialist_max_tool_calls_per_session"]["default"] == "128"
    assert table["specialist_max_tool_calls_per_pass"]["default"] == "128"
    assert "specialist_max_tool_calls_per_session: \"128\"" in text
    assert "multi-call evidence turns" in text
    assert "controller-accounted" in text


def test_documented_v3_policy_parses_with_real_policy_api_and_is_source_safe(tmp_path):
    policy_path = tmp_path / "ai-review-policy.json"
    policy_path.write_text(json.dumps(documented_v3_policy()), encoding="utf-8")

    policy = load_review_policy(policy_path)

    assert {recipe.execution for recipe in policy.recipes} == {
        "integrated", "independent",
    }
    assert policy.generated_artifacts[0]["id"] == "openapi-client"
    assert policy.verdict_policy["blocker_requires_request_changes"] is True
    assert policy.publishing["allowed_modes"] == ("review_comment",)
    assert policy.sources
    for source in policy.sources:
        assert source.classification == "official-documentation"
        assert source.schemes == ("https",)
        assert source.host in {"platform.openai.com", "docs.python.org"}
        assert source.path_prefixes


def test_migration_document_maps_old_fields_and_semantics_to_v3():
    text = MIGRATION.read_text(encoding="utf-8")
    required = (
        "## Version-1/2 to version-3 mapping",
        "`components`",
        "`recipes`",
        "`match`",
        "Every populated match group must match",
        "values within a group use `any` semantics",
        "`exclude`",
        "`generated_artifacts`",
        "`source_of_truth`",
        "`generator_config`",
        "`output_paths`",
        "`execution`",
    )
    assert all(item in text for item in required)


def test_specialist_examples_use_the_reproducible_v2_baseline():
    expected = (
        "review_strategy: specialists",
        "review_policy_file: .github/ai-review-policy.json",
        'model_context_tokens: "262144"',
        'specialist_review_deadline_sec: "7200"',
        'specialist_concurrency: "1"',
        "system_prompt_mode: append",
        "publish_mode: review_comment",
    )
    for example in (
        ROOT / "examples" / "workflow-self-hosted.yml",
        ROOT / "examples" / "workflow-cloud.yml",
    ):
        text = example.read_text(encoding="utf-8")
        assert all(item in text for item in expected)


def test_migration_explains_handoff_outputs_manual_label_safety_and_troubleshooting():
    text = MIGRATION.read_text(encoding="utf-8")
    for required in (
        "review-handoff.md",
        "review-notes.json",
        "specialist-review-artifact.json",
        "Before applying it",
        "ai-review",
        "## Troubleshooting",
        "Provider overload or nondeterministic results",
        "Policy/source access is constrained or degraded",
    ):
        assert required in text


def test_migration_contains_copy_ready_tested_qwen_baseline():
    text = MIGRATION.read_text(encoding="utf-8")
    normalized = " ".join(text.split())
    for required in (
        "125m125/pr-reviewer-action@9091b940f9f64081dcf64070b71f2a3552e36318",
        'ai_model: qwen/qwen3.6-35b-a3b',
        'ai_max_tokens: "8192"',
        'model_context_tokens: "75000"',
        'specialist_max_conversation_tokens: "60000"',
        'specialist_structured_chat_template_kwargs:',
        'specialist_planner_max_tokens: "8192"',
        'specialist_max_tokens: "8192"',
        "types: [labeled]",
        "github.event.label.name == 'ai-review'",
        "review_scope: full",
        "standards_file: .github/ai-review-rules.md",
        "## Downstream adaptation checklist",
        "Keep these values initially",
        "Change these repository-specific values",
        "Tool access is disabled for checkpoint and repair turns",
        "controller-owned coverage and evidence metadata",
        "Structurally valid checkpoints are accepted in parts",
        "authoritative receipt",
        "candidate_drafts",
        "needs_followup",
        "independently",
        "one bounded synthesis",
        "Fresh version-3 adopters should not create this file",
    ):
        assert required in normalized


def test_documented_quickstart_and_moviehrdb_policy_are_executable(tmp_path):
    text = (ROOT / "docs" / "review-policy-authoring.md").read_text(encoding="utf-8")
    for title in ("Quick start: one owner", "movieHRdb-style ownership example"):
        match = re.search(r"## " + re.escape(title) + r".*?```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        assert match, title
        path = tmp_path / "policy.json"
        path.write_text(match.group(1), encoding="utf-8")
        policy = load_review_policy(path)
        assert policy.version == 3
        assert policy.components
        if title.startswith("movieHRdb"):
            assert policy.boundaries


def test_self_policy_uses_integrated_reviews_and_keeps_security_independent():
    policy = load_review_policy(ROOT / ".github" / "ai-review-policy.json")
    assert policy.version == 3
    assert {r.id for r in policy.recipes if r.execution == "independent"} == {"tool-and-secret-boundaries"}
    assert policy.boundaries
