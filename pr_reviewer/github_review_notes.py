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

from pr_reviewer.enforcement import RuntimeVerdictPolicyResult
from pr_reviewer.platform import gh_argv
from pr_reviewer.specialist_runtime.types import ReviewHandoff, ReviewNote, ReviewNoteKind
from scripts.redact import mask_secrets
from scripts.sanitize_review_markdown import sanitize_markdown
from scripts.strip_metadata_markers import strip_reserved_markers


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
    r"(?:note|general(?:-answer)?|resolution|status|specialist(?:-handoff)?|run)"
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
    def query_managed_state(self, repo: str, pr_number: int) -> Mapping[str, Any]: ...
    def reply_thread(self, comment_id: object, body: str) -> Mapping[str, Any]: ...
    def resolve_thread(self, thread_id: str) -> Mapping[str, Any]: ...
    def create_pending_review(
        self, pull_request_id: str, head_sha: str, body: str
    ) -> Mapping[str, Any]: ...
    def add_review_thread(self, variables: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def submit_review(self, review_id: str, event: str, body: str) -> Mapping[str, Any]: ...
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
        if not isinstance(handoff, ReviewHandoff):
            raise TypeError("handoff must be a ReviewHandoff")
        if not isinstance(diff_text, str):
            raise TypeError("diff_text must be a string")
        notes_snapshot = tuple(notes)
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
        publication_id = _publication_id(
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            mode=mode,
            event=desired_event,
            policy_result=policy_result,
            notes=notes_snapshot,
            handoff=handoff,
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
            [dict(item) for item in prior_state.get("publication_errors", ())]
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

        sticky_body = _handoff_body(handoff, artifact_links)
        sticky = self._call(
            "update_sticky", self.client.update_sticky, repo, pr_number, sticky_body
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
        if mode == "comment":
            state["review_completed"] = True
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
        legacy_answered_general = {
            str(item) for item in managed.get("general_answered_fingerprints", ())
        }
        answered_general_identities = {
            str(item) for item in managed.get("general_answered_identities", ())
        }
        detail_failures: dict[str, list[str]] = {}
        all_details_confirmed = True

        def reconcile_thread_reply(
            fingerprint: str, generation: int, marker: str, prior: Mapping[str, Any]
        ) -> Mapping[str, Any] | None:
            refreshed = self._call(
                "reconcile_reply_thread",
                self.client.query_managed_state,
                repo,
                pr_number,
                retry_safe=True,
            )
            if not isinstance(refreshed, Mapping):
                return None
            for item in refreshed.get("threads", ()):
                if not isinstance(item, Mapping):
                    continue
                if (
                    item.get("fingerprint") != fingerprint
                    or int(item.get("generation", 1)) != generation
                ):
                    continue
                for comment in item.get("owned_comments", ()):
                    if (
                        isinstance(comment, Mapping)
                        and marker in str(comment.get("body") or "")
                        and _valid_comment_result(comment)
                    ):
                        return comment
                if any(
                    marker in body
                    for body in item.get("owned_comment_bodies", ())
                    if isinstance(body, str)
                ):
                    comment_id = prior.get("comment_id")
                    url = prior.get("url")
                    if (
                        isinstance(comment_id, int)
                        and not isinstance(comment_id, bool)
                        and comment_id > 0
                        and isinstance(url, str)
                        and _valid_github_result_url(url)
                    ):
                        return {"id": comment_id, "url": url}
            return None

        # Same fingerprints are updated in place; resolved human threads stay resolved.
        for fingerprint, note in current.items():
            prior = prior_threads.get(fingerprint)
            if prior is not None:
                generation = int(prior.get("generation", 1))
                owned_bodies = tuple(
                    item for item in prior.get("owned_comment_bodies", ())
                    if isinstance(item, str)
                )
                content_digest = _note_reply_digest(note)
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
                    status_marker = (
                        f"<!-- ai-pr-review-status:{fingerprint}:g{generation}:"
                        f"{head_sha}:content={content_digest}:human-resolved -->"
                    )
                    reply = None
                    status_confirmed = any(
                        status_marker in body for body in owned_bodies
                    )
                    if not status_confirmed:
                        reply = self._call(
                            "reply_thread",
                            self.client.reply_thread,
                            prior.get("comment_id"),
                            "**Re-review status:** Current evidence still references this "
                            "human-resolved thread; it remains resolved and was not reopened.\n\n"
                            + status_marker,
                        )
                        reconciled_reply = False
                        if not _valid_comment_result(reply):
                            reply = reconcile_thread_reply(
                                fingerprint, generation, status_marker, prior
                            )
                            reconciled_reply = _valid_comment_result(reply)
                        if _valid_comment_result(reply):
                            status_confirmed = True
                            self._checkpoint(
                                state,
                                "reconcile_reply_human_resolved_thread"
                                if reconciled_reply
                                else "reply_human_resolved_thread",
                                reply,
                            )
                    if not status_confirmed:
                        all_details_confirmed = False
                        self._errors.append({
                            "operation": "reply_human_resolved_thread",
                            "error": "human-resolved status reply was not confirmed",
                        })
                    state["notes"].append({
                        "fingerprint": fingerprint,
                        "generation": generation,
                        "id": prior.get("thread_id"),
                        "url": prior.get("url"),
                        "reply_id": (reply or {}).get("id"),
                        "anchor_type": prior.get("anchor_type") or (
                            note.anchor.subject_type if note.anchor else None
                        ),
                        "resolution": (
                            "human_resolved_not_reopened"
                            if status_confirmed
                            else "publication_failed"
                        ),
                        "resolution_source": "human",
                        "human_resolved": True,
                        "confirmed": status_confirmed,
                        "publication_errors": (
                            [] if status_confirmed else ["reply_human_resolved_thread"]
                        ),
                    })
                    continue
                status_marker = (
                    f"<!-- ai-pr-review-status:{fingerprint}:g{generation}:{head_sha}:"
                    f"content={content_digest}:open -->"
                )
                reply = None
                status_confirmed = any(status_marker in body for body in owned_bodies)
                if not status_confirmed:
                    reply = self._call(
                        "reply_thread",
                        self.client.reply_thread,
                        prior.get("comment_id"),
                        "**Re-review status:** This note remains open with current evidence.\n\n"
                        + note.markdown + "\n\n" + status_marker,
                    )
                    reconciled_reply = False
                    if not _valid_comment_result(reply):
                        reply = reconcile_thread_reply(
                            fingerprint, generation, status_marker, prior
                        )
                        reconciled_reply = _valid_comment_result(reply)
                    if _valid_comment_result(reply):
                        status_confirmed = True
                        self._checkpoint(
                            state,
                            "reconcile_reply_open_thread"
                            if reconciled_reply
                            else "reply_open_thread",
                            reply,
                        )
                if not status_confirmed:
                    all_details_confirmed = False
                    self._errors.append({
                        "operation": "reply_open_thread",
                        "error": "open-thread status reply was not confirmed",
                    })
                state["notes"].append({
                    "fingerprint": fingerprint,
                    "generation": generation,
                    "id": prior.get("thread_id"),
                    "url": prior.get("url"),
                    "reply_id": (reply or {}).get("id"),
                    "anchor_type": prior.get("anchor_type") or (
                        note.anchor.subject_type if note.anchor else None
                    ),
                    "resolution": "open" if status_confirmed else "publication_failed",
                    "resolution_source": "existing_thread",
                    "human_resolved": False,
                    "confirmed": status_confirmed,
                    "publication_errors": (
                        [] if status_confirmed else ["reply_open_thread"]
                    ),
                })
                continue
            if note.anchor is None:
                prior_comment = prior_general.get(fingerprint)
                body = (
                    note.markdown
                    + "\n\n> This managed general PR comment cannot be resolved in GitHub. "
                    "Reply or re-review is required to record completion.\n\n"
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

        # Missing prior fingerprints were fixed/answered by the current policy result.
        for fingerprint, prior in prior_threads.items():
            if fingerprint in current or prior.get("is_resolved"):
                continue
            generation = int(prior.get("generation", 1))
            resolution_marker = (
                f"<!-- ai-pr-review-resolution:{fingerprint}:g{generation}:"
                f"{head_sha}:publisher -->"
            )
            resolution_body = (
                "**Re-review status:** Fixed or answered in the current review; resolving.\n\n"
                + resolution_marker
            )
            owned_bodies = tuple(
                item for item in prior.get("owned_comment_bodies", ())
                if isinstance(item, str)
            )
            reply = None
            resolution_reply_confirmed = any(
                resolution_marker in body for body in owned_bodies
            )
            if not resolution_reply_confirmed:
                reply = self._call(
                    "reply_thread", self.client.reply_thread, prior.get("comment_id"), resolution_body
                )
                reconciled_reply = False
                if not _valid_comment_result(reply):
                    reply = reconcile_thread_reply(
                        fingerprint, generation, resolution_marker, prior
                    )
                    reconciled_reply = _valid_comment_result(reply)
                if _valid_comment_result(reply):
                    resolution_reply_confirmed = True
                    self._checkpoint(
                        state,
                        "reconcile_reply_resolved_thread"
                        if reconciled_reply
                        else "reply_resolved_thread",
                        reply,
                    )
            if not resolution_reply_confirmed:
                all_details_confirmed = False
                self._errors.append({
                    "operation": "reply_resolved_thread",
                    "error": "owned resolution reply marker was not confirmed",
                })
                state["notes"].append({
                    "fingerprint": fingerprint,
                    "generation": generation,
                    "id": prior.get("thread_id"),
                    "url": prior.get("url"),
                    "reply_id": None,
                    "anchor_type": prior.get("anchor_type"),
                    "resolution": "publication_failed",
                    "human_resolved": False,
                    "resolved_by_publisher": False,
                    "resolution_source": "publisher_failed",
                    "confirmed": False,
                    "publication_errors": ["reply_resolved_thread"],
                })
                continue
            resolved = self._call(
                "resolve_thread", self.client.resolve_thread, str(prior.get("thread_id") or "")
            )
            reconciled_resolution = False
            if not (
                isinstance(resolved, Mapping)
                and resolved.get("id") == prior.get("thread_id")
                and resolved.get("is_resolved") is True
            ):
                refreshed = self._call(
                    "reconcile_resolve_thread",
                    self.client.query_managed_state,
                    repo,
                    pr_number,
                    retry_safe=True,
                )
                if isinstance(refreshed, Mapping):
                    match = next((
                        item
                        for item in refreshed.get("threads", ())
                        if isinstance(item, Mapping)
                        and item.get("fingerprint") == fingerprint
                        and int(item.get("generation", 1)) == generation
                        and item.get("thread_id") == prior.get("thread_id")
                        and item.get("is_resolved") is True
                        and item.get("resolved_by_publisher") is True
                        and any(
                            resolution_marker in body
                            for body in item.get("owned_comment_bodies", ())
                            if isinstance(body, str)
                        )
                    ), None)
                    if match is not None:
                        resolved = {
                            "id": prior.get("thread_id"),
                            "is_resolved": True,
                        }
                        reconciled_resolution = True
            if not (
                isinstance(resolved, Mapping)
                and resolved.get("id") == prior.get("thread_id")
                and resolved.get("is_resolved") is True
            ):
                self._errors.append({
                    "operation": "resolve_thread",
                    "error": "resolve mutation did not confirm the managed thread as resolved",
                })
                resolved = None
                all_details_confirmed = False
            else:
                self._checkpoint(
                    state,
                    "reconcile_resolve_thread" if reconciled_resolution else "resolve_thread",
                    resolved,
                )
            state["notes"].append({
                "fingerprint": fingerprint,
                "generation": generation,
                "id": prior.get("thread_id"),
                "url": prior.get("url"),
                "reply_id": (reply or {}).get("id"),
                "anchor_type": prior.get("anchor_type"),
                "resolution": "resolved" if resolved is not None else "publication_failed",
                "human_resolved": False,
                "resolved_by_publisher": resolved is not None,
                "resolution_source": "publisher" if resolved is not None else "publisher_failed",
                "confirmed": resolved is not None,
                "publication_errors": [] if resolved is not None else ["resolve_thread"],
            })

        for fingerprint, prior in prior_general.items():
            current_note = current.get(fingerprint)
            if current_note is not None and current_note.anchor is None:
                continue
            reply = None
            prior_generation = prior.get("generation", 1)
            if not isinstance(prior_generation, int) or isinstance(
                prior_generation, bool
            ) or prior_generation < 1:
                prior_generation = 1
            prior_publication = prior.get("publication_id")
            has_bound_publication = isinstance(prior_publication, str) and bool(
                re.fullmatch(r"[0-9a-f]{32}", prior_publication)
            )
            if not has_bound_publication:
                prior_publication = publication_id
            prior_content_digest = prior.get("content_digest")
            has_bound_content = isinstance(prior_content_digest, str) and bool(
                re.fullmatch(r"[0-9a-f]{16}", prior_content_digest)
            )
            if not has_bound_content:
                prior_content_digest = hashlib.sha256(
                    str(prior.get("body") or "").encode("utf-8")
                ).hexdigest()[:16]
            answer_identity = _general_identity(
                fingerprint,
                prior_publication,
                prior_generation,
                prior_content_digest,
            )
            answer_confirmed = answer_identity in answered_general_identities
            if not answer_confirmed and (
                not has_bound_publication or not has_bound_content
            ):
                answer_confirmed = fingerprint in legacy_answered_general
            if not answer_confirmed:
                answer_marker = (
                    f"<!-- ai-pr-review-general-answer:{fingerprint} "
                    f"generation={prior_generation} publication={prior_publication} "
                    f"content={prior_content_digest} -->"
                )
                reply = self._call(
                    "reply_general_comment",
                    self.client.reply_general_comment,
                    repo,
                    pr_number,
                    prior,
                    "**Re-review status:** This request is fixed or answered. "
                    "The original general PR comment is non-resolvable and remains as history.\n\n"
                    + answer_marker,
                )
                if _valid_comment_result(reply):
                    answer_confirmed = True
                    self._checkpoint(state, "reply_general_comment", reply)
                else:
                    all_details_confirmed = False
                    detail_failures.setdefault(fingerprint, []).append(
                        "reply_general_comment"
                    )
                    self._errors.append({
                        "operation": "reply_general_comment",
                        "error": "general answer follow-up was not confirmed",
                    })
            if current_note is not None:
                if not answer_confirmed:
                    existing = next((
                        item
                        for item in reversed(state["notes"])
                        if item.get("fingerprint") == fingerprint
                    ), None)
                    if existing is not None:
                        existing["resolution"] = "publication_failed"
                        existing["confirmed"] = False
                        existing["publication_errors"] = sorted(set(
                            list(existing.get("publication_errors", ()))
                            + ["reply_general_comment"]
                        ))
                continue
            state["notes"].append({
                "fingerprint": fingerprint,
                "id": prior.get("id"),
                "url": prior.get("url"),
                "reply_id": (reply or {}).get("id"),
                "anchor_type": "GENERAL",
                "resolution": (
                    "answered_non_resolvable"
                    if answer_confirmed
                    else "publication_failed"
                ),
                "answered": answer_confirmed,
                "confirmed": answer_confirmed,
                "human_resolved": False,
                "non_resolvable": True,
                "publication_errors": (
                    [] if answer_confirmed else ["reply_general_comment"]
                ),
            })

        pull_request_id = str(managed.get("pull_request_id") or "")
        review_marker = _publication_marker(publication_id, desired_event)
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
            refreshed = self._call(
                "refresh_sticky",
                self.client.update_sticky,
                repo,
                pr_number,
                _handoff_body(handoff, artifact_links, completed_review["url"]),
                sticky["id"],
            )
            if _valid_comment_result(refreshed):
                state["sticky"] = dict(refreshed)
                self._checkpoint(state, "refresh_sticky", refreshed)
            elif refreshed is not None:
                self._errors.append({
                    "operation": "refresh_sticky",
                    "error": "sticky refresh did not return a valid id and URL",
                })
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
                note_errors = list(detail_failures.get(note.fingerprint, ()))
                if created is None:
                    note_errors.append("add_review_thread")
                detail_confirmed = created is not None and not note_errors
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
            submitted = self._call(
                "submit_review",
                self.client.submit_review,
                review_id,
                event,
                review_marker
                + (
                    "\nAutomated specialist review notes. "
                    "Detailed findings are in managed threads."
                    if new_thread_notes
                    else ""
                ),
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
            review_url = (submitted or {}).get("url")
            if isinstance(review_url, str) and _valid_github_result_url(review_url):
                refreshed = self._call(
                    "refresh_sticky",
                    self.client.update_sticky,
                    repo,
                    pr_number,
                    _handoff_body(handoff, artifact_links, review_url),
                    sticky["id"],
                )
                if _valid_comment_result(refreshed):
                    state["sticky"] = dict(refreshed)
                    self._checkpoint(state, "refresh_sticky", refreshed)
                elif refreshed is not None:
                    self._errors.append({
                        "operation": "refresh_sticky",
                        "error": "sticky refresh did not return a valid id and URL",
                    })
        state["publication_errors"] = self._errors
        state["notes"].sort(key=lambda item: str(item.get("fingerprint", "")))
        self._write_state(state)
        return state


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
      nodes { id isResolved }
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
        databaseId url body viewerDidAuthor author { login }
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
      nodes { databaseId url body viewerDidAuthor author { login } }
      pageInfo { hasNextPage endCursor }
    }
  } }
}
""".strip()

_MANAGED_REVIEWS_QUERY = """
query ManagedReviews($pullRequestId: ID!, $cursor: String) {
  node(id: $pullRequestId) { ... on PullRequest {
    reviews(first: 100, after: $cursor) {
      nodes { id url body state viewerDidAuthor author { login } }
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
                    and author_login == viewer_login
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
                and author.get("login") == viewer_login
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
            if not isinstance(author, Mapping) or author.get("login") != viewer_login:
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
                    and author.get("login") == viewer_login
                ):
                    continue
                owned_comments.append({
                    **self._validated_issue_comment(item, label="managed review comment"),
                    "body": item["body"],
                })
            bodies = [item["body"] for item in owned_comments]
            threads.append({
                "fingerprint": fingerprint,
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
        general_answered: set[str] = set()
        general_answered_identities: set[str] = set()
        for node in comment_nodes:
            body = node.get("body")
            answer = _GENERAL_ANSWER_MARKER_RE.search(body) if isinstance(body, str) else None
            author = node.get("author")
            owned = (
                node.get("viewerDidAuthor") is True
                and isinstance(author, Mapping)
                and author.get("login") == viewer_login
            )
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
        for node in reviews:
            if node.get("viewerDidAuthor") is not True:
                continue
            author = node.get("author")
            if not isinstance(author, Mapping) or author.get("login") != viewer_login:
                continue
            review_id, url, body, state = (
                node.get("id"), node.get("url"), node.get("body"), node.get("state")
            )
            if not all(isinstance(value, str) and value for value in (review_id, url, body, state)):
                raise RuntimeError("managed review object is invalid")
            if not _valid_github_result_url(url) or state not in {
                "PENDING", "COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED",
            }:
                raise RuntimeError("managed review result URL or state is invalid")
            owned_reviews.append(dict(node))

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
            "general_answered_fingerprints": tuple(sorted(general_answered)),
            "general_answered_identities": tuple(sorted(general_answered_identities)),
            "reviews": owned_reviews,
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
