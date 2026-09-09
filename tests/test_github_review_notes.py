"""Specialist GitHub review-note anchoring and publication lifecycle."""

from __future__ import annotations

import json
import os
import stat
import types
from dataclasses import replace
from pathlib import Path

import pytest

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


def _issue_url(identifier: int = 100, *, host: str = "github.com") -> str:
    return f"https://{host}/owner/repo/pull/17#issuecomment-{identifier}"


def _discussion_url(identifier: int = 100, *, host: str = "github.com") -> str:
    return f"https://{host}/owner/repo/pull/17#discussion_r{identifier}"


def _review_url(identifier: int = 100, *, host: str = "github.com") -> str:
    return f"https://{host}/owner/repo/pull/17#pullrequestreview-{identifier}"


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


def test_diff_parser_does_not_treat_hunk_source_as_file_header():
    adversarial = """\
diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -4,2 +4,3 @@
 context
+++ b/attacker-controlled.py
+real change
"""
    anchor = choose_note_anchor(_note(file="a.py", line=6), adversarial, ("a.py",))
    assert anchor.subject_type == "LINE"
    assert anchor.path == "a.py"


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


def test_graphql_variables_include_validated_multiline_suggestion_range():
    note = normalize_note(
        replace(_note(line=6), start_line=5), DIFF, ("a.py",),
    )

    assert build_review_thread_variables("review-id", note) == {
        "pullRequestReviewId": "review-id",
        "body": note.managed_markdown,
        "subjectType": "LINE",
        "path": "a.py",
        "line": 6,
        "side": "RIGHT",
        "startLine": 5,
        "startSide": "RIGHT",
    }


class _FakeClient:
    def __init__(self, managed_state=None):
        self.calls: list[tuple] = []
        self.managed_state = {
            "pull_request_id": "PR-node",
            "head_ref_oid": "a" * 40,
            "repository_full_name": "owner/repo",
            "base_repository_full_name": "owner/repo",
            "head_repository_full_name": "owner/repo",
            "changed_files": ("a.py", "b.py"),
            "changed_files_complete": True,
            "threads": [],
            "general_comments": [],
            "reviews": [],
        }
        if managed_state is not None:
            self.managed_state.update(managed_state)

    def update_sticky(self, repo, pr_number, body, known_comment_id=None):
        self.calls.append(("sticky", repo, pr_number, body, known_comment_id))
        return {"id": 100, "url": _issue_url(100)}

    def query_managed_state(self, repo, pr_number):
        self.calls.append(("query", repo, pr_number))
        return self.managed_state

    def query_pr_identity(self, repo, pr_number):
        del repo, pr_number
        return {
            key: self.managed_state[key]
            for key in (
                "head_ref_oid", "repository_full_name",
                "base_repository_full_name", "head_repository_full_name",
            )
        }

    def reply_thread(self, comment_id, body):
        self.calls.append(("reply", comment_id, body))
        return {"id": 901, "url": _discussion_url(901)}

    def resolve_thread(self, thread_id):
        self.calls.append(("resolve", thread_id))
        return {"id": thread_id, "is_resolved": True}

    def create_pending_review(self, pull_request_id, head_sha, body):
        self.calls.append(("pending", pull_request_id, head_sha, body))
        return {
            "id": "review-id", "url": _review_url(150),
            "state": "PENDING", "body": body,
        }

    def add_review_thread(self, variables):
        self.calls.append(("add", variables))
        fp = variables["body"].split("ai-pr-review-note:", 1)[1].split()[0]
        return {
            "id": f"thread-{fp}",
            "url": _discussion_url(904),
            "comment_id": 904,
        }

    def submit_review(self, review_id, event, body):
        self.calls.append(("submit", review_id, event, body))
        return {
            "id": review_id,
            "url": _review_url(151),
            "state": {
                "COMMENT": "COMMENTED",
                "APPROVE": "APPROVED",
                "REQUEST_CHANGES": "CHANGES_REQUESTED",
            }[event],
            "body": body,
        }

    def upsert_general_comment(self, repo, pr_number, prior, body):
        self.calls.append(("general", repo, pr_number, prior, body))
        return {"id": 902, "url": _issue_url(902)}

    def reply_general_comment(self, repo, pr_number, prior, body):
        self.calls.append(("general_reply", repo, pr_number, prior, body))
        return {"id": 903, "url": _issue_url(903)}


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
        head_sha="a" * 40,
        artifact_links=(("Review artifact", "https://example/artifact"),),
        approval_policy=approval or PublisherApprovalPolicy(),
        changed_files_complete=True,
        diff_complete=False,
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
    assert result["review_completed"] is True


def test_comment_mode_live_head_mismatch_stops_before_sticky_mutation(tmp_path):
    class StaleIdentityClient(_FakeClient):
        def query_pr_identity(self, repo, pr_number):
            self.calls.append(("identity", repo, pr_number))
            return {
                **super().query_pr_identity(repo, pr_number),
                "head_ref_oid": "b" * 40,
            }

    client = StaleIdentityClient()
    result, _state = _publish(tmp_path, client, mode="comment")

    assert [call[0] for call in client.calls] == ["identity"]
    assert result["review_completed"] is False
    assert result["publication_errors"][0]["operation"] == "live_identity"


def test_comment_sticky_failure_never_reports_review_completed(tmp_path):
    class StickyFailureClient(_FakeClient):
        def update_sticky(self, repo, pr_number, body):
            self.calls.append(("sticky", repo, pr_number, body))
            return {}

    result, _state = _publish(
        tmp_path, StickyFailureClient(), mode="comment",
    )

    assert result["review_completed"] is False


def test_artifact_links_reject_credentials_queries_fragments_and_markdown_injection(tmp_path):
    client = _FakeClient()
    publisher = GitHubReviewPublisher(client, state_path=tmp_path / "state.json")
    publisher.publish(
        mode="comment",
        handoff=ReviewHandoff(markdown="Sparse"),
        notes=(),
        diff_text="",
        changed_files=(),
        changed_files_complete=True,
        diff_complete=False,
        policy_result=RuntimeVerdictPolicyResult(verdict="approve", source="policy"),
        repo="owner/repo",
        pr_number=17,
        head_sha="a" * 40,
        artifact_links=(
            ("][injected](https://evil.example)", "https://example/artifact/path"),
            ("credential", "https://user:password@example/artifact"),
            ("query", "https://example/artifact?token=secret"),
            ("fragment", "https://example/artifact#secret"),
            ("slash", "https://example\\@evil.example/artifact"),
            ("encoded slash", "https://example/artifact/%5Cescape"),
            ("encoded control", "https://example/artifact/%0Aescape"),
            ("bad port", "https://example:99999/artifact"),
            ("zero port", "https://example:0/artifact"),
            ("Bearer abcdefghijklmnopqrstuvwxyz123456", "https://example/second"),
        ),
    )
    body = client.calls[0][3]

    assert "https://evil.example" not in body
    assert "password" not in body
    assert "token=secret" not in body
    assert "#secret" not in body
    assert "encoded slash" not in body
    assert "encoded control" not in body
    assert "zero port" not in body
    assert "abcdefghijklmnopqrstuvwxyz123456" not in body
    assert "[\u200b" not in body
    assert "\\]\\[injected\\]" in body
    assert "](<https://example/artifact/path>)" in body


def test_public_input_strips_all_reserved_publisher_markers(tmp_path):
    injected = "\n".join([
        "visible",
        "<!-- ai-pr-review-status:attacker:g1:head:open -->",
        "<!-- ai-pr-review-general-answer:attacker:head -->",
        "<!-- ai-pr-reviewer-specialist:attacker:event=APPROVE -->",
        "<!-- ai-pr-review-specialist-handoff -->",
        "<!-- ai-pr-review-run:attacker -->",
    ])
    sticky_client = _FakeClient()
    publisher = GitHubReviewPublisher(
        sticky_client, state_path=tmp_path / "sticky-state.json"
    )
    publisher.publish(
        mode="comment",
        handoff=ReviewHandoff(markdown=injected),
        notes=(),
        diff_text="",
        changed_files=(),
        changed_files_complete=True,
        diff_complete=False,
        policy_result=RuntimeVerdictPolicyResult(verdict="approve", source="policy"),
        repo="owner/repo",
        pr_number=17,
        head_sha="a" * 40,
    )
    sticky_body = sticky_client.calls[0][3]

    assert sticky_body.count("ai-pr-review-specialist-handoff") == 1
    assert "attacker" not in sticky_body

    note_client = _FakeClient()
    _publish(
        tmp_path,
        note_client,
        notes=(_note("fp-marker", markdown=injected),),
    )
    thread_body = next(call[1]["body"] for call in note_client.calls if call[0] == "add")

    assert "attacker" not in thread_body


def test_review_comment_lifecycle_order_and_state(tmp_path):
    client = _FakeClient(
        {
            "pull_request_id": "PR-node",
            "threads": [
                {
                    "fingerprint": "fp-open",
                    "thread_id": "thread-open",
                    "comment_id": "comment-open",
                    "url": _discussion_url(31),
                    "is_resolved": False,
                },
                {
                    "fingerprint": "fp-fixed",
                    "thread_id": "thread-fixed",
                    "comment_id": "comment-fixed",
                    "url": _discussion_url(32),
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
        "query",
        "sticky",
        "reply",
        "reply",
        "resolve",
        "pending",
        "add",
        "add",
        "query",
        "submit",
        "sticky",
    ]
    submit = next(call for call in client.calls if call[0] == "submit")
    assert submit[2] == "COMMENT"
    assert "Automated specialist review notes. Detailed findings are in managed threads." in submit[3]
    by_fp = {entry["fingerprint"]: entry for entry in state["notes"]}
    assert by_fp["fp-fixed"]["resolution"] == "resolved"
    assert by_fp["fp-line"]["anchor_type"] == "LINE"
    assert by_fp["fp-file"]["anchor_type"] == "FILE"
    assert state["publication_errors"] == []


@pytest.mark.parametrize(
    "notes",
    [
        (),
        (_note("fp-general", file=None, line=None),),
    ],
)
def test_submitted_review_omits_thread_summary_without_review_threads(tmp_path, notes):
    client = _FakeClient()

    result, _state = _publish(tmp_path, client, notes=notes)

    submit = next(call for call in client.calls if call[0] == "submit")
    assert "Automated specialist review notes." not in submit[3]
    assert result["review_completed"] is True


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
        head_sha="a" * 40,
        changed_files_complete=True,
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
                "url": _discussion_url(33),
                "is_resolved": True,
                "resolved_by_publisher": False,
            }],
            "general_comments": [],
        }
    )
    _result, state = _publish(tmp_path, client, notes=(_note(),))

    assert not any(call[0] in {"resolve", "add"} for call in client.calls)
    assert [call[0] for call in client.calls].count("reply") == 1
    assert "human-resolved" in [call for call in client.calls if call[0] == "reply"][0][2]
    assert state["notes"][0]["human_resolved"] is True
    assert state["notes"][0]["resolution"] == "human_resolved_not_reopened"


def test_human_resolved_status_reply_is_deduplicated_for_head(tmp_path):
    thread = {
        "fingerprint": "fp-1",
        "generation": 1,
        "thread_id": "thread-human",
        "comment_id": 12,
        "url": _discussion_url(12),
        "is_resolved": True,
        "resolved_by_publisher": False,
        "owned_comment_bodies": (),
    }
    first = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [thread],
        "general_comments": [],
        "reviews": [],
    })
    _publish(tmp_path, first, notes=(_note(),))
    reply_body = next(call[2] for call in first.calls if call[0] == "reply")
    client = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [{**thread, "owned_comment_bodies": (reply_body,)}],
        "general_comments": [],
        "reviews": [],
    })
    _publish(tmp_path, client, notes=(_note(),))

    assert not any(call[0] in {"reply", "resolve", "add"} for call in client.calls)


def test_publisher_resolved_recurrence_creates_new_generation(tmp_path):
    client = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [{
            "fingerprint": "fp-1",
            "generation": 1,
            "thread_id": "thread-publisher",
            "comment_id": 12,
            "url": _discussion_url(34),
            "is_resolved": True,
            "resolved_by_publisher": True,
            "owned_comment_bodies": (),
        }],
        "general_comments": [],
        "reviews": [],
    })
    _result, state = _publish(tmp_path, client, notes=(_note(),))
    add = [call for call in client.calls if call[0] == "add"][0]

    assert "generation=2" in add[1]["body"]
    current = [item for item in state["notes"] if item["fingerprint"] == "fp-1"][-1]
    assert current["generation"] == 2
    assert current["resolution_source"] == "publisher_recurrence"


