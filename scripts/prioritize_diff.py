#!/usr/bin/env python3
"""Render a bounded, project-prioritized PR diff."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pr_reviewer.diff_prioritizer import (
    load_priority_config,
    manifest_from_diff,
    prioritize_diff,
)


def _repo_relative_config(path: Path | None) -> Path | None:
    """Accept only a repository-relative priority file.

    The file is configuration only: it may reorder or quota paths already in
    the controller-owned manifest, but it must not become an arbitrary file
    read primitive through an action input.
    """
    if path is None:
        return None
    if path.is_absolute() or path.root or path.drive or ".." in path.parts:
        return None
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--diff", required=True, type=Path)
    parser.add_argument("--files", type=Path)
    parser.add_argument("--derive-files", action="store_true")
    parser.add_argument("--max-bytes", required=True, type=int)
    parser.add_argument("--total-changed-files", type=int)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--index-output", required=True, type=Path)
    args = parser.parse_args()

    diff_text = args.diff.read_text(encoding="utf-8", errors="replace") if args.diff.is_file() else ""
    if args.derive_files:
        files = list(manifest_from_diff(diff_text))
    else:
        try:
            files = json.loads(args.files.read_text(encoding="utf-8")) if args.files else []
        except (OSError, ValueError, json.JSONDecodeError):
            files = []
        if not isinstance(files, list):
            files = []
    result = prioritize_diff(
        diff_text,
        [item for item in files if isinstance(item, dict)],
        args.max_bytes,
        config=load_priority_config(_repo_relative_config(args.config)),
        total_changed_files=args.total_changed_files,
    )
    args.output.write_text(result.text, encoding="utf-8")
    args.index_output.write_text(result.index, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
