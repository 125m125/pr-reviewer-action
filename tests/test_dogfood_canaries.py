"""Guardrails for the inert review-pipeline dogfood fixture."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CANARY_DIRECTORY = ROOT / "evals" / "dogfood_canaries"
FIXTURE = CANARY_DIRECTORY / "repository_access.py"
FIXTURE_MODULE = "evals.dogfood_canaries.repository_access"


def _production_imports(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
    return imports


def test_dogfood_fixture_is_outside_production_review_paths():
    assert FIXTURE.is_file()
    assert not (CANARY_DIRECTORY / "__init__.py").exists()

    production_sources = [
        *(ROOT / "pr_reviewer").rglob("*.py"),
        *(ROOT / "scripts").rglob("*.py"),
        *(ROOT / "scripts").rglob("*.sh"),
        ROOT / "action.yml",
        ROOT / "evals" / "corpus-agentic.json",
    ]
    imported_modules = {
        module
        for source in production_sources
        if source.suffix == ".py"
        for module in _production_imports(source)
    }

    assert FIXTURE_MODULE not in imported_modules
    assert not any(module.startswith("evals.dogfood_canaries")
                   for module in imported_modules)
    assert all(
        "dogfood_canaries" not in source.read_text(encoding="utf-8")
        for source in production_sources
    )


def test_dogfood_fixture_has_no_committed_companion_artifacts():
    assert sorted(path.name for path in CANARY_DIRECTORY.iterdir()) == [
        "repository_access.py",
    ]