def test_publisher_resolved_recurrence_without_anchor_degrades_to_managed_general(
    tmp_path,
):
    client = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [{
            "fingerprint": "fp-recur-general",
            "generation": 1,
            "thread_id": "thread-old",
            "comment_id": 43,
            "url": _discussion_url(43),
            "is_resolved": True,
            "resolved_by_publisher": True,
            "owned_comment_bodies": (),
        }],
        "general_comments": [],
        "reviews": [],
    })

    result, state = _publish(
        tmp_path,
        client,
        notes=(_note("fp-recur-general", file=None, line=None),),
    )

    assert any(call[0] == "general" for call in client.calls)
    assert not any(call[0] == "add" for call in client.calls)
    note = next(
        item for item in state["notes"] if item["fingerprint"] == "fp-recur-general"
    )
    assert note["anchor_type"] == "GENERAL"
    assert note["generation"] == 2
    assert result["review"]["event"] == "COMMENT"


def test_same_open_status_reply_is_deduplicated_for_head(tmp_path):
    thread = {
        "fingerprint": "fp-1",
        "generation": 1,
        "thread_id": "thread-open",
        "comment_id": 12,
        "url": _discussion_url(12),
        "is_resolved": False,
        "resolved_by_publisher": False,
        "owned_comment_bodies": (),
    }
    first = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [thread],
        "general_comments": [],
        "reviews": [],
    })
    _publish(tmp_path, first, notes=(_note(),))
    reply_body = next(call[2] for call in first.calls if call[0] == "reply")
    client = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [{**thread, "owned_comment_bodies": (reply_body,)}],
        "general_comments": [],
        "reviews": [],
    })
    _publish(tmp_path, client, notes=(_note(),))

    assert not any(call[0] == "reply" for call in client.calls)


def test_same_head_changed_note_content_gets_one_new_status_reply(tmp_path):
    base_thread = {
        "fingerprint": "fp-status-content",
        "generation": 1,
        "thread_id": "thread-open",
        "comment_id": 45,
        "url": _discussion_url(45),
        "is_resolved": False,
        "owned_comment_bodies": (),
    }
    first = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [base_thread],
        "general_comments": [],
        "reviews": [],
    })
    _publish(
        tmp_path, first, notes=(_note("fp-status-content", markdown="old evidence"),)
    )
    old_reply = next(call[2] for call in first.calls if call[0] == "reply")
    second = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [{**base_thread, "owned_comment_bodies": (old_reply,)}],
        "general_comments": [],
        "reviews": [],
    })
    _publish(
        tmp_path,
        second,
        notes=(_note("fp-status-content", markdown="changed evidence"),),
    )
    changed_reply = next(call[2] for call in second.calls if call[0] == "reply")
    third = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [{
            **base_thread,
            "owned_comment_bodies": (old_reply, changed_reply),
        }],
        "general_comments": [],
        "reviews": [],
    })

    _publish(
        tmp_path,
        third,
        notes=(_note("fp-status-content", markdown="changed evidence"),),
    )

    assert old_reply != changed_reply
    assert not any(call[0] == "reply" for call in third.calls)


@pytest.mark.parametrize("human_resolved", [False, True])
def test_unconfirmed_existing_status_reply_keeps_review_pending(tmp_path, human_resolved):
    class ReplyFailureClient(_FakeClient):
        def reply_thread(self, comment_id, body):
            self.calls.append(("reply", comment_id, body))
            return {}

    client = ReplyFailureClient({
        "threads": [{
            "fingerprint": "fp-status-failed",
            "generation": 1,
            "thread_id": "thread-status-failed",
            "comment_id": 71,
            "url": _discussion_url(71),
            "is_resolved": human_resolved,
            "resolved_by_publisher": False,
            "owned_comment_bodies": (),
        }],
    })

    result, state = _publish(
        tmp_path, client, notes=(_note("fp-status-failed"),)
    )
    note = next(item for item in state["notes"] if item["fingerprint"] == "fp-status-failed")

    assert note["resolution"] == "publication_failed"
    assert note["confirmed"] is False
    assert note["publication_errors"]
    assert result["review"]["status"] == "pending_incomplete"
    assert not any(call[0] == "submit" for call in client.calls)


def test_general_answer_followup_is_deduplicated(tmp_path):
    client = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [],
        "general_comments": [{
            "fingerprint": "fp-general",
            "id": 15,
            "url": _issue_url(35),
        }],
        "general_answered_fingerprints": ("fp-general",),
        "reviews": [],
    })
    _publish(tmp_path, client, notes=())

    assert not any(call[0] == "general_reply" for call in client.calls)


def test_failed_general_update_never_reports_stale_comment_as_open(tmp_path):
    class GeneralFailureClient(_FakeClient):
        def upsert_general_comment(self, repo, pr_number, prior, body):
            self.calls.append(("general", repo, pr_number, prior, body))
            return {}

    client = GeneralFailureClient({
        "pull_request_id": "PR-node",
        "threads": [],
        "general_comments": [{
            "fingerprint": "fp-general-fail",
            "id": 50,
            "url": _issue_url(50),
            "body": "old",
        }],
        "reviews": [],
    })

    result, state = _publish(
        tmp_path,
        client,
        notes=(_note("fp-general-fail", file=None, line=None),),
    )
    note = next(item for item in state["notes"] if item["fingerprint"] == "fp-general-fail")

    assert note["resolution"] == "publication_failed"
    assert note["id"] is None
    assert result["review"]["status"] == "pending_incomplete"
    assert result["review_completed"] is False


def test_failed_general_answer_remains_unanswered_and_failed(tmp_path):
    class AnswerFailureClient(_FakeClient):
        def reply_general_comment(self, repo, pr_number, prior, body):
            self.calls.append(("general_reply", repo, pr_number, prior, body))
            return {}

    client = AnswerFailureClient({
        "pull_request_id": "PR-node",
        "threads": [],
        "general_comments": [{
            "fingerprint": "fp-answer-fail",
            "id": 51,
            "url": _issue_url(51),
            "body": "open",
        }],
        "reviews": [],
    })

    result, state = _publish(tmp_path, client, notes=())
    note = next(item for item in state["notes"] if item["fingerprint"] == "fp-answer-fail")

    assert note["resolution"] == "publication_failed"
    assert note["answered"] is False
    assert note["confirmed"] is False
    assert result["review"]["status"] == "pending_incomplete"
    assert not any(call[0] == "submit" for call in client.calls)


def test_failed_superseded_general_answer_keeps_anchored_note_pending(tmp_path):
    class AnswerFailureClient(_FakeClient):
        def reply_general_comment(self, repo, pr_number, prior, body):
            self.calls.append(("general_reply", repo, pr_number, prior, body))
            return {}

    client = AnswerFailureClient({
        "general_comments": [{
            "fingerprint": "fp-now-anchored-fail",
            "id": 53,
            "url": _issue_url(53),
            "body": "old fallback",
            "publication_id": "b" * 32,
            "generation": 1,
            "content_digest": "c" * 16,
        }],
    })

    result, state = _publish(
        tmp_path, client, notes=(_note("fp-now-anchored-fail"),)
    )
    note = next(
        item for item in state["notes"]
        if item["fingerprint"] == "fp-now-anchored-fail"
    )

    assert note["resolution"] == "publication_failed"
    assert note["confirmed"] is False
    assert "reply_general_comment" in note["publication_errors"]
    assert result["review"]["status"] == "pending_incomplete"
    assert not any(call[0] == "submit" for call in client.calls)


def test_anchored_note_answers_superseded_general_fallback_once(tmp_path):
    general = {
        "fingerprint": "fp-now-anchored",
        "id": 52,
        "url": _issue_url(52),
        "body": "old fallback",
    }
    first = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [],
        "general_comments": [general],
        "reviews": [],
    })
    _publish(tmp_path, first, notes=(_note("fp-now-anchored"),))
    answer = next(call[-1] for call in first.calls if call[0] == "general_reply")
    second = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [],
        "general_comments": [general],
        "general_answered_fingerprints": ("fp-now-anchored",),
        "reviews": [],
    })

    _publish(tmp_path, second, notes=(_note("fp-now-anchored"),))

    assert "fixed or answered" in answer
    assert not any(call[0] == "general_reply" for call in second.calls)


def test_general_request_and_answer_identity_include_publication_generation_and_content(
    tmp_path,
):
    first = _FakeClient()
    result, _state = _publish(
        tmp_path,
        first,
        notes=(_note("fp-general-identity", file=None, line=None),),
    )
    request_body = next(call[-1] for call in first.calls if call[0] == "general")
    marker = request_body.split("<!-- ai-pr-review-general:", 1)[1].split(" -->", 1)[0]
    assert f"publication={result['publication_id']}" in marker
    assert "generation=1" in marker
    content_digest = marker.split("content=", 1)[1]

    second = _FakeClient({
        "general_comments": [{
            "fingerprint": "fp-general-identity",
            "id": 54,
            "url": _issue_url(54),
            "body": request_body,
            "publication_id": result["publication_id"],
            "generation": 1,
            "content_digest": content_digest,
        }],
        # A prior same-head publication answered this fingerprint, but not this
        # publication/generation/content identity.
        "general_answered_fingerprints": ("fp-general-identity",),
        "general_answered_identities": (
            f"fp-general-identity:{'d' * 32}:1:{content_digest}",
        ),
    })

    _publish(tmp_path, second, notes=())
    answer_body = next(call[-1] for call in second.calls if call[0] == "general_reply")

    assert f"publication={result['publication_id']}" in answer_body
    assert "generation=1" in answer_body
    assert f"content={content_digest}" in answer_body


def test_unbound_general_answer_migrates_to_exact_identity_once(tmp_path):
    general = {
        "fingerprint": "fp-general-migration",
        "id": 55,
        "url": _issue_url(55),
        "body": "legacy unbound request",
    }
    first = _FakeClient({"general_comments": [general]})
    first_result, first_state = _publish(tmp_path, first, notes=())
    answer_body = next(
        call[-1] for call in first.calls if call[0] == "general_reply"
    )
    answer_marker = answer_body.split(
        "<!-- ai-pr-review-general-answer:", 1
    )[1].split(" -->", 1)[0]
    content_digest = answer_marker.split("content=", 1)[1]
    completed = _completed_review_from_submit(first, first_state)
    second = _FakeClient({
        "general_comments": [general],
        "general_answered_identities": (
            f"fp-general-migration:{first_result['publication_id']}:1:{content_digest}",
        ),
        "reviews": [completed],
    })

    _publish(tmp_path, second, notes=())

    assert not any(call[0] == "general_reply" for call in second.calls)


def test_unanchored_requests_use_explicitly_non_resolvable_general_comment(tmp_path):
    client = _FakeClient()
    request = _note(
        "fp-general",
        kind=ReviewNoteKind.SOURCE_ACCESS_REQUEST,
        file=None,
        line=None,
        markdown="Please grant access",
    )
    result, state = _publish(tmp_path, client, notes=(request,))

    assert [call[0] for call in client.calls] == [
        "query", "sticky", "general", "pending", "query", "submit", "sticky"
    ]
    assert any(item["operation"] == "upsert_general_comment" for item in result["journal"])
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
    assert state["review_completed"] is False
    assert state["publication_errors"][0]["operation"] == "add_review_thread"
    assert "publication unavailable" in state["publication_errors"][0]["error"]


def test_non_idempotent_thread_create_is_not_blindly_retried(tmp_path):
    class FailingCreateClient(_FakeClient):
        def add_review_thread(self, variables):
            self.calls.append(("add", variables))
            raise TimeoutError("response lost")

    client = FailingCreateClient({"changed_files": ("a.py",)})
    publisher = GitHubReviewPublisher(
        client, state_path=tmp_path / "state.json", max_attempts=3
    )
    publisher.publish(
        mode="review_comment",
        handoff=ReviewHandoff(markdown="Sparse"),
        notes=(_note("fp-timeout"),),
        diff_text=DIFF,
        changed_files=("a.py",),
        changed_files_complete=True,
        diff_complete=False,
        policy_result=RuntimeVerdictPolicyResult(verdict="approve", source="policy"),
        repo="owner/repo",
        pr_number=17,
        head_sha="a" * 40,
    )

    assert [call[0] for call in client.calls].count("add") == 1


