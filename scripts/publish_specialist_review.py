#!/usr/bin/env python3
"""Publish final specialist-runtime handoff and review-note artifacts.

This is intentionally a narrow adapter.  It accepts no transcript, model
response, evidence-store, or tool-output argument; adjudication is complete
before this process starts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

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
from pr_reviewer.specialist_runtime.policy import parse_review_policy  # noqa: E402


def _json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _handoff(value: object) -> ReviewHandoff:
    if not isinstance(value, Mapping):
        raise ValueError("handoff JSON must be an object")
    return ReviewHandoff(
        markdown=str(value.get("markdown") or ""),
        recommendation=str(value.get("recommendation") or ""),
        status=str(value.get("status") or ""),
        change_map=tuple(value.get("change_map") or ()),
        reviewed_focuses=tuple(value.get("reviewed_focuses") or ()),
        specialist_focuses=tuple(value.get("specialist_focuses") or ()),
        recipe_focuses=tuple(value.get("recipe_focuses") or ()),
        coverage_boundaries=tuple(value.get("coverage_boundaries") or ()),
        thread_status=value.get("thread_status"),
        finding_theme=value.get("finding_theme"),
        review_emphasis=tuple(value.get("review_emphasis") or ()),
        coverage_warning=value.get("coverage_warning"),
        access_request_count=value.get("access_request_count", 0),
        access_request_url=value.get("access_request_url"),
        what_changed=tuple(value.get("what_changed") or ()),
        ai_reviewed=tuple(value.get("ai_reviewed") or ()),
        human_focus=tuple(value.get("human_focus") or ()),
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
            start_line=item.get("start_line"),
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


_TRUNCATION_NOTE_RE = re.compile(
    r"file list truncated to first \d+ of \d+ changed files\Z"
)
_PR_FILE_KEYS = {
    "filename", "status", "additions", "deletions", "changes",
    "previous_filename", "patch",
}


def _changed_files(
    value: object, *, complete: bool, expected_count: int | None
) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("files JSON must be an array")
    if complete and expected_count is None:
        raise ValueError(
            "changed-files count is required for a complete changed-files snapshot"
        )
    paths: list[str] = []
    saw_truncation_note = False
    for item in value:
        if isinstance(item, str):
            paths.append(item)
            continue
        if not isinstance(item, Mapping):
            raise ValueError("each files entry must be a path or PR-file object")
        if set(item) == {"note"} and isinstance(item.get("note"), str):
            if not _TRUNCATION_NOTE_RE.fullmatch(item["note"]):
                raise ValueError("files metadata note entry is invalid")
            saw_truncation_note = True
            continue
        if not set(item).issubset(_PR_FILE_KEYS) or not isinstance(
            item.get("filename"), str
        ):
            raise ValueError("each files entry must contain a valid filename")
        paths.append(item["filename"])
    if complete and saw_truncation_note:
        raise ValueError("complete changed-files snapshot is marked incomplete")
    if expected_count is not None:
        if expected_count < 0:
            raise ValueError("changed-files count must be non-negative")
        if complete and len(paths) != expected_count:
            raise ValueError("complete changed-files count does not match the snapshot")
    return tuple(paths)


def _bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized not in {"true", "false"}:
        raise argparse.ArgumentTypeError("expected true or false")
    return normalized == "true"


def _fork_state(value: str) -> bool | None:
    normalized = str(value).strip().lower()
    if normalized in {"", "unknown"}:
        return None
    if normalized not in {"true", "false"}:
        raise argparse.ArgumentTypeError("expected true, false, or unknown")
    return normalized == "true"


def _access_configuration(value: object, *, repo: str, head_sha: str, pr_number: int):
    if value is None:
        return None, ()
    if (
        not isinstance(value, Mapping)
        or value.get("repository") != repo
        or value.get("head_sha") != head_sha
        or value.get("pr_number") != pr_number
    ):
        raise ValueError("review artifact identity does not match repository, PR, and head")
    configuration = value.get("configuration", {})
    external = value.get("external_access", {})
    if not isinstance(configuration, Mapping) or not isinstance(external, Mapping):
        raise ValueError("review artifact authorization configuration must be an object")
    adapter = configuration.get("adapter", {})
    if not isinstance(adapter, Mapping):
        raise ValueError("review artifact adapter configuration must be an object")
    repositories = adapter.get("allowed_github_repositories", [])
    if not isinstance(repositories, list) or any(not isinstance(item, str) for item in repositories):
        raise ValueError("allowed_github_repositories must be an array of strings")
    # The artifact's policy field is metadata; these are the effective policy's rules.
    sources = external.get("allowed_sources")
    policy = parse_review_policy({"version": 3, "sources": sources}) if sources is not None else None
    return policy, tuple(repositories)


def _run_url(explicit: str | None, repo: str) -> str:
    candidate = explicit
    if candidate is None:
        run_id = os.environ.get("GITHUB_RUN_ID", "")
        if not re.fullmatch(r"[0-9]+", run_id) or os.environ.get("GITHUB_REPOSITORY") != repo:
            return ""
        candidate = os.environ.get("GITHUB_SERVER_URL", "https://github.com").rstrip("/") + f"/{repo}/actions/runs/{run_id}"
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
        if re.fullmatch(r"[1-9][0-9]*", attempt) and int(attempt) > 1:
            candidate += f"/attempts/{attempt}"
    try:
        parsed = urlsplit(candidate)
        valid = (
            re.fullmatch(r"https://[^\s<>\[\]()]+", candidate)
            and parsed.hostname and parsed.username is None and parsed.password is None
            and parsed.port in (None, 443) and not parsed.query and not parsed.fragment
            and re.fullmatch("/" + re.escape(repo) + r"/actions/runs/[0-9]+(?:/attempts/[1-9][0-9]*)?", parsed.path)
        )
    except ValueError:
        valid = False
    if not valid:
        if explicit is not None:
            raise ValueError("run URL must identify this repository's HTTPS GitHub Actions run")
        return ""
    return candidate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("comment", "review_comment", "review_verdict"))
    parser.add_argument("--handoff", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--diff", required=True)
    parser.add_argument("--files", required=True)
    parser.add_argument("--changed-files-complete", type=_bool, default=False)
    parser.add_argument("--changed-files-count", type=int)
    parser.add_argument("--diff-complete", type=_bool, default=False)
    parser.add_argument("--policy-result", required=True)
    parser.add_argument("--artifacts")
    parser.add_argument("--review-artifact")
    parser.add_argument("--run-url")
    parser.add_argument("--cleanup-previous-reviews", type=_bool, default=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--state", default="specialist-publication-state.json")
    parser.add_argument("--action-root", default=str(_ROOT))
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--allow-approve", type=_bool, default=False)
    parser.add_argument("--approve-forks", type=_bool, default=False)
    parser.add_argument("--is-fork", type=_fork_state, default=None)
    parser.add_argument("--effective-scope", choices=("full", "incremental"), default="full")
    parser.add_argument("--baseline-clean", type=_bool, default=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handoff = _handoff(_json(args.handoff))
    notes = _notes(_json(args.notes))
    changed_files = _changed_files(
        _json(args.files),
        complete=args.changed_files_complete,
        expected_count=args.changed_files_count,
    )
    policy = _policy(_json(args.policy_result))
    artifacts = _artifacts(_json(args.artifacts) if args.artifacts else None)
    access_policy, allowed_repositories = _access_configuration(
        _json(args.review_artifact) if args.review_artifact else None,
        repo=args.repo, head_sha=args.head_sha, pr_number=args.pr_number,
    )
    run_url = _run_url(args.run_url, args.repo)
    client = GhReviewClient(action_root=args.action_root)
    publisher = GitHubReviewPublisher(
        client, state_path=args.state, max_attempts=args.max_attempts
    )
    state = publisher.publish(
        mode=args.mode,
        handoff=handoff,
        notes=notes,
        diff_text=Path(args.diff).read_text(encoding="utf-8", errors="replace"),
        changed_files=changed_files,
        changed_files_complete=args.changed_files_complete,
        diff_complete=args.diff_complete,
        policy_result=policy,
        repo=args.repo,
        pr_number=args.pr_number,
        head_sha=args.head_sha,
        artifact_links=artifacts,
        access_policy=access_policy,
        allowed_github_repositories=allowed_repositories,
        run_url=run_url,
        cleanup_previous_reviews=args.cleanup_previous_reviews,
        approval_policy=PublisherApprovalPolicy(
            allow_approve=args.allow_approve,
            approve_forks=args.approve_forks,
            is_fork=args.is_fork,
            effective_scope=args.effective_scope,
            baseline_clean=args.baseline_clean,
        ),
    )
    if state.get('previous_review_cleanup') is not None:
        print('[review-publisher] previous review cleanup: '
              + json.dumps(state['previous_review_cleanup'], sort_keys=True), file=sys.stderr)
    if (
        state.get("review_completed") is not True
        or bool(state.get("publication_errors"))
    ):
        print(
            "Specialist review publication was not fully confirmed",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
