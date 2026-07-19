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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from pr_reviewer.enforcement import RuntimeVerdictPolicyResult
from pr_reviewer.platform import gh_argv
from pr_reviewer.specialist_runtime.types import ReviewHandoff, ReviewNote, ReviewNoteKind
from scripts.redact import mask_secrets
from scripts.sanitize_review_markdown import sanitize_markdown
from scripts.strip_metadata_markers import strip_reserved_markers


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_NOTE_MARKER_RE = re.compile(r"<!--\s*ai-pr-review-note:([^\s>]+)\s*-->")
_GENERAL_MARKER_RE = re.compile(r"<!--\s*ai-pr-review-general:([^\s>]+)\s*-->")
_PUBLISHER_RESOLUTION_RE = re.compile(
    r"<!--\s*ai-pr-review-resolution:([^\s>]+):publisher\s*-->"
)
_OWN_MARKER_RE = re.compile(
    r"<!--\s*ai-pr-review-(?:note|general|resolution):[^>]*-->", re.IGNORECASE
)
_FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


@dataclass(frozen=True)
class NoteAnchor:
    """A GitHub-resolvable anchor validated against the current PR snapshot."""

    subject_type: str
    path: str
    line: int | None = None
    side: str | None = None


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


@dataclass(frozen=True)
class PublisherApprovalPolicy:
    """The existing native-approval safety gates, evaluated after policy."""

    allow_approve: bool = False
    approve_forks: bool = False
    is_fork: bool = False
    effective_scope: str = "full"
    baseline_clean: bool = False


class ReviewPublishClient(Protocol):
    def update_sticky(self, repo: str, pr_number: int, body: str) -> Mapping[str, Any]: ...
    def query_managed_state(self, repo: str, pr_number: int) -> Mapping[str, Any]: ...
    def reply_thread(self, comment_id: object, body: str) -> Mapping[str, Any]: ...
    def resolve_thread(self, thread_id: str) -> Mapping[str, Any]: ...
    def create_pending_review(self, pull_request_id: str, head_sha: str) -> Mapping[str, Any]: ...
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
    for raw in str(diff_text or "").splitlines():
        if raw.startswith("diff --git "):
            current = None
            in_hunk = False
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                current = None
            else:
                candidate = target[2:] if target.startswith("b/") else target
                current = _safe_repo_path(candidate)
                if current:
                    paths.add(current)
            continue
        match = _HUNK_RE.match(raw)
        if match:
            new_line = int(match.group(1))
            in_hunk = True
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