def test_timeout_after_thread_create_reconciles_by_fingerprint(tmp_path):
    class AmbiguousCreateClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.query_count = 0
            self.pending_body = ""

        def create_pending_review(self, pull_request_id, head_sha, body):
            self.pending_body = body
            return super().create_pending_review(pull_request_id, head_sha, body)

        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            self.query_count += 1
            if self.query_count == 1:
                return self.managed_state
            publication_id = self.pending_body.split(":", 1)[1].split(":", 1)[0]
            return {
                **self.managed_state,
                "threads": [{
                    "fingerprint": "fp-timeout",
                    "generation": 1,
                    "publication_id": publication_id,
                    "review_id": "review-id",
                    "review_body": self.pending_body,
                    "review_state": "PENDING",
                    "head_sha": "a" * 40,
                    "thread_id": "thread-timeout",
                    "comment_id": 55,
                    "url": _discussion_url(55),
                    "is_resolved": False,
                    "owned_comment_bodies": (),
                }],
            }

        def add_review_thread(self, variables):
            self.calls.append(("add", variables))
            raise TimeoutError("server committed before timeout")

    client = AmbiguousCreateClient()
    result, state = _publish(tmp_path, client, notes=(_note("fp-timeout"),))

    assert [call[0] for call in client.calls].count("add") == 1
    assert [call[0] for call in client.calls].count("query") == 3
    note = [item for item in state["notes"] if item["fingerprint"] == "fp-timeout"][0]
    assert note["id"] == "thread-timeout"
    assert note["resolution"] == "open"
    assert any(item["operation"] == "reconcile_add_review_thread" for item in result["journal"])


def test_timeout_after_pending_review_create_reconciles_by_run_marker(tmp_path):
    head_sha = "a" * 40

    class AmbiguousPendingClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.query_count = 0
            self.pending_body = ""

        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            self.query_count += 1
            if self.query_count == 1:
                return self.managed_state
            return {
                **self.managed_state,
                "reviews": [{
                    "id": "review-timeout",
                    "url": _review_url(71),
                    "body": self.pending_body,
                    "state": "PENDING",
                }],
            }

        def create_pending_review(self, pull_request_id, received_sha, body):
            self.calls.append(("pending", pull_request_id, received_sha, body))
            self.pending_body = body
            raise TimeoutError("server committed before timeout")

    client = AmbiguousPendingClient()
    result, _state = _publish(tmp_path, client, notes=())

    assert [call[0] for call in client.calls].count("pending") == 1
    assert [call[0] for call in client.calls].count("query") == 3
    submit = [call for call in client.calls if call[0] == "submit"][0]
    assert submit[1] == "review-timeout"
    assert any(item["operation"] == "reconcile_create_pending_review" for item in result["journal"])


def test_timeout_after_submit_reconciles_by_owned_review_marker(tmp_path):
    class SubmitTimeoutClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.query_count = 0
            self.submitted_body = ""

        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            self.query_count += 1
            state = dict(self.managed_state)
            state["reviews"] = [] if self.query_count == 1 else [{
                "id": "review-id",
                "url": _review_url(72),
                "body": self.submitted_body,
                "state": "COMMENTED",
            }]
            return state

        def submit_review(self, review_id, event, body):
            self.calls.append(("submit", review_id, event, body))
            self.submitted_body = body
            raise TimeoutError("server committed before timeout")

    result, _state = _publish(tmp_path, SubmitTimeoutClient(), notes=(_note(),))

    assert result["review"]["url"] == _review_url(72)
    assert any(item["operation"] == "reconcile_submit_review" for item in result["journal"])


def test_completed_owned_review_marker_makes_rerun_idempotent(tmp_path):
    first = _FakeClient()
    _first_result, first_state = _publish(tmp_path, first, notes=())
    completed = _completed_review_from_submit(first, first_state)
    client = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [],
        "general_comments": [],
        "reviews": [completed],
    })

    result, _state = _publish(tmp_path, client, notes=())

    assert not any(call[0] in {"pending", "add", "submit"} for call in client.calls)
    assert result["review"]["id"] == completed["id"]
    assert result["sticky"]["url"] == _issue_url(100)


def test_completed_review_reuse_is_blocked_by_failed_existing_status_reply(tmp_path):
    note = _note("fp-completed-status")
    first = _FakeClient()
    _first_result, first_state = _publish(tmp_path, first, notes=(note,))
    completed = _completed_review_from_submit(first, first_state)

    class StatusFailureClient(_FakeClient):
        def reply_thread(self, comment_id, body):
            self.calls.append(("reply", comment_id, body))
            return {}

    second = StatusFailureClient({
        "threads": [{
            "fingerprint": note.fingerprint,
            "generation": 1,
            "thread_id": "thread-completed-status",
            "comment_id": 56,
            "url": _discussion_url(56),
            "is_resolved": False,
            "owned_comment_bodies": (),
        }],
        "reviews": [completed],
    })

    result, state = _publish(tmp_path, second, notes=(note,))

    assert result["review_completed"] is False
    assert state["review"]["status"] == "pending_incomplete"
    assert len([call for call in second.calls if call[0] == "sticky"]) == 1


def test_completed_review_reuse_is_blocked_by_failed_resolution(tmp_path):
    first = _FakeClient()
    _first_result, first_state = _publish(tmp_path, first, notes=())
    completed = _completed_review_from_submit(first, first_state)

    class ResolutionFailureClient(_FakeClient):
        def resolve_thread(self, thread_id):
            self.calls.append(("resolve", thread_id))
            return {}

    second = ResolutionFailureClient({
        "threads": [{
            "fingerprint": "fp-completed-fixed",
            "generation": 1,
            "thread_id": "thread-completed-fixed",
            "comment_id": 57,
            "url": _discussion_url(57),
            "is_resolved": False,
            "owned_comment_bodies": (),
        }],
        "reviews": [completed],
    })

    result, state = _publish(tmp_path, second, notes=())

    assert result["review_completed"] is False
    assert state["review"]["status"] == "pending_incomplete"
    assert len([call for call in second.calls if call[0] == "sticky"]) == 1


def test_completed_review_reuse_is_blocked_by_failed_general_answer(tmp_path):
    first = _FakeClient()
    _first_result, first_state = _publish(tmp_path, first, notes=())
    completed = _completed_review_from_submit(first, first_state)

    class GeneralAnswerFailureClient(_FakeClient):
        def reply_general_comment(self, repo, pr_number, prior, body):
            self.calls.append(("general_reply", repo, pr_number, prior, body))
            return {}

    second = GeneralAnswerFailureClient({
        "general_comments": [{
            "fingerprint": "fp-completed-general",
            "id": 58,
            "url": _issue_url(58),
            "body": "unanswered general request",
        }],
        "reviews": [completed],
    })

    result, state = _publish(tmp_path, second, notes=())

    assert result["review_completed"] is False
    assert state["review"]["status"] == "pending_incomplete"
    assert len([call for call in second.calls if call[0] == "sticky"]) == 1


def test_completed_review_reuse_revalidates_head_after_reconciliation(tmp_path):
    first = _FakeClient()
    _first_result, first_state = _publish(tmp_path, first, notes=())
    completed = _completed_review_from_submit(first, first_state)

    class PushDuringReconciliationClient(_FakeClient):
        def __init__(self):
            super().__init__({
                "general_comments": [{
                    "fingerprint": "fp-completed-push",
                    "id": 59,
                    "url": _issue_url(59),
                    "body": "unanswered general request",
                }],
                "reviews": [completed],
            })
            self.query_count = 0

        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            self.query_count += 1
            return {
                **self.managed_state,
                "head_ref_oid": ("a" if self.query_count == 1 else "b") * 40,
            }

    second = PushDuringReconciliationClient()

    result, state = _publish(tmp_path, second, notes=())

    assert second.query_count == 2
    assert result["review_completed"] is False
    assert state["review"]["status"] == "pending_incomplete"
    assert any(
        error["operation"] == "pre_submit_head_ref_oid"
        for error in state["publication_errors"]
    )
    assert len([call for call in second.calls if call[0] == "sticky"]) == 1


def _completed_review_from_submit(client, state):
    submit = next(call for call in client.calls if call[0] == "submit")
    expected_state = {
        "COMMENT": "COMMENTED",
        "APPROVE": "APPROVED",
        "REQUEST_CHANGES": "CHANGES_REQUESTED",
    }[submit[2]]
    return {
        "id": state["review"]["id"],
        "url": _review_url(500),
        "body": submit[3],
        "state": expected_state,
    }


def test_same_head_changed_policy_creates_new_publication_not_stale_approval(tmp_path):
    approval = PublisherApprovalPolicy(allow_approve=True, baseline_clean=True)
    first = _FakeClient()
    _first_result, first_state = _publish(
        tmp_path,
        first,
        mode="review_verdict",
        notes=(_note("fp-policy"),),
        verdict="approve",
        approval=approval,
    )
    completed = _completed_review_from_submit(first, first_state)
    second = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [],
        "general_comments": [],
        "reviews": [completed],
    })

    _publish(
        tmp_path,
        second,
        mode="review_verdict",
        notes=(_note("fp-policy"),),
        verdict="request_changes",
        approval=approval,
    )

    submit = next(call for call in second.calls if call[0] == "submit")
    assert submit[2] == "REQUEST_CHANGES"
    assert submit[3] != completed["body"]


def test_same_head_changed_note_content_creates_new_publication(tmp_path):
    first = _FakeClient()
    _first_result, first_state = _publish(
        tmp_path, first, notes=(_note("fp-content", markdown="old evidence"),)
    )
    completed = _completed_review_from_submit(first, first_state)
    second = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [],
        "general_comments": [],
        "reviews": [completed],
    })

    _publish(
        tmp_path, second, notes=(_note("fp-content", markdown="changed evidence"),)
    )

    assert any(call[0] == "submit" for call in second.calls)
    assert next(call for call in second.calls if call[0] == "submit")[3] != completed["body"]


def test_dismissed_owned_review_never_satisfies_current_publication(tmp_path):
    first = _FakeClient()
    _first_result, first_state = _publish(tmp_path, first, notes=())
    dismissed = {**_completed_review_from_submit(first, first_state), "state": "DISMISSED"}
    second = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [],
        "general_comments": [],
        "reviews": [dismissed],
    })

    _publish(tmp_path, second, notes=())

    assert any(call[0] == "submit" for call in second.calls)


def test_timeout_after_resolve_reconciles_confirmed_owned_resolution_marker(tmp_path):
    class ResolveTimeoutClient(_FakeClient):
        def __init__(self):
            super().__init__({
                "pull_request_id": "PR-node",
                "threads": [{
                    "fingerprint": "fp-fixed",
                    "generation": 1,
                    "thread_id": "thread-fixed",
                    "comment_id": 44,
                    "url": _discussion_url(44),
                    "is_resolved": False,
                    "owned_comment_bodies": (),
                }],
                "general_comments": [],
                "reviews": [],
            })
            self.query_count = 0
            self.attempted_body = ""

        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            self.query_count += 1
            if self.query_count == 1:
                return self.managed_state
            marker = (
                f"<!-- ai-pr-review-resolution:fp-fixed:g1:{'a' * 40}:publisher -->"
            )
            state = dict(self.managed_state)
            state["threads"] = [{
                **self.managed_state["threads"][0],
                "is_resolved": True,
                "resolved_by_publisher": True,
                "owned_comment_bodies": (marker,),
            }]
            return state

        def resolve_thread(self, thread_id):
            self.calls.append(("resolve", thread_id))
            raise TimeoutError("server committed before timeout")

    result, state = _publish(tmp_path, ResolveTimeoutClient(), notes=())
    fixed = next(item for item in state["notes"] if item["fingerprint"] == "fp-fixed")

    assert fixed["resolution"] == "resolved"
    assert any(item["operation"] == "reconcile_resolve_thread" for item in result["journal"])


