"""Specialist GitHub review-note anchoring and publication lifecycle."""

from __future__ import annotations

import json
import os
import stat
import types
from pathlib import Path

from pr_reviewer.enforcement import RuntimeVerdictPolicyResult
from pr_reviewer.github_review_notes import (
    GhReviewClient,
    GitHubReviewPublisher,
    PublisherApprovalPolicy,
    build_review_thread_variables,
    choose_note_anchor,
    normalize_note,
)
from pr_reviewer.specialist_runtime.types import ReviewHandoff, ReviewNote, ReviewNoteKind


DIFF = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -5,2 +5,3 @@
 context
+changed
 context
diff --git a/b.py b/b.py
--- a/b.py
+++ b/b.py
@@ -1 +1 @@
-old
+new
"""


def _note(
    fingerprint: str = "fp-1",
    *,
    kind: ReviewNoteKind = ReviewNoteKind.FINDING,
    file: str | None = "a.py",
    line: int | None = 6,
    markdown: str = "Detailed claim",
) -> ReviewNote:
    return ReviewNote(
        kind=kind,
        fingerprint=fingerprint,
        markdown=markdown,
        file=file,
        line=line,
        severity="major" if kind is ReviewNoteKind.FINDING else None,
    )


def test_anchor_prefers_changed_right_line_then_changed_file():
    assert choose_note_anchor(_note(line=6), DIFF, ("a.py", "b.py")).subject_type == "LINE"
    anchor = choose_note_anchor(_note(line=99), DIFF, ("a.py", "b.py"))
    assert anchor.subject_type == "FILE"
    assert anchor.path == "a.py"


def test_anchor_rejects_context_line_and_untrusted_note_path():
    assert choose_note_anchor(_note(line=5), DIFF, ("a.py",)).subject_type == "FILE"
    assert choose_note_anchor(_note(file="not-changed.py"), DIFF, ("a.py",)) is None
    assert choose_note_anchor(_note(file="../a.py"), DIFF, ("a.py",)) is None


def test_file_anchor_can_use_current_files_snapshot_when_diff_is_truncated():
    anchor = choose_note_anchor(_note(file="large/generated.py", line=50), "", (
        "large/generated.py",
    ))
    assert anchor.subject_type == "FILE"
    assert anchor.path == "large/generated.py"


def test_unanchored_finding_becomes_non_actionable_verification_request():
    normalized = normalize_note(_note(file=None, line=None), DIFF, ("a.py",))
    assert normalized.kind is ReviewNoteKind.VERIFICATION_REQUEST
    assert normalized.anchor is None
    assert normalized.actionable is False
    assert "non-actionable" in normalized.markdown.lower()
    assert "verification request" in normalized.markdown.lower()


def test_graphql_variables_are_exact_for_line_and_file_anchors():
    line_note = normalize_note(_note(line=6), DIFF, ("a.py",))
    file_note = normalize_note(_note("fp-file", file="b.py", line=99), DIFF, ("b.py",))

    assert build_review_thread_variables("review-id", line_note) == {
        "pullRequestReviewId": "review-id",
        "body": line_note.managed_markdown,
        "subjectType": "LINE",
        "path": "a.py",
        "line": 6,
        "side": "RIGHT",
    }
    assert build_review_thread_variables("review-id", file_note) == {
        "pullRequestReviewId": "review-id",
        "body": file_note.managed_markdown,
        "subjectType": "FILE",
        "path": "b.py",
    }


class _FakeClient:
    def __init__(self, managed_state=None):
        self.calls: list[tuple] = []
        self.managed_state = managed_state or {
            "pull_request_id": "PR-node",
            "threads": [],
            "general_comments": [],
        }

    def update_sticky(self, repo, pr_number, body):
        self.calls.append(("sticky", repo, pr_number, body))
        return {"id": "sticky-id", "url": "https://example/sticky"}

    def query_managed_state(self, repo, pr_number):
        self.calls.append(("query", repo, pr_number))
        return self.managed_state

    def reply_thread(self, comment_id, body):
        self.calls.append(("reply", comment_id, body))
        return {"id": f"reply-{comment_id}", "url": f"https://example/{comment_id}/reply"}

    def resolve_thread(self, thread_id):
        self.calls.append(("resolve", thread_id))
        return {"id": thread_id, "is_resolved": True}

    def create_pending_review(self, pull_request_id, head_sha):
        self.calls.append(("pending", pull_request_id, head_sha))
        return {"id": "review-id", "url": "https://example/review"}

    def add_review_thread(self, variables):
        self.calls.append(("add", variables))
        fp = variables["body"].split("ai-pr-review-note:", 1)[1].split()[0]
        return {
            "id": f"thread-{fp}",
            "url": f"https://example/{fp}",
            "comment_id": f"comment-{fp}",
        }

    def submit_review(self, review_id, event, body):
        self.calls.append(("submit", review_id, event, body))
        return {"id": review_id, "url": "https://example/submitted"}

    def upsert_general_comment(self, repo, pr_number, prior, body):
        self.calls.append(("general", repo, pr_number, prior, body))
        return {"id": "general-id", "url": "https://example/general"}

    def reply_general_comment(self, repo, pr_number, prior, body):
        self.calls.append(("general_reply", repo, pr_number, prior, body))
        return {"id": "general-reply", "url": "https://example/general-reply"}


def _publish(tmp_path, client, *, mode="review_comment", notes=(), verdict="approve", approval=None):
    state_path = tmp_path / "publication-state.json"
    publisher = GitHubReviewPublisher(client, state_path=state_path, max_attempts=1)
    result = publisher.publish(
        mode=mode,
        handoff=ReviewHandoff(markdown="Sparse handoff"),
        notes=notes,
        diff_text=DIFF,
        changed_files=("a.py", "b.py"),
        policy_result=RuntimeVerdictPolicyResult(verdict=verdict, source="policy"),
        repo="owner/repo",
        pr_number=17,
        head_sha="abc123",
        artifact_links=(("Review artifact", "https://example/artifact"),),
        approval_policy=approval or PublisherApprovalPolicy(),
    )
    return result, json.loads(state_path.read_text(encoding="utf-8"))


def test_comment_mode_only_updates_sparse_handoff_and_artifacts(tmp_path):
    client = _FakeClient()
    result, state = _publish(tmp_path, client, mode="comment", notes=(_note(markdown="SECRET DETAIL"),))

    assert [call[0] for call in client.calls] == ["sticky"]
    assert "Sparse handoff" in client.calls[0][3]
    assert client.calls[0][3].startswith("<!-- ai-pr-review-specialist-handoff -->")
    assert "https://example/artifact" in client.calls[0][3]
    assert "SECRET DETAIL" not in client.calls[0][3]
    assert result["mode"] == state["mode"] == "comment"


def test_review_comment_lifecycle_order_and_state(tmp_path):
    client = _FakeClient(
        {
            "pull_request_id": "PR-node",
            "threads": [
                {
                    "fingerprint": "fp-open",
                    "thread_id": "thread-open",
                    "comment_id": "comment-open",
                    "url": "https://example/open",
                    "is_resolved": False,
                },
                {
                    "fingerprint": "fp-fixed",
                    "thread_id": "thread-fixed",
                    "comment_id": "comment-fixed",
                    "url": "https://example/fixed",
                    "is_resolved": False,
                },
            ],
            "general_comments": [],
        }
    )
    notes = (
        _note("fp-open", markdown="Current evidence"),
        _note("fp-line", line=6),
        _note("fp-file", file="b.py", line=99),
    )
    _result, state = _publish(tmp_path, client, notes=notes)

    assert [call[0] for call in client.calls] == [
        "sticky",
        "query",
        "reply",
        "reply",
        "resolve",
        "pending",
        "add",
        "add",
        "submit",
    ]
    assert client.calls[-1][2] == "COMMENT"
    by_fp = {entry["fingerprint"]: entry for entry in state["notes"]}
    assert by_fp["fp-fixed"]["resolution"] == "resolved"
    assert by_fp["fp-line"]["anchor_type"] == "LINE"
    assert by_fp["fp-file"]["anchor_type"] == "FILE"
    assert state["publication_errors"] == []


def test_publisher_materializes_current_files_once_for_all_notes(tmp_path):
    client = _FakeClient()
    publisher = GitHubReviewPublisher(
        client, state_path=tmp_path / "state.json", max_attempts=1
    )
    publisher.publish(
        mode="review_comment",
        handoff=ReviewHandoff(markdown="Sparse handoff"),
        notes=(_note("fp-a"), _note("fp-b", file="b.py", line=2)),
        diff_text=DIFF,
        changed_files=(path for path in ("a.py", "b.py")),
        policy_result=RuntimeVerdictPolicyResult(verdict="approve", source="policy"),
        repo="owner/repo",
        pr_number=17,
        head_sha="abc123",
    )
    adds = [call for call in client.calls if call[0] == "add"]
    assert [call[1]["path"] for call in adds] == ["a.py", "b.py"]


def test_human_resolved_thread_is_not_reopened(tmp_path):
    client = _FakeClient(
        {
            "pull_request_id": "PR-node",
            "threads": [{
                "fingerprint": "fp-1",
                "thread_id": "thread-human",
                "comment_id": "comment-human",
                "url": "https://example/human",
                "is_resolved": True,
                "resolved_by_publisher": False,
            }],
            "general_comments": [],
        }
    )
    _result, state = _publish(tmp_path, client, notes=(_note(),))

    assert not any(call[0] in {"reply", "resolve", "add"} for call in client.calls)
    assert state["notes"][0]["human_resolved"] is True
    assert state["notes"][0]["resolution"] == "human_resolved_not_reopened"


def test_unanchored_requests_use_explicitly_non_resolvable_general_comment(tmp_path):
    client = _FakeClient()
    request = _note(
        "fp-general",
        kind=ReviewNoteKind.SOURCE_ACCESS_REQUEST,
        file=None,
        line=None,
        markdown="Please grant access",
    )
    _result, state = _publish(tmp_path, client, notes=(request,))

    assert [call[0] for call in client.calls] == [
        "sticky", "query", "general", "pending", "submit"
    ]
    assert "cannot be resolved" in client.calls[2][-1].lower()
    assert state["notes"][0]["non_resolvable"] is True
    assert state["notes"][0]["anchor_type"] == "GENERAL"


def test_review_verdict_uses_typed_policy_and_existing_approval_guards(tmp_path):
    client = _FakeClient()
    _publish(
        tmp_path,
        client,
        mode="review_verdict",
        verdict="approve",
        approval=PublisherApprovalPolicy(
            allow_approve=True,
            is_fork=True,
            approve_forks=False,
            effective_scope="full",
            baseline_clean=True,
        ),
    )
    assert [call for call in client.calls if call[0] == "submit"][0][2] == "REQUEST_CHANGES"


def test_publication_errors_are_persisted_separately_without_analysis_retry(tmp_path):
    class FailingClient(_FakeClient):
        def add_review_thread(self, variables):
            self.calls.append(("add", variables))
            raise RuntimeError("publication unavailable")

    client = FailingClient()
    _result, state = _publish(tmp_path, client, notes=(_note("fp-fail"),))

    assert [call[0] for call in client.calls].count("add") == 1
    assert state["review_completed"] is True
    assert state["publication_errors"][0]["operation"] == "add_review_thread"
    assert "publication unavailable" in state["publication_errors"][0]["error"]


def test_gh_client_uses_0600_input_files_and_keeps_note_text_out_of_argv(
    tmp_path, monkeypatch
):
    captures = []

    def fake_run(argv, **_kwargs):
        payload_path = argv[argv.index("--input") + 1]
        captures.append({
            "argv": list(argv),
            "path": payload_path,
            "mode": stat.S_IMODE(os.stat(payload_path).st_mode),
            "payload": json.loads(Path(payload_path).read_text(encoding="utf-8")),
        })
        if argv[:3] == ["gh", "api", "graphql"]:
            response = {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "id": "PR-node",
                            "reviewThreads": {"nodes": []},
                            "comments": {"nodes": []},
                        }
                    }
                }
            }
        else:
            response = {"id": 12, "html_url": "https://example/reply"}
        return types.SimpleNamespace(
            returncode=0, stdout=json.dumps(response).encode(), stderr=b""
        )

    monkeypatch.setattr("pr_reviewer.github_review_notes.subprocess.run", fake_run)
    client = GhReviewClient(action_root=tmp_path)
    client.query_managed_state("owner/repo", 17)
    client.reply_thread(123, "MODEL-CONTROLLED NOTE TEXT")

    assert len(captures) == 2
    if os.name != "nt":
        assert all(item["mode"] == 0o600 for item in captures)
    assert all("MODEL-CONTROLLED NOTE TEXT" not in arg for item in captures for arg in item["argv"])
    assert captures[1]["payload"] == {
        "body": "MODEL-CONTROLLED NOTE TEXT",
        "in_reply_to": 123,
    }
    assert all(not os.path.exists(item["path"]) for item in captures)


def test_specialist_publish_cli_loads_only_typed_final_artifacts(tmp_path, monkeypatch):
    from scripts import publish_specialist_review as cli

    inputs = {
        "handoff.json": {"markdown": "Sparse handoff", "recommendation": "approve"},
        "notes.json": [{
            "kind": "finding",
            "fingerprint": "fp-cli",
            "markdown": "Claim",
            "file": "a.py",
            "line": 6,
            "severity": "major",
        }],
        "files.json": [{"filename": "a.py"}],
        "policy.json": {"verdict": "request_changes", "source": "supported-findings"},
        "artifacts.json": [{"label": "Artifact", "url": "https://example/artifact"}],
    }
    for name, value in inputs.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    (tmp_path / "pr.diff").write_text(DIFF, encoding="utf-8")

    captured = {}

    class FakePublisher:
        def __init__(self, _client, **kwargs):
            captured["init"] = kwargs

        def publish(self, **kwargs):
            captured["publish"] = kwargs
            return {"mode": kwargs["mode"]}

    monkeypatch.setattr(cli, "GhReviewClient", lambda **_kwargs: object())
    monkeypatch.setattr(cli, "GitHubReviewPublisher", FakePublisher)

    assert cli.main([
        "--mode", "review_comment",
        "--handoff", str(tmp_path / "handoff.json"),
        "--notes", str(tmp_path / "notes.json"),
        "--diff", str(tmp_path / "pr.diff"),
        "--files", str(tmp_path / "files.json"),
        "--policy-result", str(tmp_path / "policy.json"),
        "--artifacts", str(tmp_path / "artifacts.json"),
        "--repo", "owner/repo",
        "--pr-number", "17",
        "--head-sha", "abc123",
        "--state", str(tmp_path / "state.json"),
        "--action-root", str(tmp_path),
    ]) == 0
    assert isinstance(captured["publish"]["handoff"], ReviewHandoff)
    assert isinstance(captured["publish"]["notes"][0], ReviewNote)
    assert isinstance(captured["publish"]["policy_result"], RuntimeVerdictPolicyResult)
    assert "transcript" not in captured["publish"]
    assert "evidence_store" not in captured["publish"]


def test_legacy_publish_scripts_delegate_shared_diff_and_marker_primitives():
    from pr_reviewer.github_review_notes import (
        extract_managed_fingerprint,
        legacy_diff_positions,
    )
    from scripts import build_review_comments, resolve_finding_threads

    assert build_review_comments.diff_positions is legacy_diff_positions
    body = "x <!-- ai-pr-review-finding:legacy-fp -->"
    assert resolve_finding_threads.extract_marker_fingerprint(body) == "legacy-fp"
    assert extract_managed_fingerprint(
        body, build_review_comments.FINDING_MARKER_PREFIX
    ) == "legacy-fp"


def test_publish_helper_exposes_specialist_compatibility_wrapper():
    helper = (
        Path(__file__).resolve().parent.parent / "scripts" / "publish_helpers.sh"
    ).read_text(encoding="utf-8")
    assert "publish_specialist_review()" in helper
    assert "scripts/publish_specialist_review.py" in helper