def legacy_diff_positions(diff_text: str) -> dict[str, dict[int, int]]:
    """Compatibility map for the legacy inline-comment payload builder.

    The specialist publisher uses :func:`choose_note_anchor` and therefore
    only accepts actually changed RIGHT-side lines.  Existing single-review
    callers historically allow context lines too; this isolated adapter keeps
    that behavior until Task 17 removes those callers.
    """

    positions_by_path: dict[str, dict[int, int]] = {}
    current_path: str | None = None
    new_line = 0
    diff_position = 0
    in_hunk = False
    for raw in str(diff_text or "").splitlines():
        if raw.startswith("diff --git "):
            current_path = None
            in_hunk = False
            diff_position = 0
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            current_path = None if target == "/dev/null" else _safe_repo_path(
                target[2:] if target.startswith("b/") else target
            )
            continue
        match = _HUNK_RE.match(raw)
        if match:
            new_line = int(match.group(1))
            in_hunk = True
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

    A note's path or line is a hint, never authority.  LINE is limited to an
    added RIGHT-side line.  FILE requires the path to appear in both supplied
    PR files (when supplied) and the current diff.
    """

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
        return NoteAnchor("LINE", path, line, "RIGHT")
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


def _note_marker(fingerprint: str) -> str:
    return f"<!-- ai-pr-review-note:{fingerprint} -->"


def _general_marker(fingerprint: str) -> str:
    return f"<!-- ai-pr-review-general:{fingerprint} -->"


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
    elif note.anchor.subject_type != "FILE":
        raise ValueError("unsupported review-note anchor")
    return variables


def _valid_artifact_url(value: object) -> bool:
    try:
        parsed = urlsplit(str(value))
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username


def _handoff_body(
    handoff: ReviewHandoff, artifact_links: Sequence[tuple[str, str]]
) -> str:
    if not isinstance(handoff, ReviewHandoff):
        raise TypeError("handoff must be a ReviewHandoff")
    body = _clean_markdown(handoff.markdown)
    links = []
    for label, url in artifact_links[:10]:
        clean_label = " ".join(str(label).split())[:100]
        clean_url = str(url).strip()[:2048]
        if clean_label and _valid_artifact_url(clean_url):
            links.append(f"- [{sanitize_markdown(clean_label)}]({clean_url})")
    if links:
        body = body + "\n\n**Retained review artifacts**\n\n" + "\n".join(links)
    return "<!-- ai-pr-review-specialist-handoff -->\n" + body.strip()


def _native_event(
    policy_result: RuntimeVerdictPolicyResult, approval: PublisherApprovalPolicy
) -> tuple[str, str]:
    if not isinstance(policy_result, RuntimeVerdictPolicyResult):
        raise TypeError("policy_result must be a RuntimeVerdictPolicyResult")
    if policy_result.verdict == "request_changes":
        return "REQUEST_CHANGES", "policy requested changes"
    if policy_result.verdict != "approve":
        raise ValueError("policy verdict must be approve or request_changes")
    if not approval.allow_approve:
        return "REQUEST_CHANGES", "native approval disabled"
    if approval.effective_scope == "incremental" and not approval.baseline_clean:
        return "REQUEST_CHANGES", "incremental approval lacks a clean baseline"
    if approval.is_fork and not approval.approve_forks:
        return "REQUEST_CHANGES", "fork approval disabled"
    return "APPROVE", "approval safety policy passed"


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

    def _call(self, operation: str, function, *args):
        last: Exception | None = None
        for _attempt in range(self.max_attempts):
            try:
                return function(*args)
            except Exception as exc:  # publication failures are persisted separately
                last = exc
        self._errors.append({"operation": operation, "error": mask_secrets(str(last))[:500]})
        return None

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
    ) -> dict[str, Any]:
        if mode not in {"comment", "review_comment", "review_verdict"}:
            raise ValueError("unsupported publish mode")
        if not isinstance(policy_result, RuntimeVerdictPolicyResult):
            raise TypeError("policy_result must be a RuntimeVerdictPolicyResult")
        if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
            raise ValueError("pr_number must be a positive integer")
        self._errors = []
        files_snapshot = tuple(changed_files)
        normalized = tuple(
            normalize_note(note, diff_text, files_snapshot)
            for note in notes
        )
        sticky_body = _handoff_body(handoff, artifact_links)
        sticky = self._call("update_sticky", self.client.update_sticky, repo, pr_number, sticky_body)
        state: dict[str, Any] = {
            "version": 1,
            "mode": mode,
            "repo": repo,
            "pr_number": pr_number,
            "head_sha": head_sha,
            "review_completed": True,
            "sticky": dict(sticky or {}),
            "notes": [],
            "publication_errors": self._errors,
        }
        if mode == "comment":
            self._write_state(state)
            return state

        managed = self._call(
            "query_managed_state", self.client.query_managed_state, repo, pr_number
        ) or {}
        prior_threads = {
            str(item.get("fingerprint")): item
            for item in managed.get("threads", ())
            if isinstance(item, Mapping) and item.get("fingerprint")
        }
        prior_general = {
            str(item.get("fingerprint")): item
            for item in managed.get("general_comments", ())
            if isinstance(item, Mapping) and item.get("fingerprint")
        }
        current = {item.fingerprint: item for item in normalized}
        new_thread_notes: list[NormalizedReviewNote] = []

        # Same fingerprints are updated in place; resolved human threads stay resolved.
        for fingerprint, note in current.items():
            prior = prior_threads.get(fingerprint)
            if prior is not None:
                if prior.get("is_resolved"):
                    state["notes"].append({
                        "fingerprint": fingerprint,
                        "id": prior.get("thread_id"),
                        "url": prior.get("url"),
                        "anchor_type": prior.get("anchor_type") or (
                            note.anchor.subject_type if note.anchor else None
                        ),
                        "resolution": "human_resolved_not_reopened",
                        "human_resolved": not bool(prior.get("resolved_by_publisher")),
                        "publication_errors": [],
                    })
                    continue
                reply = self._call(
                    "reply_thread",
                    self.client.reply_thread,
                    prior.get("comment_id"),
                    "**Re-review status:** This note remains open with current evidence.\n\n"
                    + note.managed_markdown,
                )
                state["notes"].append({
                    "fingerprint": fingerprint,
                    "id": prior.get("thread_id"),
                    "url": prior.get("url"),
                    "reply_id": (reply or {}).get("id"),
                    "anchor_type": prior.get("anchor_type") or (
                        note.anchor.subject_type if note.anchor else None
                    ),
                    "resolution": "open",
                    "human_resolved": False,
                    "publication_errors": [],
                })
                continue
            if note.anchor is None:
                prior_comment = prior_general.get(fingerprint)
                body = (
                    note.markdown
                    + "\n\n> This managed general PR comment cannot be resolved in GitHub. "
                    "Reply or re-review is required to record completion.\n\n"
                    + _general_marker(fingerprint)
                )
                published = self._call(
                    "upsert_general_comment",
                    self.client.upsert_general_comment,
                    repo,
                    pr_number,
                    prior_comment,
                    body,
                )
                state["notes"].append({
                    "fingerprint": fingerprint,
                    "id": (published or prior_comment or {}).get("id"),
                    "url": (published or prior_comment or {}).get("url"),
                    "anchor_type": "GENERAL",
                    "resolution": "open_non_resolvable",
                    "human_resolved": False,
                    "non_resolvable": True,
                    "publication_errors": [],
                })
            else:
                new_thread_notes.append(note)

        # Missing prior fingerprints were fixed/answered by the current policy result.
        for fingerprint, prior in prior_threads.items():
            if fingerprint in current or prior.get("is_resolved"):
                continue
            resolution_body = (
                "**Re-review status:** Fixed or answered in the current review; resolving.\n\n"
                f"<!-- ai-pr-review-resolution:{fingerprint}:publisher -->"
            )
            reply = self._call(
                "reply_thread", self.client.reply_thread, prior.get("comment_id"), resolution_body
            )
            resolved = self._call(
                "resolve_thread", self.client.resolve_thread, str(prior.get("thread_id") or "")
            )
            state["notes"].append({
                "fingerprint": fingerprint,
                "id": prior.get("thread_id"),
                "url": prior.get("url"),
                "reply_id": (reply or {}).get("id"),
                "anchor_type": prior.get("anchor_type"),
                "resolution": "resolved" if resolved is not None else "resolution_failed",
                "human_resolved": False,
                "resolved_by_publisher": resolved is not None,
                "publication_errors": [],
            })

        for fingerprint, prior in prior_general.items():
            if fingerprint in current:
                continue
            reply = self._call(
                "reply_general_comment",
                self.client.reply_general_comment,
                repo,
                pr_number,
                prior,
                "**Re-review status:** This request is fixed or answered. "
                "The original general PR comment is non-resolvable and remains as history.",
            )
            state["notes"].append({
                "fingerprint": fingerprint,
                "id": prior.get("id"),
                "url": prior.get("url"),
                "reply_id": (reply or {}).get("id"),
                "anchor_type": "GENERAL",
                "resolution": "answered_non_resolvable",
                "human_resolved": False,
                "non_resolvable": True,
                "publication_errors": [],
            })

        pull_request_id = str(managed.get("pull_request_id") or "")
        review = self._call(
            "create_pending_review",
            self.client.create_pending_review,
            pull_request_id,
            head_sha,
        )
        review_id = str((review or {}).get("id") or "")
        if review_id:
            for note in new_thread_notes:
                variables = build_review_thread_variables(review_id, note)
                created = self._call(
                    "add_review_thread", self.client.add_review_thread, variables
                )
                state["notes"].append({
                    "fingerprint": note.fingerprint,
                    "id": (created or {}).get("id"),
                    "url": (created or {}).get("url"),
                    "comment_id": (created or {}).get("comment_id"),
                    "anchor_type": note.anchor.subject_type if note.anchor else None,
                    "resolution": "open" if created is not None else "publication_failed",
                    "human_resolved": False,
                    "publication_errors": [] if created is not None else ["add_review_thread"],
                })
            if mode == "review_comment":
                event, reason = "COMMENT", "non-verdict specialist review"
            else:
                event, reason = _native_event(policy_result, approval_policy)
            submitted = self._call(
                "submit_review",
                self.client.submit_review,
                review_id,
                event,
                "Automated specialist review notes. Detailed findings are in managed threads.",
            )
            state["review"] = {
                "id": review_id,
                "url": (submitted or review or {}).get("url"),
                "event": event,
                "safety_reason": reason,
            }
        state["publication_errors"] = self._errors
        state["notes"].sort(key=lambda item: str(item.get("fingerprint", "")))
        self._write_state(state)
        return state


_MANAGED_STATE_QUERY = """
query ManagedReviewNotes($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      id
      reviewThreads(first: 100) {
        nodes {
          id isResolved
          comments(first: 100) { nodes { databaseId url body } }
        }
      }
      comments(first: 100) { nodes { databaseId url body } }
    }
  }
}
""".strip()

_CREATE_REVIEW_MUTATION = """
mutation CreatePendingReview($pullRequestId: ID!, $commitOID: GitObjectID!) {
  addPullRequestReview(input: {pullRequestId: $pullRequestId, commitOID: $commitOID}) {
    pullRequestReview { id url }
  }
}
""".strip()

_ADD_THREAD_MUTATION = """
mutation AddManagedReviewThread($pullRequestReviewId: ID!, $body: String!, $subjectType: PullRequestReviewThreadSubjectType!, $path: String!, $line: Int, $side: DiffSide) {
  addPullRequestReviewThread(input: {pullRequestReviewId: $pullRequestReviewId, body: $body, subjectType: $subjectType, path: $path, line: $line, side: $side}) {
    thread { id comments(first: 1) { nodes { databaseId url } } }
  }
}
""".strip()

_SUBMIT_REVIEW_MUTATION = """
mutation SubmitManagedReview($pullRequestReviewId: ID!, $event: PullRequestReviewEvent!, $body: String!) {
  submitPullRequestReview(input: {pullRequestReviewId: $pullRequestReviewId, event: $event, body: $body}) {
    pullRequestReview { id url }
  }
}
""".strip()

_RESOLVE_THREAD_MUTATION = """
mutation ResolveManagedThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { id isResolved } }
}
""".strip()


class GhReviewClient:
    """Production GitHub client using argv lists and 0600 input files.

    GraphQL variables and all review bodies travel through ``--input`` files;
    no model-derived text is interpolated into a shell command or command argv.
    """

    def __init__(self, *, action_root: str | os.PathLike[str], timeout: int = 60) -> None:
        self.action_root = Path(action_root).resolve()
        self.timeout = max(1, min(int(timeout), 300))
        self._repo_context: tuple[str, int] | None = None

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
        return self._input_call(["api", "graphql"], {"query": query, "variables": variables})

    @staticmethod
    def _split_repo(repo: str) -> tuple[str, str]:
        parts = repo.split("/")
        if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", p) for p in parts):
            raise ValueError("repo must be owner/name")
        return parts[0], parts[1]

    def update_sticky(self, repo: str, pr_number: int, body: str) -> Mapping[str, Any]:
        self._split_repo(repo)
        fd, path = tempfile.mkstemp(prefix="ai-pr-review-handoff-", suffix=".md")
        try:
            os.chmod(path, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(body)
                handle.write("\n")
            helper = self.action_root / "scripts" / "publish_helpers.sh"
            command = 'source "$1"; platform_comment_sticky "$2" "$3" "$4"'
            completed = subprocess.run(
                ["bash", "-c", command, "publish-specialist", str(helper), repo, str(pr_number), path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.timeout,
            )
            if completed.returncode != 0:
                raise RuntimeError(mask_secrets(completed.stderr.decode(errors="replace"))[:500])
            return {}
        finally:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def query_managed_state(self, repo: str, pr_number: int) -> Mapping[str, Any]:
        owner, name = self._split_repo(repo)
        self._repo_context = (repo, pr_number)
        result = self._graphql(
            _MANAGED_STATE_QUERY, {"owner": owner, "name": name, "number": pr_number}
        )
        pr = (((result.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
        threads = []
        for node in ((pr.get("reviewThreads") or {}).get("nodes") or []):
            comments = ((node or {}).get("comments") or {}).get("nodes") or []
            first = comments[0] if comments else {}
            bodies = [str(item.get("body") or "") for item in comments if isinstance(item, Mapping)]
            marker = next((_NOTE_MARKER_RE.search(body) for body in bodies if _NOTE_MARKER_RE.search(body)), None)
            if marker is None:
                continue
            fingerprint = marker.group(1)
            threads.append({
                "fingerprint": fingerprint,
                "thread_id": node.get("id"),
                "comment_id": first.get("databaseId"),
                "url": first.get("url"),
                "is_resolved": bool(node.get("isResolved")),
                "resolved_by_publisher": any(
                    match and match.group(1) == fingerprint
                    for match in (_PUBLISHER_RESOLUTION_RE.search(body) for body in bodies)
                ),
            })
        general = []
        for node in ((pr.get("comments") or {}).get("nodes") or []):
            if not isinstance(node, Mapping):
                continue
            marker = _GENERAL_MARKER_RE.search(str(node.get("body") or ""))
            if marker:
                general.append({
                    "fingerprint": marker.group(1),
                    "id": node.get("databaseId"),
                    "url": node.get("url"),
                })
        return {"pull_request_id": pr.get("id"), "threads": threads, "general_comments": general}

    def reply_thread(self, comment_id: object, body: str) -> Mapping[str, Any]:
        if not isinstance(comment_id, int) or isinstance(comment_id, bool) or comment_id <= 0:
            raise ValueError("thread reply requires a numeric comment id")
        if self._repo_context is None:
            raise RuntimeError("query_managed_state must precede thread replies")
        repo, pr_number = self._repo_context
        result = self._input_call(
            ["api", f"repos/{repo}/pulls/{pr_number}/comments", "--method", "POST"],
            {"body": body, "in_reply_to": comment_id},
        )
        return {"id": result.get("id"), "url": result.get("html_url")}

    def resolve_thread(self, thread_id: str) -> Mapping[str, Any]:
        result = self._graphql(_RESOLVE_THREAD_MUTATION, {"threadId": thread_id})
        return (((result.get("data") or {}).get("resolveReviewThread") or {}).get("thread") or {})

    def create_pending_review(self, pull_request_id: str, head_sha: str) -> Mapping[str, Any]:
        result = self._graphql(
            _CREATE_REVIEW_MUTATION,
            {"pullRequestId": pull_request_id, "commitOID": head_sha},
        )
        return (((result.get("data") or {}).get("addPullRequestReview") or {}).get("pullRequestReview") or {})

    def add_review_thread(self, variables: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self._graphql(_ADD_THREAD_MUTATION, variables)
        thread = (((result.get("data") or {}).get("addPullRequestReviewThread") or {}).get("thread") or {})
        comments = ((thread.get("comments") or {}).get("nodes") or [])
        first = comments[0] if comments else {}
        return {"id": thread.get("id"), "url": first.get("url"), "comment_id": first.get("databaseId")}

    def submit_review(self, review_id: str, event: str, body: str) -> Mapping[str, Any]:
        result = self._graphql(
            _SUBMIT_REVIEW_MUTATION,
            {"pullRequestReviewId": review_id, "event": event, "body": body},
        )
        return (((result.get("data") or {}).get("submitPullRequestReview") or {}).get("pullRequestReview") or {})

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
        result = self._input_call(["api", endpoint, "--method", method], {"body": body})
        return {"id": result.get("id"), "url": result.get("html_url")}

    def reply_general_comment(
        self, repo: str, pr_number: int, prior: Mapping[str, Any], body: str
    ) -> Mapping[str, Any]:
        prior_url = str(prior.get("url") or "")
        prefix = f"Follow-up to {prior_url}\n\n" if _valid_artifact_url(prior_url) else ""
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
    "legacy_diff_positions",
    "normalize_note",
]