def test_timeout_after_status_reply_reconciles_by_exact_owned_marker(tmp_path):
    class ReplyTimeoutClient(_FakeClient):
        def __init__(self):
            super().__init__({
                "pull_request_id": "PR-node",
                "threads": [{
                    "fingerprint": "fp-open",
                    "generation": 1,
                    "thread_id": "thread-open",
                    "comment_id": 45,
                    "url": _discussion_url(45),
                    "is_resolved": False,
                    "owned_comment_bodies": (),
                }],
                "general_comments": [],
                "reviews": [],
            })
            self.query_count = 0

        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            self.query_count += 1
            if self.query_count == 1:
                return self.managed_state
            state = dict(self.managed_state)
            state["threads"] = [{
                **self.managed_state["threads"][0],
                "owned_comment_bodies": (self.attempted_body,),
            }]
            return state

        def reply_thread(self, comment_id, body):
            self.calls.append(("reply", comment_id, body))
            self.attempted_body = body
            raise TimeoutError("server committed before timeout")

    result, state = _publish(
        tmp_path, ReplyTimeoutClient(), notes=(_note("fp-open"),)
    )

    assert next(item for item in state["notes"] if item["fingerprint"] == "fp-open")[
        "resolution"
    ] == "open"
    assert any(item["operation"] == "reconcile_reply_open_thread" for item in result["journal"])


def test_publication_checkpoints_survive_interruption_after_thread_create(tmp_path):
    class InterruptClient(_FakeClient):
        def submit_review(self, review_id, event, body):
            self.calls.append(("submit", review_id, event, body))
            raise KeyboardInterrupt("simulated runner termination")

    state_path = tmp_path / "state.json"
    client = InterruptClient({"changed_files": ("a.py",)})
    publisher = GitHubReviewPublisher(client, state_path=state_path, max_attempts=1)
    with pytest.raises(KeyboardInterrupt):
        publisher.publish(
            mode="review_comment",
            handoff=ReviewHandoff(markdown="Sparse"),
            notes=(_note("fp-checkpoint"),),
            diff_text=DIFF,
            changed_files=("a.py",),
            changed_files_complete=True,
            diff_complete=False,
            policy_result=RuntimeVerdictPolicyResult(verdict="approve", source="policy"),
            repo="owner/repo",
            pr_number=17,
            head_sha="a" * 40,
        )

    checkpoint = json.loads(state_path.read_text(encoding="utf-8"))
    add_entry = next(
        item for item in checkpoint["journal"] if item["operation"] == "add_review_thread"
    )
    assert add_entry["generation"] == 1
    assert add_entry["publication_id"] == checkpoint["publication_id"]
    assert add_entry["review_id"] == "review-id"
    assert checkpoint["notes"][0]["fingerprint"] == "fp-checkpoint"


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
            response = {"data": {"viewer": {"login": "bot"}}}
        else:
            response = {"id": 12, "html_url": _discussion_url(12)}
        return types.SimpleNamespace(
            returncode=0, stdout=json.dumps(response).encode(), stderr=b""
        )

    monkeypatch.setattr("pr_reviewer.github_review_notes.subprocess.run", fake_run)
    client = GhReviewClient(action_root=tmp_path)
    client._graphql("query X { viewer { login } }", {"bounded": "value"})
    client._repo_context = ("owner/repo", 17)
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


def test_sticky_updates_only_owned_specialist_marker_comment(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)
    writes = []
    monkeypatch.setattr(
        client,
        "_trusted_specialist_handoffs",
        lambda *_args, **_kwargs: ({"id": 88, "url": _issue_url(88)},),
    )
    monkeypatch.setattr(
        client,
        "_api_write",
        lambda endpoint, method, payload: writes.append((endpoint, method, payload)) or {
            "id": 88, "url": _issue_url(88)
        },
        raising=False,
    )

    result = client.update_sticky(
        "owner/repo", 17,
        "<!-- ai-pr-review-specialist-handoff -->\nSparse handoff",
    )

    assert writes == [(
        "repos/owner/repo/issues/comments/88",
        "PATCH",
        {"body": "<!-- ai-pr-review-specialist-handoff -->\nSparse handoff"},
    )]
    assert result == {"id": 88, "url": _issue_url(88)}


def test_sticky_updates_newest_exact_managed_handoff_not_newest_issue_comment(
    monkeypatch, tmp_path
):
    client = GhReviewClient(action_root=tmp_path)
    comments = [
        {
            "databaseId": 41,
            "url": _issue_url(41),
            "body": "<!-- ai-pr-review-specialist-handoff -->\nOlder handoff",
            "viewerDidAuthor": True,
            "author": {"login": "bot"},
        },
        {
            "databaseId": 73,
            "url": _issue_url(73),
            "body": "<!-- ai-pr-review-specialist-handoff -->\nNewer handoff",
            "viewerDidAuthor": True,
            "author": {"login": "bot"},
        },
        {
            "databaseId": 99,
            "url": _issue_url(99),
            "body": "Unrelated newer comment",
            "viewerDidAuthor": True,
            "author": {"login": "bot"},
        },
    ]
    writes = []

    def fake_graphql(query, _variables):
        if "ManagedReviewIdentity" in query:
            return {"data": {
                "viewer": {"login": "bot"},
                "repository": {
                    "nameWithOwner": "owner/repo",
                    "pullRequest": {"id": "PR-node"},
                },
            }}
        if "ManagedIssueComments" in query:
            return {"data": {"node": {"comments": _connection(comments)}}}
        raise AssertionError(query)

    monkeypatch.setattr(client, "_graphql", fake_graphql)
    monkeypatch.setattr(
        client,
        "_api_write",
        lambda endpoint, method, payload: writes.append((endpoint, method, payload))
        or {"id": 73, "url": _issue_url(73)},
    )

    client.update_sticky(
        "owner/repo",
        17,
        "<!-- ai-pr-review-specialist-handoff -->\nCurrent handoff",
    )

    assert writes == [(
        "repos/owner/repo/issues/comments/73",
        "PATCH",
        {"body": "<!-- ai-pr-review-specialist-handoff -->\nCurrent handoff"},
    ), (
        "repos/owner/repo/issues/comments/41",
        "DELETE",
        {},
    )]


def test_sticky_duplicate_cleanup_keeps_newest_and_ignores_untrusted_or_spoofed(
    monkeypatch, tmp_path
):
    client = GhReviewClient(action_root=tmp_path)
    comments = [
        {
            "databaseId": 41,
            "url": _issue_url(41),
            "body": "<!-- ai-pr-review-specialist-handoff -->\nOlder trusted",
            "viewerDidAuthor": True,
            "author": {"login": "bot"},
        },
        {
            "databaseId": 73,
            "url": _issue_url(73),
            "body": "<!-- ai-pr-review-specialist-handoff -->\nNewest trusted",
            "viewerDidAuthor": True,
            "author": {"login": "bot"},
        },
        {
            "databaseId": 88,
            "url": _issue_url(88),
            "body": "<!-- ai-pr-review-specialist-handoff -->\nUntrusted copy",
            "viewerDidAuthor": False,
            "author": {"login": "unknown"},
        },
        {
            "databaseId": 89,
            "url": _issue_url(89),
            "body": "<!-- ai-pr-review-specialist-handoff -->spoof",
            "viewerDidAuthor": True,
            "author": {"login": "bot"},
        },
    ]
    writes = []

    def fake_graphql(query, _variables):
        if "ManagedReviewIdentity" in query:
            return {"data": {
                "viewer": {"login": "bot"},
                "repository": {
                    "nameWithOwner": "owner/repo",
                    "pullRequest": {"id": "PR-node"},
                },
            }}
        if "ManagedIssueComments" in query:
            return {"data": {"node": {"comments": _connection(comments)}}}
        raise AssertionError(query)

    monkeypatch.setattr(client, "_graphql", fake_graphql)
    monkeypatch.setattr(
        client,
        "_api_write",
        lambda endpoint, method, payload: writes.append((endpoint, method, payload))
        or ({"deleted": True} if method == "DELETE" else {
            "id": 73, "url": _issue_url(73),
        }),
    )

    client.update_sticky(
        "owner/repo",
        17,
        "<!-- ai-pr-review-specialist-handoff -->\nCurrent handoff",
    )

    assert writes == [
        (
            "repos/owner/repo/issues/comments/73",
            "PATCH",
            {"body": "<!-- ai-pr-review-specialist-handoff -->\nCurrent handoff"},
        ),
        ("repos/owner/repo/issues/comments/41", "DELETE", {}),
    ]


def test_sticky_duplicate_cleanup_failure_surfaces_without_patch_or_create(
    monkeypatch, tmp_path
):
    client = GhReviewClient(action_root=tmp_path)
    comments = [
        {
            "databaseId": comment_id,
            "url": _issue_url(comment_id),
            "body": "<!-- ai-pr-review-specialist-handoff -->\nManaged",
            "viewerDidAuthor": True,
            "author": {"login": "bot"},
        }
        for comment_id in (41, 73)
    ]
    writes = []

    def fake_graphql(query, _variables):
        if "ManagedReviewIdentity" in query:
            return {"data": {
                "viewer": {"login": "bot"},
                "repository": {
                    "nameWithOwner": "owner/repo",
                    "pullRequest": {"id": "PR-node"},
                },
            }}
        if "ManagedIssueComments" in query:
            return {"data": {"node": {"comments": _connection(comments)}}}
        raise AssertionError(query)

    def fail_delete(endpoint, method, payload):
        writes.append((endpoint, method, payload))
        if method == "DELETE":
            raise RuntimeError("delete failed")
        return {"id": 73, "url": _issue_url(73)}

    monkeypatch.setattr(client, "_graphql", fake_graphql)
    monkeypatch.setattr(client, "_api_write", fail_delete)

    result = client.update_sticky(
        "owner/repo",
        17,
        "<!-- ai-pr-review-specialist-handoff -->\nCurrent handoff",
    )

    assert result["id"] == 73
    assert result["url"] == _issue_url(73)
    assert result["cleanup_errors"] == (
        "duplicate sticky cleanup failed for comment 41",
    )
    assert writes == [
        (
            "repos/owner/repo/issues/comments/73",
            "PATCH",
            {"body": "<!-- ai-pr-review-specialist-handoff -->\nCurrent handoff"},
        ),
        ("repos/owner/repo/issues/comments/41", "DELETE", {}),
    ]


def test_sticky_ignores_marker_prefix_spoof_and_creates_when_no_exact_marker(
    monkeypatch, tmp_path
):
    client = GhReviewClient(action_root=tmp_path)
    writes = []

    def fake_graphql(query, _variables):
        if "ManagedReviewIdentity" in query:
            return {"data": {
                "viewer": {"login": "bot"},
                "repository": {
                    "nameWithOwner": "owner/repo",
                    "pullRequest": {"id": "PR-node"},
                },
            }}
        if "ManagedIssueComments" in query:
            return {"data": {"node": {"comments": _connection([{
                "databaseId": 88,
                "url": _issue_url(88),
                "body": (
                    "<!-- ai-pr-review-specialist-handoff -->copied\n"
                    "Not a managed handoff"
                ),
                "viewerDidAuthor": True,
                "author": {"login": "bot"},
            }])}}}
        raise AssertionError(query)

    monkeypatch.setattr(client, "_graphql", fake_graphql)
    monkeypatch.setattr(
        client,
        "_api_write",
        lambda endpoint, method, payload: writes.append((endpoint, method, payload))
        or {"id": 101, "url": _issue_url(101)},
    )

    client.update_sticky(
        "owner/repo",
        17,
        "<!-- ai-pr-review-specialist-handoff -->\nCurrent handoff",
    )

    assert writes == [(
        "repos/owner/repo/issues/17/comments",
        "POST",
        {"body": "<!-- ai-pr-review-specialist-handoff -->\nCurrent handoff"},
    )]


def test_sticky_rejects_repository_identity_mismatch_before_write(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)
    writes = []

    def fake_graphql(query, _variables):
        if "ManagedReviewIdentity" in query:
            return {"data": {
                "viewer": {"login": "bot"},
                "repository": {
                    "nameWithOwner": "attacker/repo",
                    "pullRequest": {"id": "PR-node"},
                },
            }}
        raise AssertionError(query)

    monkeypatch.setattr(client, "_graphql", fake_graphql)
    monkeypatch.setattr(
        client,
        "_api_write",
        lambda endpoint, method, payload: writes.append((endpoint, method, payload)),
    )

    with pytest.raises(RuntimeError, match="repository identity"):
        client.update_sticky(
            "owner/repo",
            17,
            "<!-- ai-pr-review-specialist-handoff -->\nCurrent handoff",
        )

    assert writes == []


