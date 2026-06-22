#!/usr/bin/env python3
"""Bound git-grep impact output by match, file, and total UTF-8 bytes."""

from __future__ import annotations

import argparse
import sys


def cap_impact_lines(
    lines: list[str], *, per_match_bytes: int = 500,
    per_file_matches: int = 5, total_bytes: int = 12000,
) -> tuple[str, dict[str, int | bool]]:
    kept: list[str] = []
    counts: dict[str, int] = {}
    used = 0
    truncated_matches = 0
    omitted_matches = 0

    for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        filename = line.split(":", 1)[0] if ":" in line else "(unknown)"
        if counts.get(filename, 0) >= per_file_matches:
            omitted_matches += 1
            continue
        encoded = line.encode("utf-8", errors="replace")
        if len(encoded) > per_match_bytes:
            marker = b"...[match truncated]"
            encoded = encoded[: max(0, per_match_bytes - len(marker))] + marker
            line = encoded.decode("utf-8", errors="ignore")
            truncated_matches += 1
        entry_bytes = len((line + "\n").encode("utf-8"))
        if used + entry_bytes > total_bytes:
            omitted_matches += 1
            continue
        kept.append(line)
        used += entry_bytes
        counts[filename] = counts.get(filename, 0) + 1

    was_truncated = bool(truncated_matches or omitted_matches)
    if was_truncated:
        note = (
            f"...[impact scan capped: {truncated_matches} long matches shortened; "
            f"{omitted_matches} matches omitted]"
        )
        note_bytes = len((note + "\n").encode("utf-8"))
        while kept and used + note_bytes > total_bytes:
            removed = kept.pop()
            used -= len((removed + "\n").encode("utf-8"))
            omitted_matches += 1
        kept.append(note)
        used += note_bytes
    return "\n".join(kept) + ("\n" if kept else ""), {
        "truncated": was_truncated,
        "truncated_matches": truncated_matches,
        "omitted_matches": omitted_matches,
        "output_bytes": used,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-match-bytes", type=int, default=500)
    parser.add_argument("--per-file-matches", type=int, default=5)
    parser.add_argument("--total-bytes", type=int, default=12000)
    args = parser.parse_args()
    output, _ = cap_impact_lines(
        list(sys.stdin),
        per_match_bytes=max(64, args.per_match_bytes),
        per_file_matches=max(1, args.per_file_matches),
        total_bytes=max(256, args.total_bytes),
    )
    sys.stdout.write(output)


if __name__ == "__main__":
    main()
