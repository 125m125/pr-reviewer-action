"""Contract checks for the downstream specialist-runtime migration handoff."""

from __future__ import annotations

import re
from pathlib import Path


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