def test_sticky_updates_exact_github_actions_bot_handoff_after_viewer_identity_changes(
    monkeypatch, tmp_path
):
    client = GhReviewClient(action_root=tmp_path)
    writes = []

    def fake_graphql(query, _variables):
        if "ManagedReviewIdentity" in query:
            return {"data": {
                "viewer": {"login": "current-review-app[bot]"},
                "repository": {
                    "nameWithOwner": "owner/repo",
                    "pullRequest": {"id": "PR-node"},
                },
            }}
        if "ManagedIssueComments" in query:
            return {"data": {"node": {"comments": _connection([
                {
                    "databaseId": 71,
                    "url": _issue_url(71),
                    "body": "<!-- ai-pr-review-specialist-handoff -->\nPrevious run",
                    "viewerDidAuthor": False,
                    "author": {"login": "github-actions[bot]"},
                },
                {
                    "databaseId": 89,
                    "url": _issue_url(89),
                    "body": "<!-- ai-pr-review-specialist-handoff -->\nCopied marker",
                    "viewerDidAuthor": False,
                    "author": {"login": "untrusted-user"},
                },
            ])}}}
        raise AssertionError(query)

    monkeypatch.setattr(client, "_graphql", fake_graphql)
    monkeypatch.setattr(
        client,
        "_api_write",
        lambda endpoint, method, payload: writes.append((endpoint, method, payload))
        or {"id": 71, "url": _issue_url(71)},
    )

    client.update_sticky(
        "owner/repo",
        17,
        "<!-- ai-pr-review-specialist-handoff -->\nCurrent run",
    )

    assert writes == [(
        "repos/owner/repo/issues/comments/71",
        "PATCH",
        {"body": "<!-- ai-pr-review-specialist-handoff -->\nCurrent run"},
    )]


def test_sticky_recognizes_graphql_github_actions_actor_name(
    monkeypatch, tmp_path
):
    client = GhReviewClient(action_root=tmp_path)
    writes = []

    def fake_graphql(query, _variables):
        if "ManagedReviewIdentity" in query:
            return {"data": {
                "viewer": {"login": "maintainer"},
                "repository": {
                    "nameWithOwner": "owner/repo",
                    "pullRequest": {"id": "PR-node"},
                },
            }}
        if "ManagedIssueComments" in query:
            return {"data": {"node": {"comments": _connection([{
                "databaseId": 41,
                "url": _issue_url(41),
                "body": "<!-- ai-pr-review-specialist-handoff -->\nPrevious run",
                "viewerDidAuthor": False,
                "author": {"login": "github-actions"},
            }])}}}
        raise AssertionError(query)

    monkeypatch.setattr(client, "_graphql", fake_graphql)
    monkeypatch.setattr(
        client,
        "_api_write",
        lambda endpoint, method, payload: writes.append((endpoint, method, payload))
        or {"id": 41, "url": _issue_url(41)},
    )

    client.update_sticky(
        "owner/repo", 17,
        "<!-- ai-pr-review-specialist-handoff -->\nCurrent run",
    )

    assert writes == [(
        "repos/owner/repo/issues/comments/41",
        "PATCH",
        {"body": "<!-- ai-pr-review-specialist-handoff -->\nCurrent run"},
    )]


def test_sticky_post_timeout_reconciles_exact_owned_body_without_retry(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)
    body = "<!-- ai-pr-review-specialist-handoff -->\nSparse handoff"
    finds = []

    def find(_repo, _pr_number, expected_body=None):
        finds.append(expected_body)
        if expected_body == body:
            return {"id": 74, "url": _issue_url(74)}
        return None

    writes = []

    def ambiguous_write(endpoint, method, payload):
        writes.append((endpoint, method, payload))
        raise TimeoutError("server committed before timeout")

    monkeypatch.setattr(client, "find_specialist_handoff", find)
    monkeypatch.setattr(client, "_trusted_specialist_handoffs", lambda *_args: ())
    monkeypatch.setattr(client, "_api_write", ambiguous_write)

    assert client.update_sticky("owner/repo", 17, body) == {
        "id": 74,
        "url": _issue_url(74),
    }
    assert len(writes) == 1
    assert finds == [body]


def test_sticky_refresh_rejects_untrusted_comment_id_without_a_write(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)
    writes = []
    monkeypatch.setattr(
        client,
        "_api_write",
        lambda endpoint, method, payload: writes.append((endpoint, method, payload)),
    )

    with pytest.raises(ValueError):
        client.update_sticky(
            "owner/repo",
            17,
            "<!-- ai-pr-review-specialist-handoff -->\nReview link",
            known_comment_id=74,
        )

    assert writes == []


def test_sticky_refresh_rejects_a_trusted_id_for_a_different_pull_request(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)
    initial_body = "<!-- ai-pr-review-specialist-handoff -->\nInitial handoff"
    writes = []

    def find(_repo, _pr_number, expected_body=None):
        del expected_body
        return None

    def write(endpoint, method, payload):
        writes.append((endpoint, method, payload))
        return {"id": 74, "url": _issue_url(74)}

    monkeypatch.setattr(client, "find_specialist_handoff", find)
    monkeypatch.setattr(client, "_trusted_specialist_handoffs", lambda *_args: ())
    monkeypatch.setattr(client, "_api_write", write)
    created = client.update_sticky("owner/repo", 17, initial_body)

    with pytest.raises(ValueError):
        client.update_sticky(
            "owner/repo",
            18,
            "<!-- ai-pr-review-specialist-handoff -->\nReview link",
            known_comment_id=created["id"],
        )

    assert writes == [
        ("repos/owner/repo/issues/17/comments", "POST", {"body": initial_body}),
    ]


def test_sticky_refresh_patches_known_comment_when_comment_list_is_stale(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)
    initial_body = "<!-- ai-pr-review-specialist-handoff -->\nInitial handoff"
    refreshed_body = "<!-- ai-pr-review-specialist-handoff -->\nReview link"
    finds = []
    writes = []

    def find(_repo, _pr_number, expected_body=None):
        finds.append(expected_body)
        return None

    def write(endpoint, method, payload):
        writes.append((endpoint, method, payload))
        return {"id": 74, "url": _issue_url(74)}

    monkeypatch.setattr(client, "find_specialist_handoff", find)
    monkeypatch.setattr(client, "_trusted_specialist_handoffs", lambda *_args: ())
    monkeypatch.setattr(client, "_api_write", write)

    created = client.update_sticky("owner/repo", 17, initial_body)
    refreshed = client.update_sticky(
        "owner/repo", 17, refreshed_body, known_comment_id=created["id"]
    )

    assert refreshed == {"id": 74, "url": _issue_url(74)}
    assert finds == []
    assert writes == [
        ("repos/owner/repo/issues/17/comments", "POST", {"body": initial_body}),
        ("repos/owner/repo/issues/comments/74", "PATCH", {"body": refreshed_body}),
    ]


def test_general_comment_post_timeout_reconciles_exact_owned_body_without_retry(
    monkeypatch, tmp_path
):
    client = GhReviewClient(action_root=tmp_path)
    body = "General detail\n<!-- ai-pr-review-general:fp-general -->"
    writes = []

    def ambiguous_write(endpoint, method, payload):
        writes.append((endpoint, method, payload))
        raise TimeoutError("server committed before timeout")

    monkeypatch.setattr(client, "_api_write", ambiguous_write)
    monkeypatch.setattr(
        client,
        "_find_owned_issue_comment_exact",
        lambda _repo, _pr_number, expected: (
            {"id": 75, "url": _issue_url(75)}
            if expected == body
            else None
        ),
        raising=False,
    )

    assert client.upsert_general_comment("owner/repo", 17, None, body) == {
        "id": 75,
        "url": _issue_url(75),
    }
    assert len(writes) == 1


def test_production_client_rejects_malformed_mutation_success_objects(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)
    client._repo_context = ("owner/repo", 17)
    monkeypatch.setattr(client, "_input_call", lambda *_args, **_kwargs: {"id": "12"})
    with pytest.raises(RuntimeError, match="valid id"):
        client.reply_thread(10, "reply")

    def malformed_graphql(query, _variables):
        if "ResolveManagedThread" in query:
            return {"data": {"resolveReviewThread": {"thread": {"id": "T", "isResolved": False}}}}
        if "CreatePendingReview" in query:
            return {"data": {"addPullRequestReview": {"pullRequestReview": {"id": "R"}}}}
        if "AddManagedReviewThread" in query:
            return {"data": {"addPullRequestReviewThread": {"thread": {"id": "T"}}}}
        if "SubmitManagedReview" in query:
            return {"data": {"submitPullRequestReview": {"pullRequestReview": {"url": _review_url(77)}}}}
        raise AssertionError(query)

    monkeypatch.setattr(client, "_graphql", malformed_graphql)
    with pytest.raises(RuntimeError, match="resolve mutation"):
        client.resolve_thread("T")
    with pytest.raises(RuntimeError, match="pending review"):
        client.create_pending_review("PR", "a" * 40, "marker")
    with pytest.raises(RuntimeError, match="review thread"):
        client.add_review_thread({"pullRequestReviewId": "R"})
    with pytest.raises(RuntimeError, match="submitted review"):
        client.submit_review("R", "COMMENT", "body")


def test_production_comment_delete_accepts_github_empty_success_response(
    monkeypatch, tmp_path
):
    client = GhReviewClient(action_root=tmp_path)
    monkeypatch.setattr(client, "_input_call", lambda *_args, **_kwargs: {})

    assert client._api_write(
        "repos/owner/repo/issues/comments/41", "DELETE", {}
    ) == {"deleted": True}


@pytest.mark.parametrize(
    "url",
    [
        _issue_url(12),
        _discussion_url(13),
        _review_url(14),
        _issue_url(15, host="github.enterprise.example"),
    ],
)
def test_production_result_urls_accept_realistic_github_fragments(
    monkeypatch, tmp_path, url
):
    client = GhReviewClient(action_root=tmp_path)
    monkeypatch.setattr(
        client,
        "_input_call",
        lambda *_args, **_kwargs: {"id": 12, "html_url": url},
    )

    assert client._api_write("endpoint", "POST", {"body": "x"}) == {
        "id": 12,
        "url": url,
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@github.com/owner/repo/pull/17#issuecomment-1",
        "https://github.com/owner/repo/pull/17?x=1#issuecomment-1",
        "https://github.com/owner/repo/pull/17#arbitrary",
        "https://github.com/owner/repo/pull/17",
        "https://example/review",
        "https://github.com/owner/repo/pull/17%0A#issuecomment-1",
        "https://github.com/owner/repo/pull/17%5Cevil#issuecomment-1",
        "https://github.com:0/owner/repo/pull/17#issuecomment-1",
        "https://github.com:99999/owner/repo/pull/17#issuecomment-1",
    ],
)
def test_production_result_urls_reject_dangerous_shapes(monkeypatch, tmp_path, url):
    client = GhReviewClient(action_root=tmp_path)
    monkeypatch.setattr(
        client,
        "_input_call",
        lambda *_args, **_kwargs: {"id": 12, "html_url": url},
    )

    with pytest.raises(RuntimeError, match="valid URL"):
        client._api_write("endpoint", "POST", {"body": "x"})


def test_production_review_mutations_require_expected_confirmed_state(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)

    def wrong_state(query, _variables):
        if "CreatePendingReview" in query:
            return {"data": {"addPullRequestReview": {"pullRequestReview": {
                "id": "R", "url": _review_url(21), "state": "DISMISSED", "body": "marker"
            }}}}
        if "SubmitManagedReview" in query:
            return {"data": {"submitPullRequestReview": {"pullRequestReview": {
                "id": "R", "url": _review_url(22), "state": "APPROVED", "body": "marker"
            }}}}
        raise AssertionError(query)

    monkeypatch.setattr(client, "_graphql", wrong_state)
    with pytest.raises(RuntimeError, match="pending review"):
        client.create_pending_review("PR", "a" * 40, "marker")
    with pytest.raises(RuntimeError, match="expected state"):
        client.submit_review("R", "REQUEST_CHANGES", "marker")


