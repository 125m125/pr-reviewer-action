"""Deterministic GitHub publication for specialist-runtime review notes.

The publisher's input boundary is intentionally narrow: a final policy result,
the sparse :class:`ReviewHandoff`, typed :class:`ReviewNote` values, and the
current PR snapshot.  Raw transcripts and evidence-store objects are neither
accepted nor inspected here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from pr_reviewer.access_request_reconciliation import reconcile_access_request_comment
from pr_reviewer.enforcement import RuntimeVerdictPolicyResult
from pr_reviewer.platform import gh_argv
from pr_reviewer.specialist_runtime.policy import ReviewPolicy
from pr_reviewer.specialist_runtime.types import ReviewHandoff, ReviewNote, ReviewNoteKind
from pr_reviewer.specialist_runtime.review_threads import (
    assessment_receipt, receipt_assessment, resolution_receipt, receipt_resolutions,
    resolution_is_current, _discussion_digest,
)
from scripts.redact import mask_secrets
from scripts.sanitize_review_markdown import sanitize_markdown
from scripts.strip_metadata_markers import strip_reserved_markers


def _same_github_login(left: object, right: object) -> bool:
    """REST/app bot identities can carry a suffix absent from GraphQL authors."""
    def normalized(value):
        return value.casefold().removesuffix('[bot]') if isinstance(value, str) else ''
    first, second = normalized(left), normalized(right)
    return bool(first and second and first == second)


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_NOTE_MARKER_RE = re.compile(r"<!--\s*ai-pr-review-note:([^\s>]+)\s*-->")
_NOTE_GENERATION_MARKER_RE = re.compile(
    r"<!--\s*ai-pr-review-note:([^\s>]+)\s+generation=(\d+)"
    r"(?:\s+publication=([0-9a-f]{32}))?\s*-->"
)
_GENERAL_MARKER_RE = re.compile(
    r"<!--\s*ai-pr-review-general:([^\s>]+)"
    r"(?:\s+generation=(\d+)\s+publication=([0-9a-f]{32})"
    r"\s+content=([0-9a-f]{16}))?\s*-->"
)
_GENERAL_ANSWER_MARKER_RE = re.compile(
    r"<!--\s*ai-pr-review-general-answer:([^\s>]+?)"
    r"(?:\s+generation=(\d+)\s+publication=([0-9a-f]{32})"
    r"\s+content=([0-9a-f]{16})|:[0-9a-f]{40,64})\s*-->"
)
_PUBLISHER_RESOLUTION_RE = re.compile(
    r"<!--\s*ai-pr-review-resolution:([^>]+?)(?::g\d+:[0-9a-f]{40,64})?:publisher\s*-->"
)
_OWN_MARKER_RE = re.compile(
    r"<!--\s*ai-pr-review(?:er)?-"
    r"(?:note|general(?:-answer)?|resolution|status|specialist(?:-handoff)?|run|thread-assessments|thread-resolutions|block-inventory)"
    r"(?::[^>]*)?\s*-->",
    re.IGNORECASE,
)
_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


@dataclass(frozen=True)
class NoteAnchor:
    """A GitHub-resolvable anchor validated against the current PR snapshot."""

    subject_type: str
    path: str
    line: int | None = None
    side: str | None = None
    start_line: int | None = None


@dataclass(frozen=True)
class NormalizedReviewNote:
    kind: ReviewNoteKind
    fingerprint: str
    markdown: str
    managed_markdown: str
    related_obligation_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    severity: str | None
    anchor: NoteAnchor | None
    actionable: bool
    generation: int = 1
    publication_id: str | None = None


@dataclass(frozen=True)
class PublisherApprovalPolicy:
    """The existing native-approval safety gates, evaluated after policy."""

    allow_approve: bool = False
    approve_forks: bool = False
    is_fork: bool | None = None
    effective_scope: str = "full"
    baseline_clean: bool = False

    def __post_init__(self) -> None:
        for name in ("allow_approve", "approve_forks", "baseline_clean"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")
        if self.is_fork is not None and type(self.is_fork) is not bool:
            raise TypeError("is_fork must be a boolean or unknown")
        if self.effective_scope not in {"full", "incremental"}:
            raise ValueError("effective_scope must be full or incremental")


class ReviewPublishClient(Protocol):
    def update_sticky(
        self,
        repo: str,
        pr_number: int,
        body: str,
        known_comment_id: int | None = None,
    ) -> Mapping[str, Any]: ...
    def query_pr_identity(self, repo: str, pr_number: int) -> Mapping[str, Any]: ...
    def query_review_threads(self, repo: str, pr_number: int, head_sha: str) -> Sequence[Mapping[str, Any]]: ...
    def query_managed_state(self, repo: str, pr_number: int) -> Mapping[str, Any]: ...
    def reply_thread(self, comment_id: object, body: str) -> Mapping[str, Any]: ...
    def resolve_thread(self, thread_id: str) -> Mapping[str, Any]: ...
    def create_pending_review(
        self, pull_request_id: str, head_sha: str, body: str
    ) -> Mapping[str, Any]: ...
    def add_review_thread(self, variables: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def submit_review(self, review_id: str, event: str, body: str) -> Mapping[str, Any]: ...
    def minimize_comment(self, node_id: str, classifier: str) -> Mapping[str, Any]: ...
    def dismiss_review(self, review_id: str) -> Mapping[str, Any]: ...
    def annotate_access_request(self, repo: str, pr_number: int, prior: Mapping[str, Any], body: str) -> Mapping[str, Any]: ...
    def upsert_general_comment(
        self, repo: str, pr_number: int, prior: Mapping[str, Any] | None, body: str
    ) -> Mapping[str, Any]: ...
    def reply_general_comment(
        self, repo: str, pr_number: int, prior: Mapping[str, Any], body: str
    ) -> Mapping[str, Any]: ...


def _safe_repo_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    if value.startswith(("/", "\\")) or "\\" in value:
        return None
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return value


def _changed_file_set(changed_files: Iterable[object] | None) -> set[str]:
    result: set[str] = set()
    for item in changed_files or ():
        candidate = item.get("filename") if isinstance(item, Mapping) else item
        path = _safe_repo_path(candidate)
        if path:
            result.add(path)
    return result


def _diff_snapshot(diff_text: str) -> tuple[set[str], dict[str, set[int]]]:
    """Return changed paths and added RIGHT-side lines from a unified diff."""

    paths: set[str] = set()
    added: dict[str, set[int]] = {}
    current: str | None = None
    new_line = 0
    in_hunk = False
    pending_old_header = False
    for raw in str(diff_text or "").splitlines():
        if raw.startswith("diff --git "):
            current = None
            in_hunk = False
            pending_old_header = False
            continue
        if not in_hunk and raw.startswith("--- "):
            pending_old_header = True
            continue
        if not in_hunk and pending_old_header and raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                current = None
            else:
                candidate = target[2:] if target.startswith("b/") else target
                current = _safe_repo_path(candidate)
                if current:
                    paths.add(current)
            pending_old_header = False
            continue
        match = _HUNK_RE.match(raw)
        if match:
            new_line = int(match.group(1))
            in_hunk = True
            pending_old_header = False
            continue
        if current is None or not in_hunk or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            added.setdefault(current, set()).add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            continue
        else:
            new_line += 1
    return paths, added


def diff_positions(diff_text: str) -> dict[str, dict[int, int]]:
    """Map commentable RIGHT-side lines to diff positions by changed path.

    The single-review publisher uses these positions for both GitHub line
    comments and Forgejo's ``new_position`` payload. Specialist review notes
    use :func:`choose_note_anchor`.
    """

    positions_by_path: dict[str, dict[int, int]] = {}
    current_path: str | None = None
    new_line = 0
    diff_position = 0
    in_hunk = False
    pending_old_header = False
    for raw in str(diff_text or "").splitlines():
        if raw.startswith("diff --git "):
            current_path = None
            in_hunk = False
            diff_position = 0
            pending_old_header = False
            continue
        if not in_hunk and raw.startswith("--- "):
            pending_old_header = True
            continue
        if not in_hunk and pending_old_header and raw.startswith("+++ "):
            target = raw[4:].strip()
            current_path = None if target == "/dev/null" else (
                target[2:] if target.startswith("b/") else target
            )
            pending_old_header = False
            continue
        match = _HUNK_RE.match(raw)
        if match:
            new_line = int(match.group(1))
            in_hunk = True
            pending_old_header = False
            continue
        if current_path is None or not in_hunk or raw.startswith("\\"):
            continue
        diff_position += 1
        if raw.startswith("+"):
            positions_by_path.setdefault(current_path, {})[new_line] = diff_position
            new_line += 1
        elif raw.startswith("-"):
            continue
        else:
            positions_by_path.setdefault(current_path, {})[new_line] = diff_position
            new_line += 1
    return positions_by_path


def extract_managed_fingerprint(body: object, marker_prefix: str) -> str | None:
    """Extract a bounded fingerprint following a locally supplied marker."""

    if not isinstance(body, str) or not isinstance(marker_prefix, str) or not marker_prefix:
        return None
    start = body.find(marker_prefix)
    if start < 0:
        return None
    rest = body[start + len(marker_prefix):]
    end = rest.find("-->")
    if end < 0:
        return None
    value = rest[:end].strip()
    return value[:160] if value else None


def choose_note_anchor(
    note: ReviewNote,
    diff_text: str,
    changed_files: Iterable[object] | None = None,
) -> NoteAnchor | None:
    """Choose LINE, then FILE, using only the current PR diff/files.

    A note's path or line is a hint, never authority. LINE is limited to an
    added RIGHT-side line present in the parsed diff. FILE requires the path in
    the complete current PR files snapshot, or in the diff only when no files
    snapshot was supplied.
    """

    # Future: carry LEFT-side coordinates through findings and publishing so a
    # deletion-only defect can be attached to the removed line instead of the file.

    path = _safe_repo_path(getattr(note, "file", None))
    if not path:
        return None
    diff_paths, added_lines = _diff_snapshot(diff_text)
    supplied = _changed_file_set(changed_files)
    trusted_paths = supplied if changed_files is not None else diff_paths
    if path not in trusted_paths:
        return None
    line = getattr(note, "line", None)
    if (
        isinstance(line, int)
        and not isinstance(line, bool)
        and line > 0
        and line in added_lines.get(path, set())
    ):
        commentable_lines = diff_positions(diff_text).get(path, {})
        start_line = getattr(note, "start_line", None)
        if not (
            isinstance(start_line, int)
            and not isinstance(start_line, bool)
            and 0 < start_line < line
            and all(
                candidate in commentable_lines
                for candidate in range(start_line, line + 1)
            )
        ):
            start_line = None
        return NoteAnchor("LINE", path, line, "RIGHT", start_line)
    return NoteAnchor("FILE", path)


def _safe_fingerprint(value: object) -> str:
    raw = str(value or "").strip()
    if _FINGERPRINT_RE.fullmatch(raw):
        return raw
    return "normalized:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean_markdown(value: object) -> str:
    text = str(value or "").strip()
    text = _OWN_MARKER_RE.sub("", strip_reserved_markers(text))
    return sanitize_markdown(mask_secrets(text)).strip()


def _note_marker(
    fingerprint: str, generation: int = 1, publication_id: str | None = None
) -> str:
    publication = f" publication={publication_id}" if publication_id else ""
    return f"<!-- ai-pr-review-note:{fingerprint} generation={generation}{publication} -->"


def _general_identity(
    fingerprint: str, publication_id: str, generation: int, content_digest: str
) -> str:
    return f"{fingerprint}:{publication_id}:{generation}:{content_digest}"


def _general_marker(note: NormalizedReviewNote) -> str:
    return (
        f"<!-- ai-pr-review-general:{note.fingerprint} generation={note.generation} "
        f"publication={note.publication_id} content={_note_reply_digest(note)} -->"
    )


def _note_generation(note: NormalizedReviewNote, generation: int) -> NormalizedReviewNote:
    return replace(
        note,
        generation=generation,
        managed_markdown=(
            f"{note.markdown}\n\n"
            f"{_note_marker(note.fingerprint, generation, note.publication_id)}"
        ),
    )


def _bind_note_publication(
    note: NormalizedReviewNote, publication_id: str
) -> NormalizedReviewNote:
    return replace(
        note,
        publication_id=publication_id,
        managed_markdown=(
            f"{note.markdown}\n\n"
            f"{_note_marker(note.fingerprint, note.generation, publication_id)}"
        ),
    )


def _note_reply_digest(note: NormalizedReviewNote) -> str:
    identity = {
        "kind": note.kind.value,
        "markdown": note.markdown,
        "related_obligation_ids": sorted(note.related_obligation_ids),
        "evidence_ids": sorted(note.evidence_ids),
        "severity": note.severity,
        "anchor": (
            {
                "subject_type": note.anchor.subject_type,
                "path": note.anchor.path,
                "line": note.anchor.line,
                "side": note.anchor.side,
                "start_line": note.anchor.start_line,
            }
            if note.anchor is not None
            else None
        ),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def normalize_note(
    note: ReviewNote,
    diff_text: str = "",
    changed_files: Iterable[object] | None = None,
) -> NormalizedReviewNote:
    """Sanitize, anchor, and fail closed for unanchored factual findings."""

    if not isinstance(note, ReviewNote):
        raise TypeError("note must be a ReviewNote")
    try:
        kind = note.kind if isinstance(note.kind, ReviewNoteKind) else ReviewNoteKind(note.kind)
    except (TypeError, ValueError):
        kind = ReviewNoteKind.VERIFICATION_REQUEST
    anchor = choose_note_anchor(note, diff_text, changed_files)
    actionable = kind is ReviewNoteKind.FINDING and anchor is not None
    downgraded = kind is ReviewNoteKind.FINDING and anchor is None
    if downgraded:
        kind = ReviewNoteKind.VERIFICATION_REQUEST
        actionable = False
    fingerprint = _safe_fingerprint(note.fingerprint)
    markdown = _clean_markdown(note.markdown)
    if downgraded and markdown:
        markdown = (
            "### Verification request\n\n"
            "This unanchored item is non-actionable until a human verifies it.\n\n"
            + markdown
        )
    if not markdown:
        markdown = "Verification is required before drawing a review conclusion."
        kind = ReviewNoteKind.VERIFICATION_REQUEST
        actionable = False
    managed = f"{markdown}\n\n{_note_marker(fingerprint)}"
    return NormalizedReviewNote(
        kind=kind,
        fingerprint=fingerprint,
        markdown=markdown,
        managed_markdown=managed,
        related_obligation_ids=tuple(str(item) for item in note.related_obligation_ids),
        evidence_ids=tuple(str(item) for item in note.evidence_ids),
        severity=note.severity if kind is ReviewNoteKind.FINDING else None,
        anchor=anchor,
        actionable=actionable,
    )


def build_review_thread_variables(
    pull_request_review_id: str, note: NormalizedReviewNote
) -> dict[str, Any]:
    """Build exact GitHub addPullRequestReviewThread variables."""

    if not isinstance(note, NormalizedReviewNote) or note.anchor is None:
        raise ValueError("a normalized line/file note is required")
    variables: dict[str, Any] = {
        "pullRequestReviewId": pull_request_review_id,
        "body": note.managed_markdown,
        "subjectType": note.anchor.subject_type,
        "path": note.anchor.path,
    }
    if note.anchor.subject_type == "LINE":
        variables.update({"line": note.anchor.line, "side": "RIGHT"})
        if note.anchor.start_line is not None:
            variables.update({
                "startLine": note.anchor.start_line,
                "startSide": "RIGHT",
            })
    elif note.anchor.subject_type != "FILE":
        raise ValueError("unsupported review-note anchor")
    return variables


def _canonical_artifact_url(value: object) -> str | None:
    raw = str(value or "").strip()
    if (
        not raw
        or len(raw) > 2048
        or any(character.isspace() or ord(character) < 32 for character in raw)
        or any(character in raw for character in "\\<>")
    ):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        return None
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        decoded_path = unquote(parsed.path)
    except (UnicodeError, ValueError):
        return None
    if any(
        character.isspace() or ord(character) < 32 or character in "\\<>"
        for character in decoded_path
    ):
        return None
    netloc = host + (f":{port}" if port and port != 443 else "")
    path = quote(decoded_path or "/", safe="/%:@-._~!$&'()*+,;=")
    return urlunsplit(("https", netloc, path, "", ""))


def _valid_artifact_url(value: object) -> bool:
    return _canonical_artifact_url(value) is not None


_GITHUB_RESULT_FRAGMENT_RE = re.compile(
    r"(?:issuecomment-\d+|discussion_r\d+|pullrequestreview-\d+)\Z"
)


def _canonical_github_result_url(value: object) -> str | None:
    """Canonicalize an HTTPS URL returned by GitHub/enterprise APIs."""

    raw = str(value or "").strip()
    if (
        not raw
        or len(raw) > 2048
        or any(character.isspace() or ord(character) < 32 for character in raw)
        or any(character in raw for character in "\\<>")
    ):
        return None
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or port == 0
        or not _GITHUB_RESULT_FRAGMENT_RE.fullmatch(parsed.fragment)
    ):
        return None
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        decoded_path = unquote(parsed.path)
    except (UnicodeError, ValueError):
        return None
    if (
        not decoded_path.startswith("/")
        or not re.search(r"/[^/]+/[^/]+/(?:pull|issues)/\d+\Z", decoded_path)
        or any(
            character.isspace() or ord(character) < 32 or character in "\\<>"
            for character in decoded_path
        )
    ):
        return None
    netloc = host + (f":{port}" if port and port != 443 else "")
    path = quote(decoded_path, safe="/%:@-._~!$&'()*+,;=")
    return urlunsplit(("https", netloc, path, "", parsed.fragment))


def _valid_github_result_url(value: object) -> bool:
    return _canonical_github_result_url(value) is not None


def _markdown_label(value: object) -> str:
    label = " ".join(mask_secrets(str(value or "")).split())[:100]
    label = sanitize_markdown(label)
    label = re.sub(r"(?i)https?://", lambda match: match.group(0)[:-2] + "\u200b//", label)
    return label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _valid_comment_result(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("id"), int)
        and not isinstance(value.get("id"), bool)
        and value["id"] > 0
        and isinstance(value.get("url"), str)
        and _valid_github_result_url(value["url"])
    )


def _valid_review_result(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("id"), str)
        and bool(value["id"])
        and isinstance(value.get("url"), str)
        and _valid_github_result_url(value["url"])
    )


def _valid_thread_result(value: object) -> bool:
    return (
        _valid_review_result(value)
        and isinstance(value.get("comment_id"), int)
        and not isinstance(value.get("comment_id"), bool)
        and value["comment_id"] > 0
    )


def _valid_pending_review(value: object, marker: str) -> bool:
    return (
        _valid_review_result(value)
        and value.get("state") == "PENDING"
        and value.get("body") == marker
    )


def _valid_submitted_review(
    value: object, marker: str, expected_state: str
) -> bool:
    return (
        _valid_review_result(value)
        and value.get("state") == expected_state
        and isinstance(value.get("body"), str)
        and (
            value["body"] == marker
            or value["body"].startswith(marker + "\n")
        )
    )


def _handoff_body(
    handoff: ReviewHandoff,
    artifact_links: Sequence[tuple[str, str]],
    review_url: str | None = None,
) -> str:
    if not isinstance(handoff, ReviewHandoff):
        raise TypeError("handoff must be a ReviewHandoff")
    body = _clean_markdown(handoff.markdown)
    links = []
    for label, url in artifact_links[:10]:
        clean_label = _markdown_label(label)
        clean_url = _canonical_artifact_url(url)
        if clean_label and clean_url:
            links.append(f"- [{clean_label}](<{clean_url}>)")
    if links:
        body = body + "\n\n**Retained review artifacts**\n\n" + "\n".join(links)
    clean_review_url = _canonical_github_result_url(review_url)
    if clean_review_url:
        body += f"\n\n[Detailed managed review](<{clean_review_url}>)"
    return "<!-- ai-pr-review-specialist-handoff -->\n" + body.strip()


def _native_event(
    policy_result: RuntimeVerdictPolicyResult, approval: PublisherApprovalPolicy
) -> tuple[str, str]:
    if not isinstance(policy_result, RuntimeVerdictPolicyResult):
        raise TypeError("policy_result must be a RuntimeVerdictPolicyResult")
    if policy_result.verdict == "notice":
        return "COMMENT", "coverage incomplete; non-verdict specialist review"
    if policy_result.verdict == "request_changes":
        return "REQUEST_CHANGES", "policy requested changes"
    if policy_result.verdict != "approve":
        raise ValueError("policy verdict must be approve, request_changes, or notice")
    if not approval.allow_approve:
        return "REQUEST_CHANGES", "native approval disabled"
    if approval.effective_scope == "incremental" and not approval.baseline_clean:
        return "REQUEST_CHANGES", "incremental approval lacks a clean baseline"
    if approval.is_fork is None:
        return "REQUEST_CHANGES", "pull request fork identity is unknown"
    if approval.is_fork and not approval.approve_forks:
        return "REQUEST_CHANGES", "fork approval disabled"
    return "APPROVE", "approval safety policy passed"


_EXPECTED_REVIEW_STATE = {
    "COMMENT": "COMMENTED",
    "APPROVE": "APPROVED",
    "REQUEST_CHANGES": "CHANGES_REQUESTED",
}


def _publication_id(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    mode: str,
    event: str,
    policy_result: RuntimeVerdictPolicyResult,
    notes: Sequence[ReviewNote],
    handoff: ReviewHandoff,
    run_url: str = "",
) -> str:
    note_identities = []
    for note in notes:
        note_identities.append({
            "fingerprint": _safe_fingerprint(note.fingerprint),
            "content_digest": hashlib.sha256(json.dumps({
                "kind": (
                    note.kind.value
                    if isinstance(note.kind, ReviewNoteKind)
                    else str(note.kind)
                ),
                "markdown": _clean_markdown(note.markdown),
                "related_obligation_ids": sorted(str(item) for item in note.related_obligation_ids),
                "evidence_ids": sorted(str(item) for item in note.evidence_ids),
                "file": note.file,
                "line": note.line,
                "severity": note.severity,
            }, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest(),
        })
    identity = {
        "body_format": "native-handoff-v1",
        "run_url": run_url,
        "repo": repo.lower(),
        "pr_number": pr_number,
        "head_sha": head_sha,
        "mode": mode,
        "event": event,
        "policy": {
            "verdict": policy_result.verdict,
            "source": policy_result.source,
            "blocking_finding_ids": sorted(policy_result.blocking_finding_ids),
            "blocking_obligation_ids": sorted(policy_result.blocking_obligation_ids),
            "unknown_obligation_ids": sorted(policy_result.unknown_obligation_ids),
        },
        "notes": sorted(
            note_identities,
            key=lambda item: (item["fingerprint"], item["content_digest"]),
        ),
        "handoff": {
            "markdown": _clean_markdown(handoff.markdown),
            "recommendation": str(handoff.recommendation or ""),
            "status": str(handoff.status or ""),
            "change_map": list(handoff.change_map),
            "reviewed_focuses": list(handoff.reviewed_focuses),
            "specialist_focuses": list(handoff.specialist_focuses),
            "recipe_focuses": list(handoff.recipe_focuses),
            "coverage_boundaries": list(handoff.coverage_boundaries),
            "thread_status": handoff.thread_status,
            "finding_theme": handoff.finding_theme,
            "review_emphasis": list(handoff.review_emphasis),
            "coverage_warning": handoff.coverage_warning,
            "access_request_count": handoff.access_request_count,
            "access_request_url": handoff.access_request_url,
            "what_changed": list(handoff.what_changed),
            "ai_reviewed": list(handoff.ai_reviewed),
            "human_focus": list(handoff.human_focus),
        },
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()[:32]


def _publication_marker(publication_id: str, event: str) -> str:
    return f"<!-- ai-pr-reviewer-specialist:{publication_id}:event={event} -->"


def _concern_inventory(children: Iterable[tuple[str, str]]) -> str:
    encoded = json.dumps(sorted(children), separators=(",", ":"), ensure_ascii=True)
    return "<!-- ai-pr-review-block-inventory:" + hashlib.sha256(encoded.encode()).hexdigest() + " -->"


_STATE_VERSION = 2


def _matching_prior_state(
    value: object,
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    publication_id: str,
) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping) or value.get("version") != _STATE_VERSION:
        return None
    if (
        value.get("repo") != repo
        or value.get("pr_number") != pr_number
        or value.get("head_sha") != head_sha
        or value.get("publication_id") != publication_id
    ):
        return None
    notes, journal, errors = (
        value.get("notes"), value.get("journal"), value.get("publication_errors")
    )
    if not (
        isinstance(notes, list)
        and all(isinstance(item, Mapping) for item in notes)
        and isinstance(journal, list)
        and all(isinstance(item, Mapping) for item in journal)
        and isinstance(errors, list)
        and all(isinstance(item, Mapping) for item in errors)
    ):
        return None
    expected_sequence = 1
    for entry in journal:
        if (
            entry.get("sequence") != expected_sequence
            or not isinstance(entry.get("operation"), str)
        ):
            return None
        expected_sequence += 1
    return value


class GitHubReviewPublisher:
    """Publish normalized specialist notes with deterministic lifecycle state."""

    def __init__(
        self,
        client: ReviewPublishClient,
        *,
        state_path: str | os.PathLike[str] = "specialist-publication-state.json",
        max_attempts: int = 2,
    ) -> None:
        self.client = client
        self.state_path = Path(state_path)
        self.max_attempts = max(1, min(int(max_attempts), 3))
        self._errors: list[dict[str, str]] = []

    def _call(self, operation: str, function, *args, retry_safe: bool = False):
        last: Exception | None = None
        attempts = self.max_attempts if retry_safe else 1
        for _attempt in range(attempts):
            try:
                return function(*args)
            except Exception as exc:  # publication failures are persisted separately
                last = exc
        self._errors.append({"operation": operation, "error": mask_secrets(str(last))[:500]})
        return None

    def _checkpoint(
        self,
        state: dict[str, Any],
        operation: str,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "sequence": len(state.setdefault("journal", [])) + 1,
            "operation": operation,
        }
        for key in (
            "id", "url", "comment_id", "thread_id", "fingerprint",
            "generation", "publication_id", "review_id",
        ):
            value = (result or {}).get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                entry[key] = value
        state["journal"].append(entry)
        state["publication_errors"] = self._errors
        self._write_state(state)

    def _write_state(self, state: Mapping[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=self.state_path.name + ".", suffix=".tmp", dir=self.state_path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(state, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
            os.replace(temporary, self.state_path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def publish(
        self,
        *,
        mode: str,
        handoff: ReviewHandoff,
        notes: Iterable[ReviewNote],
        diff_text: str,
        changed_files: Iterable[object],
        policy_result: RuntimeVerdictPolicyResult,
        repo: str,
        pr_number: int,
        head_sha: str,
        artifact_links: Sequence[tuple[str, str]] = (),
        approval_policy: PublisherApprovalPolicy = PublisherApprovalPolicy(),
        changed_files_complete: bool = False,
        diff_complete: bool = False,
        cleanup_previous_reviews: bool = True,
        access_policy: Mapping[str, Any] | ReviewPolicy | None = None,
        allowed_github_repositories: Iterable[str] = (),
        run_url: str = "",
        review_thread_snapshots: Sequence[Mapping[str, Any]] = (),
        review_thread_assessments: Sequence[Mapping[str, Any]] = (),
        review_thread_associations: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        if mode not in {"comment", "review_comment", "review_verdict"}:
            raise ValueError("unsupported publish mode")
        if not isinstance(policy_result, RuntimeVerdictPolicyResult):
            raise TypeError("policy_result must be a RuntimeVerdictPolicyResult")
        if policy_result.verdict not in {"approve", "request_changes", "notice"}:
            raise ValueError(
                "policy verdict must be approve, request_changes, or notice"
            )
        if not isinstance(approval_policy, PublisherApprovalPolicy):
            raise TypeError("approval_policy must be a PublisherApprovalPolicy")
        if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
            raise ValueError("pr_number must be a positive integer")
        repo_parts = str(repo or "").split("/")
        if (
            len(repo_parts) != 2
            or any(part in {"", ".", ".."} for part in repo_parts)
            or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in repo_parts)
        ):
            raise ValueError("repo must be owner/name")
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head_sha or ""):
            raise ValueError("head_sha must be a canonical 40- or 64-character hex digest")
        if type(changed_files_complete) is not bool or type(diff_complete) is not bool:
            raise TypeError("snapshot completeness flags must be boolean")
        if type(cleanup_previous_reviews) is not bool:
            raise TypeError("cleanup_previous_reviews must be boolean")
        allowed_github_repositories = tuple(allowed_github_repositories)
        if not isinstance(handoff, ReviewHandoff):
            raise TypeError("handoff must be a ReviewHandoff")
        if not isinstance(diff_text, str):
            raise TypeError("diff_text must be a string")
        notes_snapshot = tuple(notes)
        snapshots = {}
        for item in review_thread_snapshots:
            if (not isinstance(item, Mapping) or not re.fullmatch(r"H[1-9]\d*", str(item.get("target", "")))
                    or item["target"] in snapshots or item.get("repository") != repo or item.get("head_sha") != head_sha
                    or type(item.get("pr_number")) is not int or item["pr_number"] != pr_number):
                raise ValueError("review-thread snapshot identity mismatch")
            snapshots[item["target"]] = item
        for record in (*review_thread_assessments, *review_thread_associations):
            if (not isinstance(record, Mapping) or record.get("target") not in snapshots
                    or record.get("snapshot") != snapshots[record["target"]]):
                raise ValueError("review-thread decision does not match its snapshot")
        if not all(isinstance(note, ReviewNote) for note in notes_snapshot):
            raise TypeError("notes must contain only ReviewNote values")
        files_snapshot = tuple(changed_files)
        if changed_files_complete and any(
            not isinstance(path, str) or _safe_repo_path(path) != path
            for path in files_snapshot
        ):
            raise ValueError("complete changed_files must contain safe repository paths")
        if mode == "comment":
            desired_event, event_reason = "COMMENT", "sticky handoff only"
        elif mode == "review_comment":
            desired_event, event_reason = "COMMENT", "non-verdict specialist review"
        else:
            desired_event, event_reason = _native_event(policy_result, approval_policy)
        if desired_event == "APPROVE":
            try:
                prior_reviews = self.client.query_managed_state(repo, pr_number)
                blocked = any(item.get("state") == "CHANGES_REQUESTED"
                              for item in prior_reviews.get("reviews", ()))
            except Exception:
                blocked = True
            if blocked:
                desired_event, event_reason = "COMMENT", "prior changes-requested review requires explicit clearance before approval"
        publication_id = _publication_id(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            mode=mode,
            event=desired_event,
            policy_result=policy_result,
            notes=notes_snapshot,
            handoff=handoff,
            run_url=run_url,
        )
        expected_review_state = _EXPECTED_REVIEW_STATE[desired_event]
        loaded_state: object = None
        try:
            loaded_state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        prior_state = _matching_prior_state(
            loaded_state,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            publication_id=publication_id,
        )
        self._errors = (
            [dict(item) for item in prior_state.get("publication_errors", ())
             if item.get("operation") not in {
                 "annotate_access_request", "minimize_comment", "dismiss_previous_review",
                 "access_request_state",
             }]
            if prior_state is not None
            else []
        )
        state: dict[str, Any] = {
            "version": _STATE_VERSION,
            "mode": mode,
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "publication_id": publication_id,
            "expected_event": desired_event,
            "expected_review_state": expected_review_state,
            "review_completed": False,
            "changed_files_complete": changed_files_complete,
            "diff_complete": diff_complete,
            "sticky": (
                dict(prior_state.get("sticky") or {})
                if prior_state is not None
                and isinstance(prior_state.get("sticky"), Mapping)
                else {}
            ),
            "notes": [],
            "journal": (
                [dict(item) for item in prior_state.get("journal", ())]
                if prior_state is not None
                else []
            ),
            "publication_errors": self._errors,
        }
        managed: Mapping[str, Any] | None = None
        identity_method = getattr(self.client, "query_pr_identity", None)
        used_managed_identity_fallback = not callable(identity_method)
        identity_operation = "live_identity"
        if not callable(identity_method):
            identity_method = self.client.query_managed_state
            identity_operation = "query_managed_state"
        identity_error_count = len(self._errors)
        live_identity = self._call(
            identity_operation,
            identity_method,
            repo,
            pr_number,
            retry_safe=True,
        )
        if (
            not isinstance(live_identity, Mapping)
            or live_identity.get("head_ref_oid") != head_sha
        ):
            if len(self._errors) == identity_error_count:
                self._errors.append({
                    "operation": "live_identity",
                    "error": (
                        "live pull request identity is unavailable or its head "
                        "does not match the requested publication head"
                    ),
                })
            if mode != "comment":
                state["managed_state_complete"] = False
            state["publication_errors"] = self._errors
            self._write_state(state)
            return state
        if desired_event == "APPROVE":
            repository_name = live_identity.get("repository_full_name")
            base_name = live_identity.get("base_repository_full_name")
            head_name = live_identity.get("head_repository_full_name")
            if (
                repository_name != repo
                or base_name != repo
                or not isinstance(head_name, str)
                or not head_name
            ):
                self._errors.append({
                    "operation": "live_repository_identity",
                    "error": (
                        "live base/head repository identities are unavailable "
                        "or do not match the requested repository"
                    ),
                })
                state["publication_errors"] = self._errors
                self._write_state(state)
                return state
            live_is_fork = head_name != base_name
            if approval_policy.is_fork is None or (
                approval_policy.is_fork != live_is_fork
            ):
                self._errors.append({
                    "operation": "live_repository_identity",
                    "error": (
                        "precheck fork identity does not match the live pull "
                        "request repository identity"
                    ),
                })
                state["publication_errors"] = self._errors
                self._write_state(state)
                return state
        if mode != "comment":
            queried = (
                live_identity
                if used_managed_identity_fallback
                and "pull_request_id" in live_identity
                else self._call(
                    "query_managed_state",
                    self.client.query_managed_state,
                    repo,
                    pr_number,
                    retry_safe=True,
                )
            )
            if not isinstance(queried, Mapping):
                state["notes"] = (
                    [dict(item) for item in prior_state.get("notes", ())]
                    if prior_state is not None
                    else []
                )
                state["managed_state_complete"] = False
                state["publication_errors"] = self._errors
                self._write_state(state)
                return state
            live_head = queried.get("head_ref_oid")
            if live_head != head_sha:
                self._errors.append({
                    "operation": "head_ref_oid",
                    "error": "live pull request head does not match requested publication head",
                })
                state["notes"] = (
                    [dict(item) for item in prior_state.get("notes", ())]
                    if prior_state is not None
                    else []
                )
                state["managed_state_complete"] = False
                state["publication_errors"] = self._errors
                self._write_state(state)
                return state
            managed = queried
            state["managed_state_complete"] = True

            managed_files = managed.get("changed_files")
            if (
                managed.get("changed_files_complete") is not True
                or not isinstance(managed_files, (list, tuple))
                or any(
                    not isinstance(path, str) or _safe_repo_path(path) != path
                    for path in managed_files
                )
            ):
                self._errors.append({
                    "operation": "changed_files_snapshot",
                    "error": "complete live changed-files snapshot is invalid",
                })
                state["notes"] = (
                    [dict(item) for item in prior_state.get("notes", ())]
                    if prior_state is not None
                    else []
                )
                state["publication_errors"] = self._errors
                self._write_state(state)
                return state
            live_files = tuple(managed_files)
            if changed_files_complete and tuple(sorted(files_snapshot)) != tuple(
                sorted(live_files)
            ):
                self._errors.append({
                    "operation": "changed_files_snapshot",
                    "error": "complete caller changed-files identity does not match live pull request",
                })
                state["notes"] = (
                    [dict(item) for item in prior_state.get("notes", ())]
                    if prior_state is not None
                    else []
                )
                state["publication_errors"] = self._errors
                self._write_state(state)
                return state
            files_snapshot = live_files
            state["changed_files_complete"] = True

        if mode == "comment":
            sticky = self._call(
                "update_sticky", self.client.update_sticky, repo, pr_number,
                _handoff_body(handoff, artifact_links),
            )
            if not _valid_comment_result(sticky):
                self._errors.append({
                    "operation": "update_sticky",
                    "error": "sticky publication did not return a valid id and URL",
                })
                state["publication_errors"] = self._errors
                self._write_state(state)
                return state
            state["sticky"] = dict(sticky)
            self._checkpoint(state, "update_sticky", sticky)
            state["review_completed"] = True
            if run_url and (access_policy is not None or allowed_github_repositories):
                access_state = self._call(
                    "access_request_state", self.client.query_managed_state,
                    repo, pr_number, retry_safe=True,
                )
                if isinstance(access_state, Mapping) and access_state.get("head_ref_oid") == head_sha:
                    self._after_publication(
                        state, access_state, repo, pr_number, head_sha, access_policy,
                        allowed_github_repositories, run_url, False,
                    )
                elif access_state is not None:
                    self._errors.append({"operation": "access_request_state", "error": "current head not confirmed for access reconciliation"})
            state["publication_errors"] = self._errors
            self._write_state(state)
            return state

        assert managed is not None
        normalized = tuple(
            _bind_note_publication(
                normalize_note(note, diff_text, files_snapshot), publication_id
            )
            for note in notes_snapshot
        )
        prior_threads: dict[str, Mapping[str, Any]] = {}
        for item in managed.get("threads", ()):
            if not isinstance(item, Mapping) or not item.get("fingerprint"):
                continue
            fingerprint = str(item["fingerprint"])
            generation = item.get("generation", 1)
            previous = prior_threads.get(fingerprint)
            if (
                previous is None
                or isinstance(generation, int)
                and generation > int(previous.get("generation", 1))
            ):
                prior_threads[fingerprint] = item
        prior_general = {
            str(item.get("fingerprint")): item
            for item in managed.get("general_comments", ())
            if isinstance(item, Mapping) and item.get("fingerprint")
        }
        current = {item.fingerprint: item for item in normalized}
        new_thread_notes: list[NormalizedReviewNote] = []
        resolution_sources: dict[tuple[str, int], str] = {}
        all_details_confirmed = True
        failed_associations = set()
        current_targets = set()
        current_fingerprints = set(current)
        for record in review_thread_associations:
            fingerprint = record.get("fingerprint")
            if fingerprint not in current or record.get("kind") not in {"duplicate", "update"}:
                continue
            current_targets.add(record["target"])
            if self._historical_decision(state, record, repo, pr_number, head_sha, association=True):
                del current[fingerprint]
                state["notes"].append({"fingerprint": fingerprint, "id": record["snapshot"]["thread_id"],
                                       "resolution": record["kind"], "confirmed": True, "publication_errors": []})
            else:
                failed_associations.add(fingerprint)
        for record in review_thread_assessments:
            if (record.get("assessment") in {"fixed", "disproven"}
                    and (record["target"] in current_targets or record["snapshot"].get("fingerprint") in current_fingerprints)):
                state.setdefault("publication_warnings", []).append({"operation": "historical_thread",
                    "target": record["target"], "error": "current finding conflicts with historical resolution"})
                continue
            self._historical_decision(state, record, repo, pr_number, head_sha, association=False)

        # A stale critic association must not silently swallow a current finding.
        for fingerprint, note in current.items():
            prior = None if fingerprint in failed_associations else prior_threads.get(fingerprint)
            if prior is not None:
                generation = int(prior.get("generation", 1))
                if prior.get("is_resolved"):
                    if prior.get("resolved_by_publisher"):
                        recurrent = _note_generation(note, generation + 1)
                        if recurrent.anchor is None:
                            prior_comment = prior_general.get(fingerprint)
                            body = (
                                recurrent.markdown
                                + "\n\n> This managed general PR comment cannot be resolved in GitHub. "
                                "Reply or re-review is required to record completion.\n\n"
                                + _general_marker(recurrent)
                            )
                            published = self._call(
                                "upsert_general_comment",
                                self.client.upsert_general_comment,
                                repo,
                                pr_number,
                                prior_comment,
                                body,
                            )
                            if _valid_comment_result(published):
                                self._checkpoint(
                                    state,
                                    "upsert_general_comment",
                                    {
                                        **dict(published),
                                        "fingerprint": fingerprint,
                                        "generation": recurrent.generation,
                                    },
                                )
                            else:
                                all_details_confirmed = False
                                published = None
                                self._errors.append({
                                    "operation": "upsert_general_comment",
                                    "error": "recurrent general comment was not confirmed",
                                })
                            state["notes"].append({
                                "fingerprint": fingerprint,
                                "generation": recurrent.generation,
                                "id": (published or {}).get("id"),
                                "url": (published or {}).get("url"),
                                "anchor_type": "GENERAL",
                                "resolution": (
                                    "open_non_resolvable"
                                    if published is not None
                                    else "publication_failed"
                                ),
                                "resolution_source": "publisher_recurrence",
                                "human_resolved": False,
                                "non_resolvable": True,
                                "publication_errors": (
                                    []
                                    if published is not None
                                    else ["upsert_general_comment"]
                                ),
                            })
                            continue
                        new_thread_notes.append(recurrent)
                        resolution_sources[(fingerprint, generation + 1)] = "publisher_recurrence"
                        continue
                state["notes"].append({
                    "fingerprint": fingerprint, "generation": generation, "id": prior.get("thread_id"),
                    "url": prior.get("url"), "confirmed": True, "publication_errors": [],
                    "anchor_type": prior.get("anchor_type") or (note.anchor.subject_type if note.anchor else None),
                    "resolution": "human_resolved_not_reopened" if prior.get("is_resolved") else "open",
                    "resolution_source": "human" if prior.get("is_resolved") else "existing_thread",
                    "human_resolved": bool(prior.get("is_resolved")),
                })
                continue
            if note.anchor is None:
                prior_comment = prior_general.get(fingerprint)
                access_request = fingerprint.startswith("source_access_request:")
                if access_request and prior_comment and (
                    prior_comment.get("is_minimized")
                    or str(prior_comment.get("body", "")).startswith("<!-- ai-pr-review-access-granted:")
                ):
                    # A later revocation starts a new visible request. Preserve
                    # the previous request and its historical grant verbatim.
                    prior_comment = None
                body = (
                    note.markdown
                    + (
                        "\n\n> A later review will annotate and minimize this request "
                        "when the effective configuration authorizes the requested access.\n\n"
                        if access_request else
                        "\n\n> This managed general PR comment cannot be resolved in GitHub. "
                        "Reply or re-review is required to record completion.\n\n"
                    )
                    + _general_marker(note)
                )
                published = self._call(
                    "upsert_general_comment",
                    self.client.upsert_general_comment,
                    repo,
                    pr_number,
                    prior_comment,
                    body,
                )
                if not _valid_comment_result(published):
                    self._errors.append({
                        "operation": "upsert_general_comment",
                        "error": "general comment response is missing a valid id and URL",
                    })
                    published = None
                    all_details_confirmed = False
                else:
                    self._checkpoint(
                        state,
                        "upsert_general_comment",
                        {**dict(published), "fingerprint": fingerprint},
                    )
                state["notes"].append({
                    "fingerprint": fingerprint,
                    "id": (published or {}).get("id"),
                    "url": (published or {}).get("url"),
                    "anchor_type": "GENERAL",
                    "resolution": (
                        "open_non_resolvable"
                        if published is not None
                        else "publication_failed"
                    ),
                    "human_resolved": False,
                    "non_resolvable": True,
                    "publication_errors": (
                        [] if published is not None
                        else ["upsert_general_comment"]
                    ),
                })
            else:
                new_thread_notes.append(note)
                resolution_sources[(fingerprint, note.generation)] = "new"


        pull_request_id = str(managed.get("pull_request_id") or "")
        review_marker = _publication_marker(publication_id, desired_event)
        review_body = (
            review_marker + "\n" + _handoff_body(handoff, artifact_links)
            + f"\n\nReviewed head: `{head_sha}` · Scope: {approval_policy.effective_scope}."
        )
        receipt = assessment_receipt(review_thread_assessments)
        if receipt:
            review_body += "\n\n" + receipt
        resolution_record = resolution_receipt(state.get("confirmed_historical_resolutions", ()))
        if resolution_record:
            review_body += "\n\n" + resolution_record
        safe_run_url = _canonical_artifact_url(run_url)
        if safe_run_url:
            review_body += f" [Review run](<{safe_run_url}>)"
        completed_review = next((
            item
            for item in managed.get("reviews", ())
            if isinstance(item, Mapping)
            and item.get("state") == expected_review_state
            and review_marker in str(item.get("body") or "")
            and _valid_submitted_review(
                item, review_marker, expected_review_state
            )
        ), None)
        if completed_review is not None:
            if not all_details_confirmed:
                state["review"] = {
                    "id": completed_review["id"],
                    "url": None,
                    "status": "pending_incomplete",
                    "expected_event": desired_event,
                    "safety_reason": "one or more intended details are unconfirmed",
                }
                state["publication_errors"] = self._errors
                state["notes"].sort(
                    key=lambda item: str(item.get("fingerprint", ""))
                )
                self._write_state(state)
                return state
            pre_reuse_state = self._call(
                "pre_submit_head_ref_oid",
                self.client.query_managed_state,
                repo,
                pr_number,
                retry_safe=True,
            )
            if (
                not isinstance(pre_reuse_state, Mapping)
                or pre_reuse_state.get("head_ref_oid") != head_sha
            ):
                self._errors.append({
                    "operation": "pre_submit_head_ref_oid",
                    "error": (
                        "live pull request head could not be confirmed immediately "
                        "before completed review reuse"
                    ),
                })
                state["review"] = {
                    "id": completed_review["id"],
                    "url": None,
                    "status": "pending_incomplete",
                    "expected_event": desired_event,
                    "safety_reason": "live pull request head changed or was not confirmed",
                }
                state["publication_errors"] = self._errors
                state["notes"].sort(
                    key=lambda item: str(item.get("fingerprint", ""))
                )
                self._write_state(state)
                return state
            state["review_completed"] = True
            state["review"] = {
                "id": completed_review["id"],
                "url": completed_review["url"],
                "event": desired_event,
                "safety_reason": "owned specialist review already submitted for this head",
            }
            self._checkpoint(state, "resume_submitted_review", completed_review)
            self._after_publication(
                state, managed, repo, pr_number, head_sha, access_policy,
                allowed_github_repositories, run_url,
                cleanup_previous_reviews and approval_policy.effective_scope == "full",
            )
            state["publication_errors"] = self._errors
            state["notes"].sort(key=lambda item: str(item.get("fingerprint", "")))
            self._write_state(state)
            return state
        review = next((
            item
            for item in managed.get("reviews", ())
            if isinstance(item, Mapping)
            and item.get("state") == "PENDING"
            and review_marker in str(item.get("body") or "")
        ), None)
        review_operation = "resume_pending_review" if review is not None else "create_pending_review"
        if review is None:
            review = self._call(
                "create_pending_review",
                self.client.create_pending_review,
                pull_request_id,
                head_sha,
                review_marker,
            )
            if review is not None and not _valid_pending_review(review, review_marker):
                self._errors.append({
                    "operation": "create_pending_review",
                    "error": "pending review response is missing a valid id and URL",
                })
                review = None
        if review is None:
            reconciled = self._call(
                "reconcile_pending_review",
                self.client.query_managed_state,
                repo,
                pr_number,
                retry_safe=True,
            )
            if isinstance(reconciled, Mapping):
                review = next((
                    item
                    for item in reconciled.get("reviews", ())
                    if isinstance(item, Mapping)
                    and item.get("state") == "PENDING"
                    and review_marker in str(item.get("body") or "")
                ), None)
                if review is not None:
                    review_operation = "reconcile_create_pending_review"
        if review is not None and not _valid_pending_review(review, review_marker):
            self._errors.append({
                "operation": review_operation,
                "error": "pending review response is missing a valid id and URL",
            })
            review = None
        review_id = str((review or {}).get("id") or "")
        if review_id:
            self._checkpoint(state, review_operation, review)
            for note in new_thread_notes:
                variables = build_review_thread_variables(review_id, note)
                created = self._call(
                    "add_review_thread", self.client.add_review_thread, variables
                )
                if created is not None and not _valid_thread_result(created):
                    self._errors.append({
                        "operation": "add_review_thread",
                        "error": "thread response is missing required id, URL, or comment id",
                    })
                    created = None
                if created is None:
                    reconciled = self._call(
                        "reconcile_managed_state",
                        self.client.query_managed_state,
                        repo,
                        pr_number,
                        retry_safe=True,
                    )
                    if isinstance(reconciled, Mapping):
                        match = next((
                            item
                            for item in reconciled.get("threads", ())
                            if isinstance(item, Mapping)
                            and item.get("fingerprint") == note.fingerprint
                            and int(item.get("generation", 1)) == note.generation
                            and item.get("publication_id") == publication_id
                            and item.get("review_id") == review_id
                            and item.get("head_sha") == head_sha
                            and review_marker in str(item.get("review_body") or "")
                            and item.get("review_state") == "PENDING"
                        ), None)
                        if match is not None:
                            created = {
                                "id": match.get("thread_id"),
                                "url": match.get("url"),
                                "comment_id": match.get("comment_id"),
                            }
                            if not _valid_thread_result(created):
                                created = None
                note_errors = ["add_review_thread"] if created is None else []
                detail_confirmed = created is not None
                state["notes"].append({
                    "fingerprint": note.fingerprint,
                    "generation": note.generation,
                    "id": (created or {}).get("id"),
                    "url": (created or {}).get("url"),
                    "comment_id": (created or {}).get("comment_id"),
                    "anchor_type": note.anchor.subject_type if note.anchor else None,
                    "resolution": "open" if detail_confirmed else "publication_failed",
                    "resolution_source": resolution_sources.get(
                        (note.fingerprint, note.generation), "new"
                    ),
                    "human_resolved": False,
                    "confirmed": detail_confirmed,
                    "publication_errors": sorted(set(note_errors)),
                })
                if not detail_confirmed:
                    all_details_confirmed = False
                if created is not None:
                    operation = (
                        "reconcile_add_review_thread"
                        if any(
                            error.get("operation") == "add_review_thread"
                            for error in self._errors
                        )
                        else "add_review_thread"
                    )
                    self._checkpoint(
                        state,
                        operation,
                        {
                            **dict(created),
                            "fingerprint": note.fingerprint,
                            "generation": note.generation,
                            "publication_id": publication_id,
                            "review_id": review_id,
                        },
                    )
            if not all_details_confirmed:
                state["review"] = {
                    "id": review_id,
                    "url": review.get("url"),
                    "status": "pending_incomplete",
                    "expected_event": desired_event,
                    "safety_reason": "one or more intended details are unconfirmed",
                }
                state["publication_errors"] = self._errors
                state["notes"].sort(
                    key=lambda item: str(item.get("fingerprint", ""))
                )
                self._write_state(state)
                return state
            pre_submit_state = self._call(
                "pre_submit_head_ref_oid",
                self.client.query_managed_state,
                repo,
                pr_number,
                retry_safe=True,
            )
            if (
                not isinstance(pre_submit_state, Mapping)
                or pre_submit_state.get("head_ref_oid") != head_sha
                or (desired_event == "APPROVE" and any(item.get("state") == "CHANGES_REQUESTED"
                    for item in pre_submit_state.get("reviews", ())))
            ):
                self._errors.append({
                    "operation": "pre_submit_head_ref_oid",
                    "error": (
                        "live pull request head could not be confirmed immediately "
                        "before review submission"
                    ),
                })
                state["review"] = {
                    "id": review_id,
                    "url": review.get("url"),
                    "status": "pending_incomplete",
                    "expected_event": desired_event,
                    "safety_reason": "live pull request head changed or was not confirmed",
                }
                state["publication_errors"] = self._errors
                state["notes"].sort(
                    key=lambda item: str(item.get("fingerprint", ""))
                )
                self._write_state(state)
                return state
            event, reason = desired_event, event_reason
            # Only attest to a complete inventory when every note became a new,
            # confirmed line thread in this very review (not a general or association).
            if (event == "REQUEST_CHANGES" and notes_snapshot and len(notes_snapshot) == len(state["notes"])
                    and all(item.kind == ReviewNoteKind.FINDING for item in notes_snapshot)
                    and all(item.get("confirmed") and item.get("anchor_type") == "LINE"
                            and item.get("resolution_source") == "new" for item in state["notes"])):
                inventory = _concern_inventory((n["id"], n["fingerprint"]) for n in state["notes"])
                review_body = review_body.replace(review_marker, review_marker + "\n" + inventory, 1)
            submitted = self._call(
                "submit_review",
                self.client.submit_review,
                review_id,
                event,
                review_body,
            )
            if submitted is not None and not _valid_submitted_review(
                submitted, review_marker, expected_review_state
            ):
                self._errors.append({
                    "operation": "submit_review",
                    "error": "submitted review response is missing a valid id and URL",
                })
                submitted = None
            reconciled_submission = False
            if submitted is None:
                refreshed = self._call(
                    "reconcile_submit_review",
                    self.client.query_managed_state,
                    repo,
                    pr_number,
                    retry_safe=True,
                )
                if isinstance(refreshed, Mapping):
                    submitted = next((
                        item
                        for item in refreshed.get("reviews", ())
                        if isinstance(item, Mapping)
                        and item.get("state") == expected_review_state
                        and review_marker in str(item.get("body") or "")
                        and _valid_submitted_review(
                            item, review_marker, expected_review_state
                        )
                    ), None)
                    reconciled_submission = submitted is not None
            if isinstance(submitted, Mapping):
                state["review_completed"] = True
                state["review"] = {
                    "id": review_id,
                    "url": submitted.get("url"),
                    "event": event,
                    "status": "submitted",
                    "safety_reason": reason,
                }
                self._checkpoint(
                    state,
                    "reconcile_submit_review" if reconciled_submission else "submit_review",
                    submitted,
                )
            else:
                state["review"] = {
                    "id": review_id,
                    "url": None,
                    "event": None,
                    "status": "submission_failed",
                    "expected_event": event,
                    "safety_reason": "review submission was not confirmed",
                }
            if state["review_completed"]:
                self._after_publication(
                    state, managed, repo, pr_number, head_sha, access_policy,
                    allowed_github_repositories, run_url,
                    cleanup_previous_reviews and approval_policy.effective_scope == "full",
                )
        state["publication_errors"] = self._errors
        state["notes"].sort(key=lambda item: str(item.get("fingerprint", "")))
        self._write_state(state)
        return state

    def _historical_decision(self, state, record, repo, pr_number, head_sha, *, association):
        snapshot = record["snapshot"]
        identity_keys = ("repository", "pr_number", "head_sha", "thread_id", "comment_id", "origin",
                         "fingerprint", "review_id", "path", "line", "side", "author", "author_type")
        def fresh():
            return next((s for s in self.client.query_review_threads(repo, pr_number, head_sha)
                         if all(s.get(k) == snapshot.get(k) for k in identity_keys)
                         and s.get("is_resolved") is False and s.get("discussion_complete") is True), {})
        try:
            live = fresh()
            # A retry may see exactly the update that the previous attempt posted.
            if (live and association and record.get("kind") == "update"
                    and snapshot.get("discussion_complete") is True
                    and list(live["comments"][:-1]) == list(snapshot["comments"])
                    and live["comments"][-1].get("viewer_owned") is True
                    and live["comments"][-1]["id"] == live.get("last_comment_id")
                    and live["comments"][-1]["body"] == _clean_markdown(record.get("update"))):
                return True
            if (not live or snapshot.get("discussion_complete") is not True
                    or live.get("last_comment_id") != snapshot.get("last_comment_id")
                    or list(live["comments"]) != list(snapshot["comments"])):
                raise ValueError("thread changed, resolved, or discussion incomplete")
            if association and record["kind"] == "duplicate":
                return True
            resolve = (not association and record.get("assessment") in {"fixed", "disproven"}
                       and live.get("origin") == "managed" and live.get("human_reply") is False
                       and bool(record.get("evidence_ids")))
            text = _clean_markdown(record.get("update") or (record.get("reason") if resolve else ""))
            if not text:
                return False
            if any(_clean_markdown(c["body"]) == text for c in live["comments"]):
                return not resolve
            if resolve:
                text += f"\n\n<!-- ai-pr-review-resolution:{live['fingerprint']}:publisher -->"
            reply = self.client.reply_thread(live["comment_id"], text)
            if not _valid_comment_result(reply):
                raise ValueError("thread reply was not confirmed")
            self._checkpoint(state, "historical_thread_reply", reply)
            if resolve:
                after = fresh()
                if (after.get("last_comment_id") != reply["id"] or after.get("human_reply") is not False
                        or list(after["comments"][:-1]) != list(live["comments"]) or after["comments"][-1]["body"] != text):
                    raise ValueError("thread changed after explanatory reply; left unresolved")
                result = self.client.resolve_thread(live["thread_id"])
                if not isinstance(result, Mapping) or result.get("id") != live["thread_id"] or result.get("is_resolved") is not True:
                    raise ValueError("resolution was not confirmed")
                self._checkpoint(state, "resolve_historical_thread", result)
                state.setdefault("confirmed_historical_resolutions", []).append({
                    "thread_id": live["thread_id"], "last_comment_id": after["last_comment_id"],
                    "discussion_digest": _discussion_digest(after),
                })
            return True
        except Exception as exc:
            state.setdefault("publication_warnings", []).append({"operation": "historical_thread",
                "target": record["target"], "error": mask_secrets(str(exc))[:500]})
            return False

    def _after_publication(
        self, state: dict[str, Any], managed: Mapping[str, Any], repo: str,
        pr_number: int, head_sha: str, access_policy: Mapping[str, Any] | ReviewPolicy | None,
        allowed_repositories: Iterable[str], run_url: str, cleanup: bool,
    ) -> None:
        """Update owned historical objects only after the replacement is confirmed."""
        for prior in managed.get("general_comments", ()):
            if not str(prior.get("fingerprint", "")).startswith("source_access_request:"):
                continue
            body = reconcile_access_request_comment(
                str(prior.get("body") or ""), policy=access_policy,
                allowed_github_repositories=allowed_repositories,
                current_repository=repo, run_url=run_url, head_sha=head_sha,
            )
            if body is None:
                continue
            if body != prior.get("body"):
                updated = self._call(
                    "annotate_access_request", self.client.annotate_access_request,
                    repo, pr_number, prior, body,
                )
                if not _valid_comment_result(updated) or updated.get("id") != prior.get("id"):
                    self._errors.append({"operation": "annotate_access_request", "error": "original comment update was not confirmed"})
                    continue
                self._checkpoint(state, "annotate_access_request", updated)
            if not prior.get("is_minimized"):
                self._minimize(state, prior.get("node_id"), "RESOLVED")

        diagnostics = state['previous_review_cleanup'] = {
            'enabled': cleanup,
            'ownership': managed.get('review_ownership_diagnostics', {}),
            'owned_reviews': len(managed.get('reviews', ())),
            'eligible_reviews': 0, 'minimized_reviews': 0, 'failed_minimizations': 0,
            'minimized_handoff_comments': 0,
            'skipped_current_review': 0, 'skipped_pending': 0,
            'skipped_unmanaged': 0, 'skipped_already_minimized': 0,
        }
        if not cleanup:
            diagnostics['skip_reason'] = 'cleanup disabled or review scope is not full'
            return
        cleanup_errors_start = len(self._errors)
        for review in managed.get("reviews", ()):
            if review.get('id') == state['review']['id']:
                diagnostics['skipped_current_review'] += 1
                continue
            if review.get('state') == 'PENDING':
                diagnostics['skipped_pending'] += 1
                continue
            if not str(review.get('body', '')).startswith((
                '<!-- ai-pr-reviewer-specialist:', '<!-- ai-pr-reviewer -->',
            )):
                diagnostics['skipped_unmanaged'] += 1
                continue
            diagnostics['eligible_reviews'] += 1
            # The replacement verdict alone never retires an old block.
            if review.get("state") == "APPROVED" or (
                review.get("state") == "CHANGES_REQUESTED"
                and self._block_explicitly_cleared(state, review, repo, pr_number, head_sha)
            ):
                dismissed = self._call("dismiss_previous_review", self.client.dismiss_review, review["id"])
                if not isinstance(dismissed, Mapping) or dismissed.get("state") != "DISMISSED" or dismissed.get("id") != review["id"]:
                    self._errors.append({"operation": "dismiss_previous_review", "error": "review dismissal was not confirmed"})
            if not review.get("isMinimized"):
                success = self._minimize(state, review.get("id"), "OUTDATED")
                diagnostics['minimized_reviews' if success else 'failed_minimizations'] += 1
            else:
                diagnostics['skipped_already_minimized'] += 1
        for prior in managed.get("handoff_comments", ()):
            if not prior.get("is_minimized"):
                success = self._minimize(state, prior.get("node_id"), "OUTDATED")
                diagnostics['minimized_handoff_comments' if success else 'failed_minimizations'] += 1
        if self._errors[cleanup_errors_start:]:
            state.setdefault("publication_warnings", []).extend(self._errors[cleanup_errors_start:])
            del self._errors[cleanup_errors_start:]

    def _block_explicitly_cleared(self, state, review, repo, pr_number, head_sha):
        confirmations = state.get("confirmed_historical_resolutions", ())
        lines = review["body"].splitlines()
        if len(lines) < 2:
            return False
        marker = re.fullmatch(r"<!-- ai-pr-reviewer-specialist:([0-9a-f]{32}):event=REQUEST_CHANGES -->", lines[0])
        if not marker or not lines[1].startswith("<!-- ai-pr-review-block-inventory:"):
            return False
        live = self._call("refresh_blocking_review", self.client.query_managed_state, repo, pr_number, retry_safe=True)
        if (not live or live.get("head_ref_oid") != head_sha or live.get("repository_full_name") != repo
                or not any(r.get("id") == review["id"] and r.get("body") == review["body"]
                           and r.get("state") == "CHANGES_REQUESTED" for r in live.get("reviews", ()))
                or any(c.get("publication_id") in {None, marker[1]} for c in live.get("general_comments", ()))):
            return False
        children = [t for t in live.get("threads", ()) if t.get("review_id") == review["id"]]
        fixed = {t["thread_id"] for t in children if t.get("confirmed_resolution") is True
                 or any(resolution_is_current(record, t) for record in confirmations)}
        return (bool(children) and all(t.get("thread_id") in fixed and t.get("is_resolved") is True
                                       and t.get("publication_id") == marker[1] for t in children)
                and lines[1] == _concern_inventory((t["thread_id"], t["fingerprint"]) for t in children))

    def _minimize(self, state: dict[str, Any], node_id: object, classifier: str) -> bool:
        if not isinstance(node_id, str) or not node_id:
            self._errors.append({"operation": "minimize_comment", "error": "owned comment node ID is missing"})
            return False
        result = self._call("minimize_comment", self.client.minimize_comment, node_id, classifier)
        if not isinstance(result, Mapping) or result.get("isMinimized") is not True:
            self._errors.append({"operation": "minimize_comment", "error": "comment minimization was not confirmed"})
            return False
        self._checkpoint(state, "minimize_comment", {"node_id": node_id, "classifier": classifier})
        return True


_MANAGED_IDENTITY_QUERY = """
query ManagedReviewIdentity($owner: String!, $name: String!, $number: Int!) {
  viewer { login }
  repository(owner: $owner, name: $name) {
    nameWithOwner
    pullRequest(number: $number) {
      id changedFiles headRefOid
      baseRepository { nameWithOwner }
      headRepository { nameWithOwner }
    }
  }
}
""".strip()

_MANAGED_THREADS_QUERY = """
query ManagedReviewThreads($pullRequestId: ID!, $cursor: String) {
  node(id: $pullRequestId) { ... on PullRequest {
    reviewThreads(first: 100, after: $cursor) {
      nodes { id isResolved subjectType path originalLine line diffSide }
      pageInfo { hasNextPage endCursor }
    }
  } }
}
""".strip()

_MANAGED_THREAD_COMMENTS_QUERY = """
query ManagedThreadComments($threadId: ID!, $cursor: String) {
  node(id: $threadId) { ... on PullRequestReviewThread {
    comments(first: 100, after: $cursor) {
      nodes {
        databaseId url body viewerDidAuthor author { login __typename }
        pullRequestReview { id body state commit { oid } }
      }
      pageInfo { hasNextPage endCursor }
    }
  } }
}
""".strip()

_MANAGED_ISSUE_COMMENTS_QUERY = """
query ManagedIssueComments($pullRequestId: ID!, $cursor: String) {
  node(id: $pullRequestId) { ... on PullRequest {
    comments(first: 100, after: $cursor) {
      nodes { id databaseId url body isMinimized viewerDidAuthor author { login } }
      pageInfo { hasNextPage endCursor }
    }
  } }
}
""".strip()

_MANAGED_REVIEWS_QUERY = """
query ManagedReviews($pullRequestId: ID!, $cursor: String) {
  node(id: $pullRequestId) { ... on PullRequest {
    reviews(first: 100, after: $cursor) {
      nodes { id url body state isMinimized viewerDidAuthor author { login } }
      pageInfo { hasNextPage endCursor }
    }
  } }
}
""".strip()

_CREATE_REVIEW_MUTATION = """
mutation CreatePendingReview($pullRequestId: ID!, $commitOID: GitObjectID!, $body: String!) {
  addPullRequestReview(input: {pullRequestId: $pullRequestId, commitOID: $commitOID, body: $body}) {
    pullRequestReview { id url state body }
  }
}
""".strip()

_ADD_THREAD_MUTATION = """
mutation AddManagedReviewThread($pullRequestReviewId: ID!, $body: String!, $subjectType: PullRequestReviewThreadSubjectType!, $path: String!, $line: Int, $side: DiffSide, $startLine: Int, $startSide: DiffSide) {
  addPullRequestReviewThread(input: {pullRequestReviewId: $pullRequestReviewId, body: $body, subjectType: $subjectType, path: $path, line: $line, side: $side, startLine: $startLine, startSide: $startSide}) {
    thread { id comments(first: 1) { nodes { databaseId url } } }
  }
}
""".strip()

_SUBMIT_REVIEW_MUTATION = """
mutation SubmitManagedReview($pullRequestReviewId: ID!, $event: PullRequestReviewEvent!, $body: String!) {
  submitPullRequestReview(input: {pullRequestReviewId: $pullRequestReviewId, event: $event, body: $body}) {
    pullRequestReview { id url state body }
  }
}
""".strip()

_RESOLVE_THREAD_MUTATION = """
mutation ResolveManagedThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { id isResolved } }
}
""".strip()

_SPECIALIST_HANDOFF_MARKER = "<!-- ai-pr-review-specialist-handoff -->"
_TRUSTED_WORKFLOW_COMMENT_AUTHORS = frozenset({
    # REST represents the workflow actor with the ``[bot]`` suffix while
    # GraphQL exposes the same actor as ``github-actions``.
    "github-actions",
    "github-actions[bot]",
})


class GhReviewClient:
    """Production GitHub client using argv lists and 0600 input files.

    GraphQL variables and all review bodies travel through ``--input`` files;
    no model-derived text is interpolated into a shell command or command argv.
    """

    def __init__(self, *, action_root: str | os.PathLike[str], timeout: int = 60) -> None:
        self.action_root = Path(action_root).resolve()
        self.timeout = max(1, min(int(timeout), 300))
        self._repo_context: tuple[str, int] | None = None
        self._trusted_sticky_comment_ids: dict[tuple[str, int], set[int]] = {}

    def _input_call(self, args: list[str], payload: Mapping[str, Any]) -> dict[str, Any]:
        fd, path = tempfile.mkstemp(prefix="ai-pr-review-publish-", suffix=".json")
        try:
            os.chmod(path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.write("\n")
            argv = gh_argv([*args, "--input", path])
            completed = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=self.timeout
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    mask_secrets(completed.stderr.decode("utf-8", errors="replace"))[:500]
                    or "GitHub publication call failed"
                )
            parsed = json.loads(completed.stdout.decode("utf-8", errors="replace") or "{}")
            if not isinstance(parsed, dict):
                raise RuntimeError("GitHub publication returned a non-object response")
            return parsed
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def _graphql(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        result = self._input_call(
            ["api", "graphql"], {"query": query, "variables": variables}
        )
        if result.get("errors"):
            raise RuntimeError("GitHub GraphQL errors: request was not completed")
        if not isinstance(result.get("data"), dict):
            raise RuntimeError("GitHub GraphQL response is missing data")
        return result

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        parts = repo.split("/")
        if (
            len(parts) != 2
            or any(part in {"", ".", ".."} for part in parts)
            or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts)
        ):
            raise ValueError("repo must be owner/name")
        return parts[0], parts[1]

    def _api_get(self, endpoint: str) -> Any:
        completed = subprocess.run(
            gh_argv(["api", endpoint]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                mask_secrets(completed.stderr.decode("utf-8", errors="replace"))[:500]
                or "GitHub API query failed"
            )
        try:
            return json.loads(completed.stdout.decode("utf-8", errors="replace"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("GitHub API query returned invalid JSON") from exc

    def list_changed_files(self, repo: str, pr_number: int) -> tuple[str, ...]:
        self._split_repo(repo)
        files: list[str] = []
        for page in range(1, 101):
            payload = self._api_get(
                f"repos/{repo}/pulls/{pr_number}/files?per_page=100&page={page}"
            )
            if not isinstance(payload, list) or not all(
                isinstance(item, Mapping) for item in payload
            ):
                raise RuntimeError("changed-files page is not a JSON array of objects")
            for item in payload:
                path = _safe_repo_path(item.get("filename"))
                if path is None:
                    raise RuntimeError("changed-files response contains an invalid filename")
                files.append(path)
            if len(payload) < 100:
                return tuple(files)
        raise RuntimeError("changed-files pagination incomplete: page limit exceeded")

    def update_sticky(
        self,
        repo: str,
        pr_number: int,
        body: str,
        known_comment_id: int | None = None,
    ) -> Mapping[str, Any]:
        self._split_repo(repo)
        sticky_key = (repo, pr_number)
        duplicates: tuple[Mapping[str, Any], ...] = ()
        if known_comment_id is None:
            managed = self._trusted_specialist_handoffs(repo, pr_number)
            existing = max(managed, key=lambda item: item["id"]) if managed else None
            comment_id = existing["id"] if existing is not None else None
            if comment_id is not None:
                self._trusted_sticky_comment_ids.setdefault(sticky_key, set()).add(comment_id)
                duplicates = tuple(sorted(
                    (
                        item for item in managed
                        if item["id"] != comment_id
                    ),
                    key=lambda item: item["id"],
                ))
        else:
            if (
                not isinstance(known_comment_id, int)
                or isinstance(known_comment_id, bool)
                or known_comment_id <= 0
            ):
                raise ValueError("known sticky comment id must be a positive integer")
            if known_comment_id not in self._trusted_sticky_comment_ids.get(
                sticky_key, set()
            ):
                raise ValueError(
                    "known sticky comment id is not trusted for this pull request"
                )
            comment_id = known_comment_id
        if comment_id is None:
            endpoint = f"repos/{repo}/issues/{pr_number}/comments"
            method = "POST"
        else:
            endpoint = f"repos/{repo}/issues/comments/{comment_id}"
            method = "PATCH"
        try:
            result = self._api_write(endpoint, method, {"body": body})
            if method == "POST":
                self._trusted_sticky_comment_ids.setdefault(sticky_key, set()).add(result["id"])
        except Exception:
            reconciled = self.find_specialist_handoff(
                repo, pr_number, expected_body=body
            )
            if reconciled is not None:
                self._trusted_sticky_comment_ids.setdefault(sticky_key, set()).add(
                    reconciled["id"]
                )
                result = reconciled
            else:
                raise
        cleanup_errors = []
        for duplicate in duplicates:
            try:
                self._api_write(
                    f"repos/{repo}/issues/comments/{duplicate['id']}",
                    "DELETE",
                    {},
                )
            except Exception:
                cleanup_errors.append(
                    f"duplicate sticky cleanup failed for comment {duplicate['id']}"
                )
        if cleanup_errors:
            return {**result, "cleanup_errors": tuple(cleanup_errors[:10])}
        return result

    def _api_write(
        self, endpoint: str, method: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        result = self._input_call(["api", endpoint, "--method", method], payload)
        if method == "DELETE":
            return {"deleted": True}
        comment_id = result.get("id")
        url = result.get("html_url")
        if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id <= 0:
            raise RuntimeError("GitHub comment response is missing a valid id")
        if not isinstance(url, str) or not _valid_github_result_url(url):
            raise RuntimeError("GitHub comment response is missing a valid URL")
        return {"id": comment_id, "url": url}

    def find_specialist_handoff(
        self, repo: str, pr_number: int, expected_body: str | None = None
    ) -> Mapping[str, Any] | None:
        matches = self._trusted_specialist_handoffs(
            repo, pr_number, expected_body=expected_body,
        )
        return max(matches, key=lambda item: item["id"]) if matches else None

    def _trusted_specialist_handoffs(
        self, repo: str, pr_number: int, expected_body: str | None = None
    ) -> tuple[Mapping[str, Any], ...]:
        matches = []
        for node, viewer_login in self._owned_issue_comment_nodes(repo, pr_number):
            body = node.get("body")
            author = node.get("author")
            author_login = (
                author.get("login") if isinstance(author, Mapping) else None
            )
            trusted_author = (
                (
                    node.get("viewerDidAuthor") is True
                    and _same_github_login(author_login, viewer_login)
                )
                or author_login in _TRUSTED_WORKFLOW_COMMENT_AUTHORS
            )
            if (
                isinstance(body, str)
                and (
                    body == _SPECIALIST_HANDOFF_MARKER
                    or body.startswith(_SPECIALIST_HANDOFF_MARKER + "\n")
                )
                and (expected_body is None or body == expected_body)
                and trusted_author
            ):
                comment = self._validated_issue_comment(node, label="sticky comment")
                matches.append(comment)
        return tuple(matches)

    @staticmethod
    def _validated_issue_comment(
        node: Mapping[str, Any], *, label: str
    ) -> Mapping[str, Any]:
        comment_id, url = node.get("databaseId"), node.get("url")
        if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id <= 0:
            raise RuntimeError(f"{label} id is invalid")
        if not isinstance(url, str) or not _valid_github_result_url(url):
            raise RuntimeError(f"{label} URL is invalid")
        return {"id": comment_id, "url": url}

    def _owned_issue_comment_nodes(
        self, repo: str, pr_number: int
    ) -> tuple[tuple[Mapping[str, Any], str], ...]:
        owner, name = self._split_repo(repo)
        identity = self._graphql(
            _MANAGED_IDENTITY_QUERY,
            {"owner": owner, "name": name, "number": pr_number},
        )["data"]
        viewer = identity.get("viewer")
        repository = identity.get("repository")
        pr = repository.get("pullRequest") if isinstance(repository, Mapping) else None
        viewer_login = viewer.get("login") if isinstance(viewer, Mapping) else None
        pull_request_id = pr.get("id") if isinstance(pr, Mapping) else None
        repository_full_name = (
            repository.get("nameWithOwner")
            if isinstance(repository, Mapping)
            else None
        )
        if not isinstance(viewer_login, str) or not viewer_login:
            raise RuntimeError("sticky viewer identity is missing")
        if repository_full_name != repo:
            raise RuntimeError("sticky repository identity is invalid")
        if not isinstance(pull_request_id, str) or not pull_request_id:
            raise RuntimeError("sticky pull request id is missing")
        nodes = self._paged_nodes(
            _MANAGED_ISSUE_COMMENTS_QUERY,
            {"pullRequestId": pull_request_id},
            connection_name="comments",
            label="sticky comments",
        )
        return tuple((node, viewer_login) for node in nodes)

    def _find_owned_issue_comment_exact(
        self, repo: str, pr_number: int, expected_body: str
    ) -> Mapping[str, Any] | None:
        matches = []
        for node, viewer_login in self._owned_issue_comment_nodes(repo, pr_number):
            author = node.get("author")
            if (
                node.get("body") == expected_body
                and node.get("viewerDidAuthor") is True
                and isinstance(author, Mapping)
                and _same_github_login(author.get("login"), viewer_login)
            ):
                matches.append(
                    self._validated_issue_comment(node, label="managed issue comment")
                )
        return max(matches, key=lambda item: item["id"]) if matches else None

    @staticmethod
    def _connection(value: object, *, label: str) -> tuple[list[Mapping[str, Any]], bool, str | None]:
        if not isinstance(value, Mapping):
            raise RuntimeError(f"{label} connection is missing")
        nodes = value.get("nodes")
        page_info = value.get("pageInfo")
        if not isinstance(nodes, list) or not all(isinstance(item, Mapping) for item in nodes):
            raise RuntimeError(f"{label} nodes are invalid")
        if not isinstance(page_info, Mapping) or not isinstance(page_info.get("hasNextPage"), bool):
            raise RuntimeError(f"{label} pageInfo is invalid")
        cursor = page_info.get("endCursor")
        if cursor is not None and not isinstance(cursor, str):
            raise RuntimeError(f"{label} cursor is invalid")
        if page_info["hasNextPage"] and not cursor:
            raise RuntimeError(f"{label} incomplete pagination: missing cursor")
        return nodes, page_info["hasNextPage"], cursor

    def _paged_nodes(
        self,
        query: str,
        variables: Mapping[str, Any],
        *,
        connection_name: str,
        label: str,
    ) -> list[Mapping[str, Any]]:
        cursor: str | None = None
        seen: set[str] = set()
        result_nodes: list[Mapping[str, Any]] = []
        for _page in range(100):
            response = self._graphql(query, {**variables, "cursor": cursor})
            node = response["data"].get("node")
            if not isinstance(node, Mapping):
                raise RuntimeError(f"{label} parent node is missing")
            nodes, has_next, next_cursor = self._connection(
                node.get(connection_name), label=label
            )
            result_nodes.extend(nodes)
            if not has_next:
                return result_nodes
            if next_cursor in seen:
                raise RuntimeError(f"{label} incomplete pagination: repeated cursor")
            seen.add(str(next_cursor))
            cursor = next_cursor
        raise RuntimeError(f"{label} incomplete pagination: page limit exceeded")

    def query_pr_identity(self, repo: str, pr_number: int) -> Mapping[str, Any]:
        owner, name = self._split_repo(repo)
        data = self._graphql(
            _MANAGED_IDENTITY_QUERY,
            {"owner": owner, "name": name, "number": pr_number},
        )["data"]
        repository = data.get("repository")
        pr = (
            repository.get("pullRequest")
            if isinstance(repository, Mapping)
            else None
        )
        base_repository = (
            pr.get("baseRepository") if isinstance(pr, Mapping) else None
        )
        head_repository = (
            pr.get("headRepository") if isinstance(pr, Mapping) else None
        )
        result = {
            "head_ref_oid": (
                pr.get("headRefOid") if isinstance(pr, Mapping) else None
            ),
            "repository_full_name": (
                repository.get("nameWithOwner")
                if isinstance(repository, Mapping)
                else None
            ),
            "base_repository_full_name": (
                base_repository.get("nameWithOwner")
                if isinstance(base_repository, Mapping)
                else None
            ),
            "head_repository_full_name": (
                head_repository.get("nameWithOwner")
                if isinstance(head_repository, Mapping)
                else None
            ),
        }
        if (
            result["repository_full_name"] != repo
            or result["base_repository_full_name"] != repo
            or not isinstance(result["head_repository_full_name"], str)
            or not result["head_repository_full_name"]
            or not isinstance(result["head_ref_oid"], str)
            or not re.fullmatch(
                r"(?:[0-9a-f]{40}|[0-9a-f]{64})",
                result["head_ref_oid"],
            )
        ):
            raise RuntimeError("live pull request repository identity is invalid")
        return result

    def query_review_threads(self, repo: str, pr_number: int, head_sha: str) -> tuple[dict, ...]:
        owner, name = self._split_repo(repo)
        def identity():
            data = self._graphql(_MANAGED_IDENTITY_QUERY, {"owner": owner, "name": name, "number": pr_number})["data"]
            repository = data["repository"]
            pr = repository["pullRequest"]
            if (repository["nameWithOwner"] != repo or pr["baseRepository"]["nameWithOwner"] != repo
                    or pr["headRefOid"] != head_sha or not pr["id"] or not data["viewer"]["login"]):
                raise RuntimeError("review-thread snapshot identity changed")
            return pr["id"], data["viewer"]["login"]
        pr_id, viewer = identity()
        snapshots = []
        for thread in self._paged_nodes(_MANAGED_THREADS_QUERY, {"pullRequestId": pr_id},
                                        connection_name="reviewThreads", label="review threads"):
            if thread["isResolved"] is not False or thread.get("subjectType") != "LINE":
                continue
            line = thread.get("originalLine") or thread.get("line")
            if type(line) is not int or line < 1 or not _safe_repo_path(thread.get("path")) or thread.get("diffSide") not in {"LEFT", "RIGHT"}:
                continue
            comments = self._paged_nodes(_MANAGED_THREAD_COMMENTS_QUERY, {"threadId": thread["id"]},
                                         connection_name="comments", label="review thread comments")
            if not comments:
                raise RuntimeError("review thread has no root")
            root = comments[0]
            author = root.get("author") or {}
            marker = _NOTE_GENERATION_MARKER_RE.search(root["body"]) or _NOTE_MARKER_RE.search(root["body"])
            owned = root.get("viewerDidAuthor") is True and _same_github_login(author.get("login"), viewer)
            if marker and owned:
                origin = "managed"
            elif not marker and author.get("__typename") == "User" and author.get("login"):
                origin = "human"
            else:
                continue
            retained = []
            remaining = 16384
            complete = True
            human_reply = False
            for index, item in enumerate(comments):
                if type(item.get("databaseId")) is not int or item["databaseId"] <= 0 or not isinstance(item.get("body"), str):
                    raise RuntimeError("review thread comment is invalid")
                actor = item.get("author") or {}
                resolution = _PUBLISHER_RESOLUTION_RE.search(item["body"])
                publisher_explanation = (resolution and marker and resolution.group(1) == marker.group(1)
                    and item.get("viewerDidAuthor") is True and _same_github_login(actor.get("login"), viewer))
                if index and actor.get("__typename") != "Bot" and not publisher_explanation:
                    human_reply = True
                encoded = mask_secrets(item["body"]).encode("utf-8")
                if len(retained) >= 32 or not remaining:
                    complete = False
                    continue
                body = encoded[:remaining].decode("utf-8", "ignore")
                complete &= len(encoded) <= remaining
                remaining -= min(remaining, len(encoded))
                retained.append({"id": item["databaseId"], "author": actor.get("login") or "", "body": body,
                                 "viewer_owned": item.get("viewerDidAuthor") is True and _same_github_login(actor.get("login"), viewer)})
            review = root.get("pullRequestReview") or {}
            snapshots.append(dict(target=f"H{len(snapshots)+1}", thread_id=thread["id"], comment_id=root["databaseId"],
                                  last_comment_id=comments[-1]["databaseId"], path=thread["path"], line=line, side=thread["diffSide"],
                                  review_id=review.get("id"), review_state=review.get("state"),
                                  original_head_sha=(review.get("commit") or {}).get("oid"),
                                  author=author["login"], author_type=author.get("__typename"), origin=origin,
                                  fingerprint=marker.group(1) if marker else None, repository=repo, pr_number=pr_number,
                                  head_sha=head_sha, discussion_complete=complete, human_reply=human_reply,
                                  comments=retained, is_resolved=False))
        if snapshots:
            reviews = self._paged_nodes(_MANAGED_REVIEWS_QUERY, {"pullRequestId": pr_id},
                                        connection_name="reviews", label="review assessment receipts")
            for review in reviews:
                if (review.get("viewerDidAuthor") is not True
                        or not _same_github_login((review.get("author") or {}).get("login"), viewer)
                        or review.get("state") in {"PENDING", "DISMISSED"}
                        or not str(review.get("body", "")).startswith("<!-- ai-pr-reviewer-specialist:")):
                    continue
                for snapshot in snapshots:
                    prior = receipt_assessment(str(review["body"]), snapshot)
                    if prior:
                        snapshot["prior_assessment"] = prior
        if identity() != (pr_id, viewer):
            raise RuntimeError("review-thread snapshot identity changed")
        return tuple(snapshots)

    def query_managed_state(self, repo: str, pr_number: int) -> Mapping[str, Any]:
        owner, name = self._split_repo(repo)
        self._repo_context = (repo, pr_number)
        identity = self._graphql(
            _MANAGED_IDENTITY_QUERY,
            {"owner": owner, "name": name, "number": pr_number},
        )["data"]
        viewer = identity.get("viewer")
        repository = identity.get("repository")
        pr = repository.get("pullRequest") if isinstance(repository, Mapping) else None
        viewer_login = viewer.get("login") if isinstance(viewer, Mapping) else None
        pull_request_id = pr.get("id") if isinstance(pr, Mapping) else None
        changed_files_count = pr.get("changedFiles") if isinstance(pr, Mapping) else None
        head_ref_oid = pr.get("headRefOid") if isinstance(pr, Mapping) else None
        repository_full_name = (
            repository.get("nameWithOwner")
            if isinstance(repository, Mapping)
            else None
        )
        base_repository = (
            pr.get("baseRepository") if isinstance(pr, Mapping) else None
        )
        head_repository = (
            pr.get("headRepository") if isinstance(pr, Mapping) else None
        )
        if not isinstance(viewer_login, str) or not viewer_login:
            raise RuntimeError("managed-state viewer identity is missing")
        if not isinstance(pull_request_id, str) or not pull_request_id:
            raise RuntimeError("managed-state pull request id is missing")
        if (
            not isinstance(changed_files_count, int)
            or isinstance(changed_files_count, bool)
            or changed_files_count < 0
        ):
            raise RuntimeError("managed-state changed-files count is invalid")
        if not isinstance(head_ref_oid, str) or not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head_ref_oid
        ):
            raise RuntimeError("managed-state head OID is invalid")

        thread_nodes = self._paged_nodes(
            _MANAGED_THREADS_QUERY,
            {"pullRequestId": pull_request_id},
            connection_name="reviewThreads",
            label="review threads",
        )
        threads = []
        for node in thread_nodes:
            thread_id = node.get("id")
            is_resolved = node.get("isResolved")
            if not isinstance(thread_id, str) or not thread_id or not isinstance(is_resolved, bool):
                raise RuntimeError("review thread state is invalid")
            comments = self._paged_nodes(
                _MANAGED_THREAD_COMMENTS_QUERY,
                {"threadId": thread_id},
                connection_name="comments",
                label=f"review thread {thread_id} comments",
            )
            if not comments:
                raise RuntimeError("review thread has no starter comment")
            first = comments[0]
            body = first.get("body")
            if not isinstance(body, str):
                raise RuntimeError("review thread starter body is invalid")
            generation_marker = _NOTE_GENERATION_MARKER_RE.search(body)
            legacy_marker = _NOTE_MARKER_RE.search(body)
            marker = generation_marker or legacy_marker
            if marker is None or first.get("viewerDidAuthor") is not True:
                continue
            author = first.get("author")
            if not isinstance(author, Mapping) or not _same_github_login(author.get("login"), viewer_login):
                continue
            comment_id = first.get("databaseId")
            url = first.get("url")
            if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id <= 0:
                raise RuntimeError("managed review starter id is invalid")
            if not isinstance(url, str) or not _valid_github_result_url(url):
                raise RuntimeError("managed review starter URL is invalid")
            fingerprint = marker.group(1)
            generation = int(generation_marker.group(2)) if generation_marker else 1
            publication_id = generation_marker.group(3) if generation_marker else None
            owning_review = first.get("pullRequestReview")
            if owning_review is not None and not isinstance(owning_review, Mapping):
                raise RuntimeError("managed review comment owner is invalid")
            review_commit = (
                owning_review.get("commit")
                if isinstance(owning_review, Mapping)
                else None
            )
            owned_comments = []
            for item in comments:
                author = item.get("author")
                if not (
                    isinstance(item.get("body"), str)
                    and item.get("viewerDidAuthor") is True
                    and isinstance(author, Mapping)
                    and _same_github_login(author.get("login"), viewer_login)
                ):
                    continue
                owned_comments.append({
                    **self._validated_issue_comment(item, label="managed review comment"),
                    "body": item["body"],
                })
            bodies = [item["body"] for item in owned_comments]
            threads.append({
                "fingerprint": fingerprint,
                "last_comment_id": comments[-1]["databaseId"],
                "discussion_digest": _discussion_digest({"comments": [{
                    "id": item["databaseId"], "author": (item.get("author") or {}).get("login") or "",
                    "body": mask_secrets(item["body"]),
                    "viewer_owned": item.get("viewerDidAuthor") is True
                        and _same_github_login((item.get("author") or {}).get("login"), viewer_login),
                } for item in comments]}),
                "generation": generation,
                "publication_id": publication_id,
                "review_id": (
                    owning_review.get("id")
                    if isinstance(owning_review, Mapping)
                    else None
                ),
                "review_body": (
                    owning_review.get("body")
                    if isinstance(owning_review, Mapping)
                    else None
                ),
                "review_state": (
                    owning_review.get("state")
                    if isinstance(owning_review, Mapping)
                    else None
                ),
                "head_sha": (
                    review_commit.get("oid")
                    if isinstance(review_commit, Mapping)
                    else None
                ),
                "thread_id": thread_id,
                "comment_id": comment_id,
                "url": url,
                "is_resolved": is_resolved,
                "resolved_by_publisher": any(
                    match and match.group(1) == fingerprint
                    for match in (_PUBLISHER_RESOLUTION_RE.search(item) for item in bodies)
                ),
                "owned_comments": tuple(owned_comments),
                "owned_comment_bodies": tuple(bodies),
            })

        comment_nodes = self._paged_nodes(
            _MANAGED_ISSUE_COMMENTS_QUERY,
            {"pullRequestId": pull_request_id},
            connection_name="comments",
            label="pull request comments",
        )
        general = []
        handoff_comments = []
        general_answered: set[str] = set()
        general_answered_identities: set[str] = set()
        for node in comment_nodes:
            body = node.get("body")
            answer = _GENERAL_ANSWER_MARKER_RE.search(body) if isinstance(body, str) else None
            author = node.get("author")
            owned = (
                node.get("viewerDidAuthor") is True
                and isinstance(author, Mapping)
                and _same_github_login(author.get("login"), viewer_login)
            )
            if owned and isinstance(body, str) and body.startswith(_SPECIALIST_HANDOFF_MARKER):
                handoff_comments.append({
                    "node_id": node.get("id"),
                    "is_minimized": node.get("isMinimized") is True,
                })
            if answer is not None and owned:
                if all(answer.group(index) is not None for index in (2, 3, 4)):
                    general_answered_identities.add(_general_identity(
                        answer.group(1),
                        answer.group(3),
                        int(answer.group(2)),
                        answer.group(4),
                    ))
                else:
                    general_answered.add(answer.group(1))
            marker = _GENERAL_MARKER_RE.search(body) if isinstance(body, str) else None
            if (
                marker is None
                or not owned
            ):
                continue
            comment_id = node.get("databaseId")
            url = node.get("url")
            if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id <= 0:
                raise RuntimeError("managed general comment id is invalid")
            if not isinstance(url, str) or not _valid_github_result_url(url):
                raise RuntimeError("managed general comment URL is invalid")
            general.append({
                "fingerprint": marker.group(1),
                "id": comment_id,
                "node_id": node.get("id"),
                "is_minimized": node.get("isMinimized") is True,
                "url": url,
                "body": body,
                "generation": int(marker.group(2)) if marker.group(2) else 1,
                "publication_id": marker.group(3),
                "content_digest": marker.group(4),
            })

        reviews = self._paged_nodes(
            _MANAGED_REVIEWS_QUERY,
            {"pullRequestId": pull_request_id},
            connection_name="reviews",
            label="pull request reviews",
        )
        owned_reviews = []
        ownership_diagnostics = {
            'viewer_login': viewer_login, 'found': len(reviews), 'owned': 0,
            'skipped_viewer_did_not_author': 0, 'skipped_author_mismatch': 0,
            'author_logins': sorted({str(node['author'].get('login', ''))[:100]
                                     for node in reviews if isinstance(node.get('author'), Mapping)})[:10],
        }
        for node in reviews:
            if node.get("viewerDidAuthor") is not True:
                ownership_diagnostics['skipped_viewer_did_not_author'] += 1
                continue
            author = node.get("author")
            if not isinstance(author, Mapping) or not _same_github_login(author.get("login"), viewer_login):
                ownership_diagnostics['skipped_author_mismatch'] += 1
                continue
            review_id, url, body, state = (
                node.get("id"), node.get("url"), node.get("body"), node.get("state")
            )
            if (not all(isinstance(value, str) and value for value in (review_id, url, state))
                    or not isinstance(body, str)):
                raise RuntimeError("managed review object is invalid")
            if not _valid_github_result_url(url) or state not in {
                "PENDING", "COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED",
            }:
                raise RuntimeError("managed review result URL or state is invalid")
            owned_reviews.append(dict(node))
            ownership_diagnostics['owned'] += 1

        confirmed = [record for review in owned_reviews
                     if review.get("state") not in {"PENDING", "DISMISSED"}
                     and str(review.get("body", "")).startswith("<!-- ai-pr-reviewer-specialist:")
                     for record in receipt_resolutions(review["body"])]
        for thread in threads:
            thread["confirmed_resolution"] = thread.get("is_resolved") is True and any(
                resolution_is_current(record, thread) for record in confirmed)
        changed_files = tuple(self.list_changed_files(repo, pr_number))
        if len(changed_files) != changed_files_count:
            raise RuntimeError(
                "changed-files count mismatch between GraphQL and REST snapshots"
            )
        confirmation = self._graphql(
            _MANAGED_IDENTITY_QUERY,
            {"owner": owner, "name": name, "number": pr_number},
        )["data"]
        confirmation_repo = confirmation.get("repository")
        confirmation_pr = (
            confirmation_repo.get("pullRequest")
            if isinstance(confirmation_repo, Mapping)
            else None
        )
        if not isinstance(confirmation_pr, Mapping) or (
            confirmation_pr.get("id") != pull_request_id
            or confirmation_pr.get("changedFiles") != changed_files_count
            or confirmation_pr.get("headRefOid") != head_ref_oid
        ):
            raise RuntimeError("pull request changed during managed-state collection")
        return {
            "pull_request_id": pull_request_id,
            "viewer_login": viewer_login,
            "threads": threads,
            "general_comments": general,
            "handoff_comments": handoff_comments,
            "general_answered_fingerprints": tuple(sorted(general_answered)),
            "general_answered_identities": tuple(sorted(general_answered_identities)),
            "reviews": owned_reviews,
            "review_ownership_diagnostics": ownership_diagnostics,
            "changed_files": changed_files,
            "changed_files_complete": True,
            "changed_files_count": changed_files_count,
            "head_ref_oid": head_ref_oid,
            "repository_full_name": repository_full_name,
            "base_repository_full_name": (
                base_repository.get("nameWithOwner")
                if isinstance(base_repository, Mapping)
                else None
            ),
            "head_repository_full_name": (
                head_repository.get("nameWithOwner")
                if isinstance(head_repository, Mapping)
                else None
            ),
        }

    def reply_thread(self, comment_id: object, body: str) -> Mapping[str, Any]:
        if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id <= 0:
            raise ValueError("thread reply requires a numeric comment id")
        if self._repo_context is None:
            raise RuntimeError("query_managed_state must precede thread replies")
        repo, pr_number = self._repo_context
        return self._api_write(
            f"repos/{repo}/pulls/{pr_number}/comments",
            "POST",
            {"body": body, "in_reply_to": comment_id},
        )

    def resolve_thread(self, thread_id: str) -> Mapping[str, Any]:
        result = self._graphql(_RESOLVE_THREAD_MUTATION, {"threadId": thread_id})
        thread = (((result.get("data") or {}).get("resolveReviewThread") or {}).get("thread") or {})
        if thread.get("id") != thread_id or thread.get("isResolved") is not True:
            raise RuntimeError("resolve mutation did not confirm the requested thread")
        return {"id": thread_id, "is_resolved": True}

    def create_pending_review(
        self, pull_request_id: str, head_sha: str, body: str
    ) -> Mapping[str, Any]:
        result = self._graphql(
            _CREATE_REVIEW_MUTATION,
            {"pullRequestId": pull_request_id, "commitOID": head_sha, "body": body},
        )
        review = (((result.get("data") or {}).get("addPullRequestReview") or {}).get("pullRequestReview") or {})
        if (
            not _valid_review_result(review)
            or review.get("state") != "PENDING"
            or review.get("body") != body
        ):
            raise RuntimeError("pending review response is missing a valid id and URL")
        return review

    def add_review_thread(self, variables: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self._graphql(_ADD_THREAD_MUTATION, variables)
        thread = (((result.get("data") or {}).get("addPullRequestReviewThread") or {}).get("thread") or {})
        comments = ((thread.get("comments") or {}).get("nodes") or [])
        first = comments[0] if comments else {}
        created = {
            "id": thread.get("id"),
            "url": first.get("url"),
            "comment_id": first.get("databaseId"),
        }
        if not _valid_thread_result(created):
            raise RuntimeError("review thread response is missing a valid id, URL, or comment id")
        return created

    def submit_review(self, review_id: str, event: str, body: str) -> Mapping[str, Any]:
        result = self._graphql(
            _SUBMIT_REVIEW_MUTATION,
            {"pullRequestReviewId": review_id, "event": event, "body": body},
        )
        review = (((result.get("data") or {}).get("submitPullRequestReview") or {}).get("pullRequestReview") or {})
        expected_state = _EXPECTED_REVIEW_STATE.get(event)
        if (
            not _valid_review_result(review)
            or review.get("state") != expected_state
            or review.get("body") != body
        ):
            raise RuntimeError("submitted review response did not confirm the expected state")
        return review

    def upsert_general_comment(
        self, repo: str, pr_number: int, prior: Mapping[str, Any] | None, body: str
    ) -> Mapping[str, Any]:
        self._split_repo(repo)
        if prior and isinstance(prior.get("id"), int):
            endpoint = f"repos/{repo}/issues/comments/{prior['id']}"
            method = "PATCH"
        else:
            endpoint = f"repos/{repo}/issues/{pr_number}/comments"
            method = "POST"
        try:
            return self._api_write(endpoint, method, {"body": body})
        except Exception:
            reconciled = self._find_owned_issue_comment_exact(repo, pr_number, body)
            if reconciled is not None:
                return reconciled
            raise

    def annotate_access_request(
        self, repo: str, pr_number: int, prior: Mapping[str, Any], body: str,
    ) -> Mapping[str, Any]:
        self._split_repo(repo)
        comment_id = prior.get("id")
        if type(comment_id) is not int or comment_id <= 0:
            raise ValueError("access request requires its original comment id")
        try:
            result = self._input_call(
                ["api", f"repos/{repo}/issues/comments/{comment_id}", "--method", "PATCH"],
                {"body": body},
            )
            confirmed = {"id": result.get("id"), "url": result.get("html_url")}
            if result.get("body") != body or confirmed["id"] != comment_id or not _valid_comment_result(confirmed):
                raise RuntimeError("access request annotation was not confirmed")
            return confirmed
        except Exception:
            confirmed = self._find_owned_issue_comment_exact(repo, pr_number, body)
            if confirmed is not None and confirmed.get("id") == comment_id:
                return confirmed
            raise

    def minimize_comment(self, node_id: str, classifier: str) -> Mapping[str, Any]:
        if classifier not in {"RESOLVED", "OUTDATED"}:
            raise ValueError("unsupported comment minimization reason")
        result = self._graphql(
            "mutation MinimizeManagedComment($id: ID!, $reason: ReportedContentClassifiers!) { "
            "minimizeComment(input: {subjectId: $id, classifier: $reason}) { "
            "minimizedComment { isMinimized minimizedReason } } }",
            {"id": node_id, "reason": classifier},
        )
        minimized = ((result.get("data") or {}).get("minimizeComment") or {}).get("minimizedComment") or {}
        if minimized.get("isMinimized") is not True or minimized.get("minimizedReason") != classifier.lower():
            raise RuntimeError("comment minimization was not confirmed")
        return minimized

    def dismiss_review(self, review_id: str) -> Mapping[str, Any]:
        result = self._graphql(
            "mutation DismissManagedReview($id: ID!) { dismissPullRequestReview(input: {"
            "pullRequestReviewId: $id, message: \"Superseded by a newer completed AI review.\"}) {"
            "pullRequestReview { id state } } }",
            {"id": review_id},
        )
        review = ((result.get("data") or {}).get("dismissPullRequestReview") or {}).get("pullRequestReview") or {}
        if review.get("id") != review_id or review.get("state") != "DISMISSED":
            raise RuntimeError("review dismissal was not confirmed")
        return review

    def reply_general_comment(
        self, repo: str, pr_number: int, prior: Mapping[str, Any], body: str
    ) -> Mapping[str, Any]:
        prior_url = str(prior.get("url") or "")
        prefix = (
            f"Follow-up to {prior_url}\n\n"
            if _valid_github_result_url(prior_url)
            else ""
        )
        return self.upsert_general_comment(repo, pr_number, None, prefix + body)


__all__ = [
    "GhReviewClient",
    "GitHubReviewPublisher",
    "NormalizedReviewNote",
    "NoteAnchor",
    "PublisherApprovalPolicy",
    "build_review_thread_variables",
    "choose_note_anchor",
    "extract_managed_fingerprint",
    "diff_positions",
    "normalize_note",
]
