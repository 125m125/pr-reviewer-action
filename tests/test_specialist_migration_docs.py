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


def documented_v2_policy() -> dict[str, object]:
    """Return the version-2 JSON fence from the executable migration guide."""
    text = MIGRATION.read_text(encoding="utf-8")
    match = re.search(
        r"## Complete version-2 policy example.*?```json\s*(\{.*?\})\s*```",
        text,
        flags=re.DOTALL,
    )
    assert match, "the migration handoff must contain one version-2 policy JSON fence"
    return json.loads(match.group(1))


def test_migration_document_covers_required_repository_files():
    text = MIGRATION.read_text(encoding="utf-8")
    for required in (
        ".github/ai-review-rules.md",
        ".github/ai-review-specialists.json",
        ".github/ai-review-prompt.md",
        ".github/ai-review-policy.json",
        "review_policy_file",
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


def test_documented_v2_policy_parses_with_real_policy_api_and_is_source_safe(tmp_path):
    policy_path = tmp_path / "ai-review-policy.json"
    policy_path.write_text(json.dumps(documented_v2_policy()), encoding="utf-8")

    policy = load_review_policy(policy_path)

    assert {recipe.execution for recipe in policy.recipes} == {
        "coverage", "dedicated", "independent",
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


def test_migration_document_maps_v1_fields_and_semantics_to_v2():
    text = MIGRATION.read_text(encoding="utf-8")
    required = (
        "## Version-1 to version-2 mapping",
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