def test_final_sticky_refresh_has_one_aggregate_review_link_and_no_note_detail(tmp_path):
    client = _FakeClient()
    _publish(
        tmp_path,
        client,
        notes=(_note("fp-secret", markdown="PRIVATE NOTE DETAIL"),),
    )
    sticky_calls = [call for call in client.calls if call[0] == "sticky"]

    assert len(sticky_calls) == 2
    assert "PRIVATE NOTE DETAIL" not in sticky_calls[-1][3]
    assert sticky_calls[-1][3].count(_review_url(151)) == 1
    assert "Detailed managed review" in sticky_calls[-1][3]


def test_final_sticky_refresh_uses_initial_comment_id_when_list_is_stale(tmp_path):
    class StaleListClient(_FakeClient):
        def update_sticky(self, repo, pr_number, body, known_comment_id=None):
            self.calls.append(("sticky", repo, pr_number, body, known_comment_id))
            return {"id": 100, "url": _issue_url(100)}

    client = StaleListClient()
    _publish(tmp_path, client, notes=())
    sticky_calls = [call for call in client.calls if call[0] == "sticky"]

    assert [call[4] for call in sticky_calls] == [None, 100]


@pytest.mark.parametrize(
    "mode,verdict,approval,event",
    [
        ("review_comment", "approve", PublisherApprovalPolicy(), "COMMENT"),
        (
            "review_verdict",
            "approve",
            PublisherApprovalPolicy(
                allow_approve=True, is_fork=False, baseline_clean=True,
            ),
            "APPROVE",
        ),
        ("review_verdict", "request_changes", PublisherApprovalPolicy(), "REQUEST_CHANGES"),
        ("review_verdict", "notice", PublisherApprovalPolicy(), "COMMENT"),
    ],
)
def test_submitted_specialist_review_body_uses_cleanup_compatible_marker(
    tmp_path, mode, verdict, approval, event
):
    client = _FakeClient()
    _publish(
        tmp_path,
        client,
        mode=mode,
        verdict=verdict,
        approval=approval,
    )
    submit = [call for call in client.calls if call[0] == "submit"][0]

    assert submit[2] == event
    assert submit[3].startswith("<!-- ai-pr-reviewer")
    helper = (Path(__file__).parents[1] / "scripts" / "publish_helpers.sh").read_text(
        encoding="utf-8"
    )
    assert 'startswith("<!-- ai-pr-reviewer")' in helper


def test_invalid_sticky_success_object_fails_closed_before_query(tmp_path):
    class InvalidStickyClient(_FakeClient):
        def update_sticky(self, repo, pr_number, body):
            self.calls.append(("sticky", repo, pr_number, body))
            return {}

    client = InvalidStickyClient()
    result, _state = _publish(tmp_path, client, notes=(_note(),))

    assert [call[0] for call in client.calls] == ["query", "sticky"]
    assert result["publication_errors"][0]["operation"] == "update_sticky"


def test_invalid_final_sticky_refresh_is_not_checkpointed_as_success(tmp_path):
    class InvalidRefreshClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.sticky_count = 0

        def update_sticky(self, repo, pr_number, body):
            self.calls.append(("sticky", repo, pr_number, body))
            self.sticky_count += 1
            if self.sticky_count == 1:
                return {"id": 100, "url": _issue_url(100)}
            return {}

    result, _state = _publish(tmp_path, InvalidRefreshClient(), notes=())

    assert result["sticky"] == {"id": 100, "url": _issue_url(100)}
    assert not any(item["operation"] == "refresh_sticky" for item in result["journal"])
    assert any(error["operation"] == "refresh_sticky" for error in result["publication_errors"])


def test_failed_submit_records_no_desired_event_url_or_final_success_link(tmp_path):
    class SubmitFailureClient(_FakeClient):
        def submit_review(self, review_id, event, body):
            self.calls.append(("submit", review_id, event, body))
            return {}

    client = SubmitFailureClient()
    result, _state = _publish(tmp_path, client, notes=())

    assert result["review"]["status"] == "submission_failed"
    assert result["review_completed"] is False
    assert result["review"].get("event") is None
    assert result["review"].get("url") is None
    assert [call[0] for call in client.calls].count("sticky") == 1
    assert not any(item["operation"] == "refresh_sticky" for item in result["journal"])


def test_publisher_rejects_valid_shape_submit_with_wrong_terminal_state(tmp_path):
    class WrongStateClient(_FakeClient):
        def submit_review(self, review_id, event, body):
            self.calls.append(("submit", review_id, event, body))
            return {
                "id": review_id,
                "url": _review_url(181),
                "state": "APPROVED",
                "body": body,
            }

    result, _state = _publish(tmp_path, WrongStateClient(), notes=())

    assert result["review"]["status"] == "submission_failed"
    assert result["review_completed"] is False


def test_resolve_requires_confirmed_resolved_thread(tmp_path):
    class FalseResolveClient(_FakeClient):
        def resolve_thread(self, thread_id):
            self.calls.append(("resolve", thread_id))
            return {"id": thread_id, "is_resolved": False}

    client = FalseResolveClient({
        "pull_request_id": "PR-node",
        "threads": [{
            "fingerprint": "fp-fixed",
            "generation": 1,
            "thread_id": "thread-fixed",
            "comment_id": 44,
            "url": _discussion_url(44),
            "is_resolved": False,
        }],
        "general_comments": [],
        "reviews": [],
    })
    result, state = _publish(tmp_path, client, notes=())
    fixed = [item for item in state["notes"] if item["fingerprint"] == "fp-fixed"][0]

    assert fixed["resolution"] == "publication_failed"
    assert fixed["confirmed"] is False
    assert result["review"]["status"] == "pending_incomplete"
    assert not any(call[0] == "submit" for call in client.calls)
    assert any(error["operation"] == "resolve_thread" for error in state["publication_errors"])


def test_resolution_is_not_attempted_when_owned_resolution_reply_is_unconfirmed(tmp_path):
    class ReplyFailureClient(_FakeClient):
        def reply_thread(self, comment_id, body):
            self.calls.append(("reply", comment_id, body))
            raise TimeoutError("unconfirmed")

    client = ReplyFailureClient({
        "pull_request_id": "PR-node",
        "threads": [{
            "fingerprint": "fp-unconfirmed-resolution",
            "generation": 1,
            "thread_id": "thread-fixed",
            "comment_id": 44,
            "url": _discussion_url(44),
            "is_resolved": False,
            "owned_comment_bodies": (),
        }],
        "general_comments": [],
        "reviews": [],
    })

    result, state = _publish(tmp_path, client, notes=())

    assert not any(call[0] == "resolve" for call in client.calls)
    note = next(
        item
        for item in state["notes"]
        if item["fingerprint"] == "fp-unconfirmed-resolution"
    )
    assert note["resolution"] == "publication_failed"
    assert note["confirmed"] is False
    assert result["review"]["status"] == "pending_incomplete"
    assert not any(call[0] == "submit" for call in client.calls)


def test_invalid_pending_review_object_stops_thread_and_submit_mutations(tmp_path):
    class InvalidPendingClient(_FakeClient):
        def create_pending_review(self, pull_request_id, head_sha, body):
            self.calls.append(("pending", pull_request_id, head_sha, body))
            return {"id": "review-without-url"}

    client = InvalidPendingClient()
    result, _state = _publish(tmp_path, client, notes=(_note(),))

    assert not any(call[0] in {"add", "submit"} for call in client.calls)
    assert any(error["operation"] == "create_pending_review" for error in result["publication_errors"])


def test_invalid_created_thread_object_is_not_persisted_as_success(tmp_path):
    class InvalidThreadClient(_FakeClient):
        def add_review_thread(self, variables):
            self.calls.append(("add", variables))
            return {"id": "thread-without-url"}

    client = InvalidThreadClient()
    _result, state = _publish(tmp_path, client, notes=(_note("fp-invalid"),))
    note = [item for item in state["notes"] if item["fingerprint"] == "fp-invalid"][0]

    assert note["resolution"] == "publication_failed"
    assert note["id"] is None
    assert not any(call[0] == "submit" for call in client.calls)
    assert state["review"]["status"] == "pending_incomplete"
    assert not any(item["operation"] == "refresh_sticky" for item in state["journal"])


def test_incomplete_thread_publication_resumes_same_pending_review_next_run(tmp_path):
    class FirstRun(_FakeClient):
        def add_review_thread(self, variables):
            self.calls.append(("add", variables))
            return {}

    first = FirstRun()
    first_result, _first_state = _publish(
        tmp_path, first, notes=(_note("fp-resume"),)
    )
    pending = next(call for call in first.calls if call[0] == "pending")
    publication_id = first_result["publication_id"]
    assert not any(call[0] == "submit" for call in first.calls)
    assert first_result["review"]["status"] == "pending_incomplete"
    second = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [],
        "general_comments": [],
        "reviews": [{
            "id": "review-id",
            "url": _review_url(81),
            "body": pending[3],
            "state": "PENDING",
        }],
    })

    second_result, _second_state = _publish(
        tmp_path, second, notes=(_note("fp-resume"),)
    )

    assert second_result["publication_id"] == publication_id
    assert second_result["journal"][: len(first_result["journal"])] == first_result["journal"]
    assert not any(call[0] == "pending" for call in second.calls)
    assert any(call[0] == "add" for call in second.calls)
    assert any(call[0] == "submit" for call in second.calls)


def test_thread_timeout_does_not_reconcile_cross_publication_fingerprint(tmp_path):
    class CollisionClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.query_count = 0
            self.pending_body = ""

        def create_pending_review(self, pull_request_id, head_sha, body):
            self.pending_body = body
            return super().create_pending_review(pull_request_id, head_sha, body)

        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            self.query_count += 1
            if self.query_count == 1:
                return self.managed_state
            publication_id = self.pending_body.split(":", 1)[1].split(":", 1)[0]
            return {
                **self.managed_state,
                "threads": [{
                    "fingerprint": "fp-collision",
                    "generation": 1,
                    "publication_id": publication_id,
                    "review_id": "review-id",
                    "review_body": self.pending_body,
                    "review_state": "PENDING",
                    "head_sha": None,
                    "thread_id": "thread-other",
                    "comment_id": 90,
                    "url": _discussion_url(90),
                    "is_resolved": False,
                }],
            }

        def add_review_thread(self, variables):
            self.calls.append(("add", variables))
            raise TimeoutError("ambiguous")

    result, state = _publish(
        tmp_path, CollisionClient(), notes=(_note("fp-collision"),)
    )
    note = next(item for item in state["notes"] if item["fingerprint"] == "fp-collision")

    assert note["resolution"] == "publication_failed"
    assert result["review"]["status"] == "pending_incomplete"


