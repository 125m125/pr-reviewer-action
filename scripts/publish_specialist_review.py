#!/usr/bin/env python3
"""Publish final specialist-runtime handoff and review-note artifacts.

This is intentionally a narrow adapter.  It accepts no transcript, model
response, evidence-store, or tool-output argument; adjudication is complete
before this process starts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from pr_reviewer.enforcement import RuntimeVerdictPolicyResult  # noqa: E402
from pr_reviewer.github_review_notes import (  # noqa: E402
    GhReviewClient,
    GitHubReviewPublisher,
    PublisherApprovalPolicy,
)
from pr_reviewer.specialist_runtime.types import (  # noqa: E402
    ReviewHandoff,
    ReviewNote,
    ReviewNoteKind,
)


def _json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _handoff(value: object) -> ReviewHandoff:
    if not isinstance(value, Mapping):
        raise ValueError("handoff JSON must be an object")
    return ReviewHandoff(
        markdown=str(value.get("markdown") or ""),
        recommendation=str(value.get("recommendation") or ""),
        change_map=tuple(value.get("change_map") or ()),
        reviewed_focuses=tuple(value.get("reviewed_focuses") or ()),
        thread_status=value.get("thread_status"),
        finding_theme=value.get("finding_theme"),
        review_emphasis=tuple(value.get("review_emphasis") or ()),
        coverage_warning=value.get("coverage_warning"),
        access_request_count=value.get("access_request_count", 0),
        access_request_url=value.get("access_request_url"),
    )


def _notes(value: object) -> tuple[ReviewNote, ...]:
    if not isinstance(value, list):
        raise ValueError("notes JSON must be an array")
    notes = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("each review note must be an object")
        notes.append(ReviewNote(
            kind=ReviewNoteKind(str(item.get("kind") or "")),
            fingerprint=str(item.get("fingerprint") or ""),
            markdown=str(item.get("markdown") or ""),
            related_obligation_ids=tuple(item.get("related_obligation_ids") or ()),
            evidence_ids=tuple(item.get("evidence_ids") or ()),
            file=item.get("file"),
            line=item.get("line"),
            severity=item.get("severity"),
        ))
    return tuple(notes)


def _policy(value: object) -> RuntimeVerdictPolicyResult:
    if not isinstance(value, Mapping):
        raise ValueError("policy-result JSON must be an object")
    return RuntimeVerdictPolicyResult(
        verdict=str(value.get("verdict") or ""),
        source=str(value.get("source") or ""),
        blocking_finding_ids=tuple(value.get("blocking_finding_ids") or ()),
        blocking_obligation_ids=tuple(value.get("blocking_obligation_ids") or ()),
        unknown_obligation_ids=tuple(value.get("unknown_obligation_ids") or ()),
    )


def _artifacts(value: object) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("artifacts JSON must be an array")
    return tuple(
        (str(item.get("label") or ""), str(item.get("url") or ""))
        for item in value
        if isinstance(item, Mapping)
    )


def _bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized not in {"true", "false"}:
        raise argparse.ArgumentTypeError("expected true or false")
    return normalized == "true"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("comment", "review_comment", "review_verdict"))
    parser.add_argument("--handoff", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--diff", required=True)
    parser.add_argument("--files", required=True)
    parser.add_argument("--policy-result", required=True)
    parser.add_argument("--artifacts")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--state", default="specialist-publication-state.json")
    parser.add_argument("--action-root", default=str(_ROOT))
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--allow-approve", type=_bool, default=False)
    parser.add_argument("--approve-forks", type=_bool, default=False)
    parser.add_argument("--is-fork", type=_bool, default=False)
    parser.add_argument("--effective-scope", choices=("full", "incremental"), default="full")
    parser.add_argument("--baseline-clean", type=_bool, default=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handoff = _handoff(_json(args.handoff))
    notes = _notes(_json(args.notes))
    changed_files = _json(args.files)
    if not isinstance(changed_files, list):
        raise ValueError("files JSON must be an array")
    policy = _policy(_json(args.policy_result))
    artifacts = _artifacts(_json(args.artifacts) if args.artifacts else None)
    client = GhReviewClient(action_root=args.action_root)
    publisher = GitHubReviewPublisher(
        client, state_path=args.state, max_attempts=args.max_attempts
    )
    publisher.publish(
        mode=args.mode,
        handoff=handoff,
        notes=notes,
        diff_text=Path(args.diff).read_text(encoding="utf-8", errors="replace"),
        changed_files=changed_files,
        policy_result=policy,
        repo=args.repo,
        pr_number=args.pr_number,
        head_sha=args.head_sha,
        artifact_links=artifacts,
        approval_policy=PublisherApprovalPolicy(
            allow_approve=args.allow_approve,
            approve_forks=args.approve_forks,
            is_fork=args.is_fork,
            effective_scope=args.effective_scope,
            baseline_clean=args.baseline_clean,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