def test_specialist_publish_cli_loads_only_typed_final_artifacts(tmp_path, monkeypatch):
    from scripts import publish_specialist_review as cli

    inputs = {
        "handoff.json": {
            "markdown": "Sparse handoff",
            "recommendation": "Approve",
            "status": "AI review complete",
            "change_map": ["Component: worker"],
            "reviewed_focuses": [
                "Failure recovery",
                "Repository recipe: delivery",
            ],
            "specialist_focuses": [],
            "recipe_focuses": ["Repository recipe: delivery"],
            "coverage_boundaries": ["Failure recovery"],
            "review_emphasis": ["Failure recovery"],
            "what_changed": ["`a.py` changes runtime behavior."],
            "ai_reviewed": ["Reviewed runtime behavior in `a.py`."],
            "human_focus": ["Failure recovery"],
        },
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
            return {
                "mode": kwargs["mode"],
                "review_completed": True,
                "publication_errors": [],
            }

    monkeypatch.setattr(cli, "GhReviewClient", lambda **_kwargs: object())
    monkeypatch.setattr(cli, "GitHubReviewPublisher", FakePublisher)

    assert cli.main([
        "--mode", "review_comment",
        "--handoff", str(tmp_path / "handoff.json"),
        "--notes", str(tmp_path / "notes.json"),
        "--diff", str(tmp_path / "pr.diff"),
        "--files", str(tmp_path / "files.json"),
        "--changed-files-complete", "true",
        "--changed-files-count", "1",
        "--diff-complete", "false",
        "--policy-result", str(tmp_path / "policy.json"),
        "--artifacts", str(tmp_path / "artifacts.json"),
        "--repo", "owner/repo",
        "--pr-number", "17",
        "--head-sha", "abc123",
        "--state", str(tmp_path / "state.json"),
        "--action-root", str(tmp_path),
    ]) == 0
    handoff = captured["publish"]["handoff"]
    assert isinstance(handoff, ReviewHandoff)
    assert handoff.status == "AI review complete"
    assert handoff.specialist_focuses == ()
    assert handoff.recipe_focuses == ("Repository recipe: delivery",)
    assert handoff.coverage_boundaries == ("Failure recovery",)
    assert handoff.what_changed == ("`a.py` changes runtime behavior.",)
    assert handoff.ai_reviewed == ("Reviewed runtime behavior in `a.py`.",)
    assert handoff.human_focus == ("Failure recovery",)
    assert isinstance(captured["publish"]["notes"][0], ReviewNote)
    assert isinstance(captured["publish"]["policy_result"], RuntimeVerdictPolicyResult)
    assert captured["publish"]["changed_files"] == ("a.py",)
    assert captured["publish"]["changed_files_complete"] is True
    assert captured["publish"]["diff_complete"] is False
    assert "transcript" not in captured["publish"]
    assert "evidence_store" not in captured["publish"]


def test_specialist_publish_cli_exits_nonzero_on_partial_publication(
    tmp_path, monkeypatch,
):
    from scripts import publish_specialist_review as cli

    for name, value in {
        "handoff.json": {"markdown": "Sparse handoff"},
        "notes.json": [],
        "files.json": [{"filename": "a.py"}],
        "policy.json": {"verdict": "approve", "source": "policy"},
    }.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    (tmp_path / "pr.diff").write_text(DIFF, encoding="utf-8")

    class PartialPublisher:
        def __init__(self, *_args, **_kwargs):
            pass

        def publish(self, **_kwargs):
            return {
                "review_completed": False,
                "publication_errors": [{
                    "operation": "update_sticky", "error": "denied",
                }],
            }

    monkeypatch.setattr(cli, "GhReviewClient", lambda **_kwargs: object())
    monkeypatch.setattr(cli, "GitHubReviewPublisher", PartialPublisher)

    assert cli.main([
        "--mode", "comment",
        "--handoff", str(tmp_path / "handoff.json"),
        "--notes", str(tmp_path / "notes.json"),
        "--diff", str(tmp_path / "pr.diff"),
        "--files", str(tmp_path / "files.json"),
        "--changed-files-complete", "true",
        "--changed-files-count", "1",
        "--policy-result", str(tmp_path / "policy.json"),
        "--repo", "owner/repo",
        "--pr-number", "17",
        "--head-sha", "a" * 40,
    ]) == 1


def test_cli_file_normalization_handles_pr_file_objects_and_explicit_note_entries():
    from scripts import publish_specialist_review as cli

    value = [
        {"filename": "a.py", "status": "modified"},
        {"note": "file list truncated to first 100 of 123 changed files"},
    ]

    assert cli._changed_files(value, complete=False, expected_count=None) == ("a.py",)
    with pytest.raises(ValueError, match="incomplete"):
        cli._changed_files(value, complete=True, expected_count=123)
    with pytest.raises(ValueError, match="count"):
        cli._changed_files([{"filename": "a.py"}], complete=True, expected_count=2)
    with pytest.raises(ValueError, match="required"):
        cli._changed_files([{"filename": "a.py"}], complete=True, expected_count=None)
    with pytest.raises(ValueError, match="entry"):
        cli._changed_files([{"unexpected": "value"}], complete=False, expected_count=None)


def test_cli_pr_file_objects_reach_real_publisher_as_complete_paths(tmp_path, monkeypatch):
    from scripts import publish_specialist_review as cli

    inputs = {
        "handoff.json": {"markdown": "Sparse handoff"},
        "notes.json": [],
        "files.json": [{"filename": "a.py", "status": "modified"}],
        "policy.json": {"verdict": "approve", "source": "policy"},
    }
    for name, value in inputs.items():
        (tmp_path / name).write_text(json.dumps(value), encoding="utf-8")
    (tmp_path / "pr.diff").write_text(DIFF, encoding="utf-8")
    client = _FakeClient()
    monkeypatch.setattr(cli, "GhReviewClient", lambda **_kwargs: client)

    assert cli.main([
        "--mode", "comment",
        "--handoff", str(tmp_path / "handoff.json"),
        "--notes", str(tmp_path / "notes.json"),
        "--diff", str(tmp_path / "pr.diff"),
        "--files", str(tmp_path / "files.json"),
        "--changed-files-complete", "true",
        "--changed-files-count", "1",
        "--diff-complete", "false",
        "--policy-result", str(tmp_path / "policy.json"),
        "--repo", "owner/repo",
        "--pr-number", "17",
        "--head-sha", "a" * 40,
        "--state", str(tmp_path / "state.json"),
        "--action-root", str(tmp_path),
    ]) == 0
    assert [call[0] for call in client.calls] == ["sticky"]
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["changed_files_complete"] is True


def test_diff_positions_preserves_linux_backslash_filename():
    from pr_reviewer.github_review_notes import diff_positions

    diff = """\
diff --git "a/dir\\name.py" "b/dir\\name.py"
--- "a/dir\\name.py"
+++ b/dir\\name.py
@@ -1 +1 @@
-old
+new
"""
    assert diff_positions(diff) == {"dir\\name.py": {1: 2}}


def _connection(nodes=(), *, has_next=False, cursor=None):
    return {
        "nodes": list(nodes),
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
    }


def test_managed_state_paginates_threads_comments_and_thread_comments(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)
    calls = []

    thread_pages = {
        None: _connection([{"id": "T1", "isResolved": False}], has_next=True, cursor="tc1"),
        "tc1": _connection([{"id": "T2", "isResolved": False}]),
    }
    thread_comment_pages = {
        ("T1", None): _connection([{
            "databaseId": 11,
            "url": _discussion_url(11),
            "body": "human starter",
            "viewerDidAuthor": False,
            "author": {"login": "human"},
        }]),
        ("T2", None): _connection([{
            "databaseId": 22,
            "url": _discussion_url(22),
            "body": "<!-- ai-pr-review-note:fp-page2 generation=1 -->",
            "viewerDidAuthor": True,
            "author": {"login": "bot"},
        }], has_next=True, cursor="cc1"),
        ("T2", "cc1"): _connection([{
            "databaseId": 23,
            "url": _discussion_url(23),
            "body": "owned status reply",
            "viewerDidAuthor": True,
            "author": {"login": "bot"},
        }]),
    }

    def fake_graphql(query, variables):
        calls.append((query, dict(variables)))
        if "ManagedReviewIdentity" in query:
            return {"data": {"viewer": {"login": "bot"}, "repository": {
                    "pullRequest": {
                        "id": "PR-node", "changedFiles": 1, "headRefOid": "a" * 40
                    }
            }}}
        if "ManagedReviewThreads" in query:
            return {"data": {"node": {"reviewThreads": thread_pages[variables.get("cursor")]}}}
        if "ManagedThreadComments" in query:
            key = (variables["threadId"], variables.get("cursor"))
            return {"data": {"node": {"comments": thread_comment_pages[key]}}}
        if "ManagedIssueComments" in query:
            page = variables.get("cursor")
            nodes = [{
                "databaseId": 101 if page is None else 202,
                "url": _issue_url(101 if page is None else 202),
                "body": "ordinary" if page is None else "<!-- ai-pr-review-general:fp-general -->",
                "viewerDidAuthor": page is not None,
                "author": {"login": "bot" if page is not None else "human"},
            }]
            return {"data": {"node": {"comments": _connection(
                nodes, has_next=page is None, cursor="ic1" if page is None else None
            )}}}
        if "ManagedReviews" in query:
            return {"data": {"node": {"reviews": _connection([])}}}
        raise AssertionError(query)

    monkeypatch.setattr(client, "_graphql", fake_graphql)
    monkeypatch.setattr(client, "list_changed_files", lambda *_args: ("a.py",), raising=False)
    state = client.query_managed_state("owner/repo", 17)

    assert [item["fingerprint"] for item in state["threads"]] == ["fp-page2"]
    assert state["threads"][0]["generation"] == 1
    assert [item["fingerprint"] for item in state["general_comments"]] == ["fp-general"]
    assert state["changed_files"] == ("a.py",)
    assert state["changed_files_complete"] is True
    assert sum("ManagedReviewThreads" in query for query, _ in calls) == 2
    assert sum("ManagedIssueComments" in query for query, _ in calls) == 2
    assert sum("ManagedThreadComments" in query for query, _ in calls) == 3


def test_managed_state_legacy_answer_marker_stops_fingerprint_before_sha(
    monkeypatch, tmp_path
):
    client = GhReviewClient(action_root=tmp_path)
    legacy_sha = "c" * 40

    def fake_graphql(query, _variables):
        if "ManagedReviewIdentity" in query:
            return {"data": {"viewer": {"login": "bot"}, "repository": {
                "pullRequest": {
                    "id": "PR-node", "changedFiles": 0, "headRefOid": "a" * 40
                }
            }}}
        if "ManagedIssueComments" in query:
            return {"data": {"node": {"comments": _connection([{
                "databaseId": 60,
                "url": _issue_url(60),
                "body": (
                    "answered\n<!-- ai-pr-review-general-answer:"
                    f"fp-legacy-answer:{legacy_sha} -->"
                ),
                "viewerDidAuthor": True,
                "author": {"login": "bot"},
            }])}}}
        connection = (
            "reviewThreads" if "ManagedReviewThreads" in query else "reviews"
        )
        return {"data": {"node": {connection: _connection([])}}}

    monkeypatch.setattr(client, "_graphql", fake_graphql)
    monkeypatch.setattr(client, "list_changed_files", lambda *_args: ())

    state = client.query_managed_state("owner/repo", 17)

    assert state["general_answered_fingerprints"] == ("fp-legacy-answer",)


def test_managed_state_rejects_incomplete_cursor_and_copied_starter_marker(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)

    def fake_graphql(query, variables):
        if "ManagedReviewIdentity" in query:
            return {"data": {"viewer": {"login": "bot"}, "repository": {
                "pullRequest": {
                    "id": "PR-node", "changedFiles": 0, "headRefOid": "a" * 40
                }
            }}}
        if "ManagedReviewThreads" in query:
            return {"data": {"node": {"reviewThreads": _connection(
                [{"id": "T-human", "isResolved": False}], has_next=True, cursor=None
            )}}}
        raise AssertionError(query)

    monkeypatch.setattr(client, "_graphql", fake_graphql)
    monkeypatch.setattr(client, "list_changed_files", lambda *_args: (), raising=False)
    with pytest.raises(RuntimeError, match="incomplete pagination"):
        client.query_managed_state("owner/repo", 17)


def test_changed_files_rest_pagination_is_complete_and_flat(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)
    pages = {
        1: [{"filename": f"src/file-{index}.py"} for index in range(100)],
        2: [{"filename": "src/file-100.py"}],
    }
    calls = []

    def fake_get(endpoint):
        calls.append(endpoint)
        page = int(endpoint.rsplit("page=", 1)[1])
        return pages[page]

    monkeypatch.setattr(client, "_api_get", fake_get, raising=False)
    result = client.list_changed_files("owner/repo", 17)

    assert len(result) == 101
    assert result[-1] == "src/file-100.py"
    assert calls == [
        "repos/owner/repo/pulls/17/files?per_page=100&page=1",
        "repos/owner/repo/pulls/17/files?per_page=100&page=2",
    ]


def test_managed_state_rejects_rest_file_count_mismatch(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)

    def fake_graphql(query, _variables):
        if "ManagedReviewIdentity" in query:
            return {"data": {"viewer": {"login": "bot"}, "repository": {
                "pullRequest": {
                    "id": "PR-node",
                    "changedFiles": 2,
                    "headRefOid": "a" * 40,
                }
            }}}
        connection = (
            "reviewThreads" if "ManagedReviewThreads" in query
            else "comments" if "ManagedIssueComments" in query
            else "reviews"
        )
        return {"data": {"node": {connection: _connection([])}}}

    monkeypatch.setattr(client, "_graphql", fake_graphql)
    monkeypatch.setattr(client, "list_changed_files", lambda *_args: ("a.py",))

    with pytest.raises(RuntimeError, match="changed-files count"):
        client.query_managed_state("owner/repo", 17)


def test_managed_state_detects_head_change_during_collection(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)
    identity_calls = 0

    def fake_graphql(query, _variables):
        nonlocal identity_calls
        if "ManagedReviewIdentity" in query:
            identity_calls += 1
            return {"data": {"viewer": {"login": "bot"}, "repository": {
                "pullRequest": {
                    "id": "PR-node",
                    "changedFiles": 1,
                    "headRefOid": ("a" if identity_calls == 1 else "b") * 40,
                }
            }}}
        connection = (
            "reviewThreads" if "ManagedReviewThreads" in query
            else "comments" if "ManagedIssueComments" in query
            else "reviews"
        )
        return {"data": {"node": {connection: _connection([])}}}

    monkeypatch.setattr(client, "_graphql", fake_graphql)
    monkeypatch.setattr(client, "list_changed_files", lambda *_args: ("a.py",))

    with pytest.raises(RuntimeError, match="changed during managed-state collection"):
        client.query_managed_state("owner/repo", 17)


def test_changed_files_page_cap_fails_closed(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)
    monkeypatch.setattr(
        client,
        "_api_get",
        lambda _endpoint: [{"filename": f"file-{index}.py"} for index in range(100)],
    )

    with pytest.raises(RuntimeError, match="page limit exceeded"):
        client.list_changed_files("owner/repo", 17)


def test_graphql_errors_and_missing_data_are_failures(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)

    def response(payload):
        def fake_run(argv, **_kwargs):
            return types.SimpleNamespace(
                returncode=0, stdout=json.dumps(payload).encode(), stderr=b""
            )
        return fake_run

    monkeypatch.setattr(
        "pr_reviewer.github_review_notes.subprocess.run",
        response({"errors": [{"message": "denied"}], "data": {}}),
    )
    with pytest.raises(RuntimeError, match="GraphQL errors"):
        client._graphql("query X { viewer { login } }", {})

    monkeypatch.setattr(
        "pr_reviewer.github_review_notes.subprocess.run", response({"data": None})
    )
    with pytest.raises(RuntimeError, match="missing data"):
        client._graphql("query X { viewer { login } }", {})


def test_managed_query_failure_stops_mutations_and_ignores_unversioned_state(tmp_path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "notes": [{"fingerprint": "prior-fp", "resolution": "open"}]
    }), encoding="utf-8")

    class QueryFailureClient(_FakeClient):
        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            raise RuntimeError("pagination incomplete")

    client = QueryFailureClient()
    publisher = GitHubReviewPublisher(client, state_path=state_path, max_attempts=1)
    result = publisher.publish(
        mode="review_comment",
        handoff=ReviewHandoff(markdown="Sparse handoff"),
        notes=(_note(),),
        diff_text=DIFF,
        changed_files=("a.py",),
        policy_result=RuntimeVerdictPolicyResult(verdict="approve", source="policy"),
        repo="owner/repo",
        pr_number=17,
        head_sha="a" * 40,
    )

    assert [call[0] for call in client.calls] == ["query"]
    assert result["notes"] == []
    assert result["publication_errors"][0]["operation"] == "query_managed_state"


def test_live_head_mismatch_fails_before_any_publication_mutation(tmp_path):
    client = _FakeClient({
        "pull_request_id": "PR-node",
        "head_ref_oid": "b" * 40,
        "changed_files": ("a.py", "b.py"),
        "changed_files_complete": True,
        "threads": [],
        "general_comments": [],
        "reviews": [],
    })

    result, _state = _publish(tmp_path, client, notes=())

    assert client.calls == []
    assert result["managed_state_complete"] is False
    assert any(error["operation"] == "live_identity" for error in result["publication_errors"])


def test_discovery_failure_preserves_only_matching_publication_journal_and_notes(tmp_path):
    class IncompleteClient(_FakeClient):
        def add_review_thread(self, variables):
            self.calls.append(("add", variables))
            return {}

    first = IncompleteClient()
    first_result, first_state = _publish(
        tmp_path, first, notes=(_note("fp-prior-journal"),)
    )

    class QueryFailureClient(_FakeClient):
        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            raise RuntimeError("discovery failed")

    second = QueryFailureClient()
    second_result, _second_state = _publish(
        tmp_path, second, notes=(_note("fp-prior-journal"),)
    )

    assert first_result["publication_id"] == second_result["publication_id"]
    assert second_result["journal"] == first_state["journal"]
    assert second_result["notes"] == first_state["notes"]
    assert len(second_result["publication_errors"]) >= len(first_state["publication_errors"])
    assert [call[0] for call in second.calls] == ["query"]


def test_discovery_failure_never_imports_mismatched_state_file(tmp_path):
    state_path = tmp_path / "publication-state.json"
    state_path.write_text(json.dumps({
        "version": 2,
        "repo": "other/repo",
        "pr_number": 99,
        "head_sha": "b" * 40,
        "publication_id": "wrong",
        "notes": [{"fingerprint": "foreign"}],
        "journal": [{"sequence": 1, "operation": "foreign"}],
        "publication_errors": [{"operation": "foreign", "error": "foreign"}],
    }), encoding="utf-8")

    class QueryFailureClient(_FakeClient):
        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            raise RuntimeError("discovery failed")

    client = QueryFailureClient()
    publisher = GitHubReviewPublisher(client, state_path=state_path, max_attempts=1)
    result = publisher.publish(
        mode="review_comment",
        handoff=ReviewHandoff(markdown="Sparse handoff"),
        notes=(),
        diff_text=DIFF,
        changed_files=("a.py", "b.py"),
        changed_files_complete=True,
        diff_complete=False,
        policy_result=RuntimeVerdictPolicyResult(verdict="approve", source="policy"),
        repo="owner/repo",
        pr_number=17,
        head_sha="a" * 40,
    )

    assert result["notes"] == []
    assert result["journal"] == []
    assert all(error["operation"] != "foreign" for error in result["publication_errors"])


def test_publisher_uses_complete_managed_files_for_file_101_and_tracks_diff_completeness(tmp_path):
    client = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [],
        "general_comments": [],
        "reviews": [],
        "changed_files": tuple(["a.py"] + [f"src/file-{index}.py" for index in range(101)]),
        "changed_files_complete": True,
    })
    publisher = GitHubReviewPublisher(client, state_path=tmp_path / "state.json", max_attempts=1)
    result = publisher.publish(
        mode="review_comment",
        handoff=ReviewHandoff(markdown="Sparse handoff"),
        notes=(_note("fp-late", file="src/file-100.py", line=999),),
        diff_text=DIFF,
        changed_files=("a.py",),
        changed_files_complete=False,
        diff_complete=False,
        policy_result=RuntimeVerdictPolicyResult(verdict="approve", source="policy"),
        repo="owner/repo",
        pr_number=17,
        head_sha="a" * 40,
    )

    add = [call for call in client.calls if call[0] == "add"][0]
    assert add[1]["subjectType"] == "FILE"
    assert add[1]["path"] == "src/file-100.py"
    assert result["changed_files_complete"] is True
    assert result["diff_complete"] is False


def test_complete_caller_files_must_exactly_match_live_file_identity(tmp_path):
    client = _FakeClient({
        "changed_files": ("a.py", "live-only.py"),
        "changed_files_count": 2,
    })

    result, _state = _publish(
        tmp_path, client, notes=(_note("fp-stale-file", file="b.py", line=99),)
    )

    assert [call[0] for call in client.calls] == ["query"]
    assert result["review_completed"] is False
    assert any(
        error["operation"] == "changed_files_snapshot"
        for error in result["publication_errors"]
    )


@pytest.mark.parametrize(
    ("mode", "verdict", "approval"),
    [
        ("review_comment", "approve", None),
        (
            "review_verdict",
            "request_changes",
            PublisherApprovalPolicy(effective_scope="full", baseline_clean=True),
        ),
    ],
)
def test_push_during_detail_mutations_keeps_review_pending(
    tmp_path, mode, verdict, approval
):
    class PushDuringMutationClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.query_count = 0

        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            self.query_count += 1
            return {
                **self.managed_state,
                "head_ref_oid": ("a" if self.query_count == 1 else "b") * 40,
            }

    client = PushDuringMutationClient()
    result, state = _publish(
        tmp_path,
        client,
        mode=mode,
        verdict=verdict,
        approval=approval,
        notes=(_note("fp-push-race"),),
    )

    assert any(call[0] == "add" for call in client.calls)
    assert not any(call[0] == "submit" for call in client.calls)
    assert [call[0] for call in client.calls].count("sticky") == 1
    assert result["review_completed"] is False
    assert state["review"]["status"] == "pending_incomplete"
    assert any(
        error["operation"] == "pre_submit_head_ref_oid"
        for error in state["publication_errors"]
    )


@pytest.mark.parametrize("field", [
    "allow_approve", "approve_forks", "is_fork", "baseline_clean",
])
def test_approval_policy_rejects_truthy_non_booleans(field):
    with pytest.raises((TypeError, ValueError), match="boolean"):
        PublisherApprovalPolicy(**{field: "true"})


def test_unknown_fork_identity_cannot_produce_native_approval(tmp_path):
    client = _FakeClient()
    result, _state = _publish(
        tmp_path,
        client,
        mode="review_verdict",
        approval=PublisherApprovalPolicy(
            allow_approve=True,
            approve_forks=True,
            is_fork=None,
            effective_scope="full",
            baseline_clean=True,
        ),
    )

    submit = next(call for call in client.calls if call[0] == "submit")
    assert submit[2] == "REQUEST_CHANGES"
    assert result["review"]["event"] == "REQUEST_CHANGES"


@pytest.mark.parametrize("repo,head_sha", [
    ("owner/repo/extra", "a" * 40),
    ("./repo", "a" * 40),
    ("../repo", "a" * 40),
    ("owner/.", "a" * 40),
    ("owner/..", "a" * 40),
    ("owner/repo", "abc123"),
    ("owner/repo", "A" * 40),
])
def test_direct_publisher_validation_happens_before_any_mutation(tmp_path, repo, head_sha):
    client = _FakeClient()
    publisher = GitHubReviewPublisher(client, state_path=tmp_path / "state.json")
    with pytest.raises(ValueError):
        publisher.publish(
            mode="review_comment",
            handoff=ReviewHandoff(markdown="Sparse"),
            notes=(),
            diff_text="",
            changed_files=(),
            changed_files_complete=True,
            diff_complete=False,
            policy_result=RuntimeVerdictPolicyResult(verdict="approve", source="policy"),
            repo=repo,
            pr_number=1,
            head_sha=head_sha,
        )
    assert client.calls == []
    assert not (tmp_path / "state.json").exists()


def test_direct_publisher_rejects_non_boolean_completeness_before_mutation(tmp_path):
    client = _FakeClient()
    publisher = GitHubReviewPublisher(client, state_path=tmp_path / "state.json")
    with pytest.raises(TypeError, match="boolean"):
        publisher.publish(
            mode="review_comment",
            handoff=ReviewHandoff(markdown="Sparse"),
            notes=(),
            diff_text="",
            changed_files=(),
            changed_files_complete="true",
            diff_complete=False,
            policy_result=RuntimeVerdictPolicyResult(verdict="approve", source="policy"),
            repo="owner/repo",
            pr_number=1,
            head_sha="a" * 40,
        )
    assert client.calls == []


@pytest.mark.parametrize(
    "override,error",
    [
        ({"notes": (object(),)}, TypeError),
        ({"diff_text": b"not text"}, TypeError),
        ({"changed_files": ("../escape.py",)}, ValueError),
        (
            {
                "policy_result": RuntimeVerdictPolicyResult(
                    verdict="maybe", source="invalid"
                )
            },
            ValueError,
        ),
    ],
)
def test_direct_publisher_rejects_invalid_final_artifacts_before_mutation(
    tmp_path, override, error
):
    client = _FakeClient()
    publisher = GitHubReviewPublisher(client, state_path=tmp_path / "state.json")
    arguments = {
        "mode": "review_comment",
        "handoff": ReviewHandoff(markdown="Sparse"),
        "notes": (),
        "diff_text": "",
        "changed_files": ("a.py",),
        "changed_files_complete": True,
        "diff_complete": False,
        "policy_result": RuntimeVerdictPolicyResult(verdict="approve", source="policy"),
        "repo": "owner/repo",
        "pr_number": 1,
        "head_sha": "a" * 40,
    }
    arguments.update(override)

    with pytest.raises(error):
        publisher.publish(**arguments)

    assert client.calls == []
    assert not (tmp_path / "state.json").exists()
