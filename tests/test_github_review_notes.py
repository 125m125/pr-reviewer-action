"""Specialist GitHub review-note anchoring and publication lifecycle."""

from __future__ import annotations

import json
import os
import stat
import types
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
        return {"id": 100, "url": "https://example/sticky"}

    def query_managed_state(self, repo, pr_number):
        self.calls.append(("query", repo, pr_number))
        return self.managed_state

    def reply_thread(self, comment_id, body):
        self.calls.append(("reply", comment_id, body))
        return {"id": 901, "url": f"https://example/{comment_id}/reply"}

    def resolve_thread(self, thread_id):
        self.calls.append(("resolve", thread_id))
        return {"id": thread_id, "is_resolved": True}

    def create_pending_review(self, pull_request_id, head_sha, body):
        self.calls.append(("pending", pull_request_id, head_sha, body))
        return {"id": "review-id", "url": "https://example/review"}

    def add_review_thread(self, variables):
        self.calls.append(("add", variables))
        fp = variables["body"].split("ai-pr-review-note:", 1)[1].split()[0]
        return {
            "id": f"thread-{fp}",
            "url": f"https://example/{fp}",
            "comment_id": 904,
        }

    def submit_review(self, review_id, event, body):
        self.calls.append(("submit", review_id, event, body))
        return {"id": review_id, "url": "https://example/submitted"}

    def upsert_general_comment(self, repo, pr_number, prior, body):
        self.calls.append(("general", repo, pr_number, prior, body))
        return {"id": 902, "url": "https://example/general"}

    def reply_general_comment(self, repo, pr_number, prior, body):
        self.calls.append(("general_reply", repo, pr_number, prior, body))
        return {"id": 903, "url": "https://example/general-reply"}


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
    assert "abcdefghijklmnopqrstuvwxyz123456" not in body
    assert "[\u200b" not in body
    assert "\\]\\[injected\\]" in body
    assert "](<https://example/artifact/path>)" in body


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
        "sticky",
    ]
    assert [call for call in client.calls if call[0] == "submit"][0][2] == "COMMENT"
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
                "url": "https://example/human",
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
    marker = f"<!-- ai-pr-review-status:fp-1:g1:{'a' * 40}:human-resolved -->"
    client = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [{
            "fingerprint": "fp-1",
            "generation": 1,
            "thread_id": "thread-human",
            "comment_id": 12,
            "url": "https://example/human",
            "is_resolved": True,
            "resolved_by_publisher": False,
            "owned_comment_bodies": (marker,),
        }],
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
            "url": "https://example/publisher",
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


def test_same_open_status_reply_is_deduplicated_for_head(tmp_path):
    marker = f"<!-- ai-pr-review-status:fp-1:g1:{'a' * 40}:open -->"
    client = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [{
            "fingerprint": "fp-1",
            "generation": 1,
            "thread_id": "thread-open",
            "comment_id": 12,
            "url": "https://example/open",
            "is_resolved": False,
            "resolved_by_publisher": False,
            "owned_comment_bodies": (marker,),
        }],
        "general_comments": [],
        "reviews": [],
    })
    _publish(tmp_path, client, notes=(_note(),))

    assert not any(call[0] == "reply" for call in client.calls)


def test_general_answer_followup_is_deduplicated(tmp_path):
    client = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [],
        "general_comments": [{
            "fingerprint": "fp-general",
            "id": 15,
            "url": "https://example/general",
        }],
        "general_answered_fingerprints": ("fp-general",),
        "reviews": [],
    })
    _publish(tmp_path, client, notes=())

    assert not any(call[0] == "general_reply" for call in client.calls)


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
        "sticky", "query", "general", "pending", "submit", "sticky"
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
    assert state["review_completed"] is True
    assert state["publication_errors"][0]["operation"] == "add_review_thread"
    assert "publication unavailable" in state["publication_errors"][0]["error"]


def test_non_idempotent_thread_create_is_not_blindly_retried(tmp_path):
    class FailingCreateClient(_FakeClient):
        def add_review_thread(self, variables):
            self.calls.append(("add", variables))
            raise TimeoutError("response lost")

    client = FailingCreateClient()
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

        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            self.query_count += 1
            if self.query_count == 1:
                return self.managed_state
            return {
                **self.managed_state,
                "threads": [{
                    "fingerprint": "fp-timeout",
                    "generation": 1,
                    "thread_id": "thread-timeout",
                    "comment_id": 55,
                    "url": "https://example/fp-timeout",
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
    assert [call[0] for call in client.calls].count("query") == 2
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

        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            self.query_count += 1
            if self.query_count == 1:
                return self.managed_state
            return {
                **self.managed_state,
                "reviews": [{
                    "id": "review-timeout",
                    "url": "https://example/review-timeout",
                    "body": f"<!-- ai-pr-reviewer-specialist:{head_sha} -->",
                    "state": "PENDING",
                }],
            }

        def create_pending_review(self, pull_request_id, received_sha, body):
            self.calls.append(("pending", pull_request_id, received_sha, body))
            raise TimeoutError("server committed before timeout")

    client = AmbiguousPendingClient()
    result, _state = _publish(tmp_path, client, notes=())

    assert [call[0] for call in client.calls].count("pending") == 1
    assert [call[0] for call in client.calls].count("query") == 2
    submit = [call for call in client.calls if call[0] == "submit"][0]
    assert submit[1] == "review-timeout"
    assert any(item["operation"] == "reconcile_create_pending_review" for item in result["journal"])


def test_timeout_after_submit_reconciles_by_owned_review_marker(tmp_path):
    marker = f"<!-- ai-pr-reviewer-specialist:{'a' * 40} -->"

    class SubmitTimeoutClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.query_count = 0

        def query_managed_state(self, repo, pr_number):
            self.calls.append(("query", repo, pr_number))
            self.query_count += 1
            state = dict(self.managed_state)
            state["reviews"] = [] if self.query_count == 1 else [{
                "id": "review-id",
                "url": "https://example/submitted-after-timeout",
                "body": marker + "\nAutomated specialist review notes.",
                "state": "COMMENTED",
            }]
            return state

        def submit_review(self, review_id, event, body):
            self.calls.append(("submit", review_id, event, body))
            raise TimeoutError("server committed before timeout")

    result, _state = _publish(tmp_path, SubmitTimeoutClient(), notes=(_note(),))

    assert result["review"]["url"] == "https://example/submitted-after-timeout"
    assert any(item["operation"] == "reconcile_submit_review" for item in result["journal"])


def test_completed_owned_review_marker_makes_rerun_idempotent(tmp_path):
    marker = f"<!-- ai-pr-reviewer-specialist:{'a' * 40} -->"
    client = _FakeClient({
        "pull_request_id": "PR-node",
        "threads": [],
        "general_comments": [],
        "reviews": [{
            "id": "review-complete",
            "url": "https://example/review-complete",
            "body": marker + "\nAutomated specialist review notes.",
            "state": "COMMENTED",
        }],
    })

    result, _state = _publish(tmp_path, client, notes=())

    assert not any(call[0] in {"pending", "add", "submit"} for call in client.calls)
    assert result["review"]["id"] == "review-complete"
    assert result["sticky"]["url"] == "https://example/sticky"


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
                    "url": "https://example/fixed",
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
                    "url": "https://example/open",
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
            marker = f"<!-- ai-pr-review-status:fp-open:g1:{'a' * 40}:open -->"
            state = dict(self.managed_state)
            state["threads"] = [{
                **self.managed_state["threads"][0],
                "owned_comment_bodies": (marker,),
            }]
            return state

        def reply_thread(self, comment_id, body):
            self.calls.append(("reply", comment_id, body))
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
    client = InterruptClient()
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
    assert any(item["operation"] == "add_review_thread" for item in checkpoint["journal"])
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
            response = {"id": 12, "html_url": "https://example/reply"}
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
    monkeypatch.setattr(client, "find_specialist_handoff", lambda *_args: {
        "id": 88, "url": "https://example/handoff"
    }, raising=False)
    monkeypatch.setattr(
        client,
        "_api_write",
        lambda endpoint, method, payload: writes.append((endpoint, method, payload)) or {
            "id": 88, "url": "https://example/handoff"
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
    assert result == {"id": 88, "url": "https://example/handoff"}


def test_sticky_post_timeout_reconciles_exact_owned_body_without_retry(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)
    body = "<!-- ai-pr-review-specialist-handoff -->\nSparse handoff"
    finds = []

    def find(_repo, _pr_number, expected_body=None):
        finds.append(expected_body)
        if expected_body == body:
            return {"id": 74, "url": "https://example/sticky-74"}
        return None

    writes = []

    def ambiguous_write(endpoint, method, payload):
        writes.append((endpoint, method, payload))
        raise TimeoutError("server committed before timeout")

    monkeypatch.setattr(client, "find_specialist_handoff", find)
    monkeypatch.setattr(client, "_api_write", ambiguous_write)

    assert client.update_sticky("owner/repo", 17, body) == {
        "id": 74,
        "url": "https://example/sticky-74",
    }
    assert len(writes) == 1
    assert finds == [None, body]


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
            {"id": 75, "url": "https://example/general-75"}
            if expected == body
            else None
        ),
        raising=False,
    )

    assert client.upsert_general_comment("owner/repo", 17, None, body) == {
        "id": 75,
        "url": "https://example/general-75",
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
            return {"data": {"submitPullRequestReview": {"pullRequestReview": {"url": "https://example/review"}}}}
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
    assert sticky_calls[-1][3].count("https://example/submitted") == 1
    assert "Detailed managed review" in sticky_calls[-1][3]


@pytest.mark.parametrize(
    "mode,verdict,approval,event",
    [
        ("review_comment", "approve", PublisherApprovalPolicy(), "COMMENT"),
        (
            "review_verdict",
            "approve",
            PublisherApprovalPolicy(allow_approve=True, baseline_clean=True),
            "APPROVE",
        ),
        ("review_verdict", "request_changes", PublisherApprovalPolicy(), "REQUEST_CHANGES"),
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

    assert [call[0] for call in client.calls] == ["sticky"]
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
                return {"id": 100, "url": "https://example/sticky"}
            return {}

    result, _state = _publish(tmp_path, InvalidRefreshClient(), notes=())

    assert result["sticky"] == {"id": 100, "url": "https://example/sticky"}
    assert not any(item["operation"] == "refresh_sticky" for item in result["journal"])
    assert any(error["operation"] == "refresh_sticky" for error in result["publication_errors"])


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
            "url": "https://example/fixed",
            "is_resolved": False,
        }],
        "general_comments": [],
        "reviews": [],
    })
    _result, state = _publish(tmp_path, client, notes=())
    fixed = [item for item in state["notes"] if item["fingerprint"] == "fp-fixed"][0]

    assert fixed["resolution"] == "resolution_failed"
    assert any(error["operation"] == "resolve_thread" for error in state["publication_errors"])


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
        "--changed-files-complete", "true",
        "--diff-complete", "false",
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
    assert captured["publish"]["changed_files_complete"] is True
    assert captured["publish"]["diff_complete"] is False
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


def test_legacy_diff_adapter_preserves_linux_backslash_filename():
    from pr_reviewer.github_review_notes import legacy_diff_positions

    diff = """\
diff --git "a/dir\\name.py" "b/dir\\name.py"
--- "a/dir\\name.py"
+++ b/dir\\name.py
@@ -1 +1 @@
-old
+new
"""
    assert legacy_diff_positions(diff) == {"dir\\name.py": {1: 2}}


def test_publish_helper_exposes_specialist_compatibility_wrapper():
    helper = (
        Path(__file__).resolve().parent.parent / "scripts" / "publish_helpers.sh"
    ).read_text(encoding="utf-8")
    assert "publish_specialist_review()" in helper
    assert "scripts/publish_specialist_review.py" in helper


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
            "url": "https://example/T1",
            "body": "human starter",
            "viewerDidAuthor": False,
            "author": {"login": "human"},
        }]),
        ("T2", None): _connection([{
            "databaseId": 22,
            "url": "https://example/T2",
            "body": "<!-- ai-pr-review-note:fp-page2 generation=1 -->",
            "viewerDidAuthor": True,
            "author": {"login": "bot"},
        }], has_next=True, cursor="cc1"),
        ("T2", "cc1"): _connection([{
            "databaseId": 23,
            "url": "https://example/T2-reply",
            "body": "owned status reply",
            "viewerDidAuthor": True,
            "author": {"login": "bot"},
        }]),
    }

    def fake_graphql(query, variables):
        calls.append((query, dict(variables)))
        if "ManagedReviewIdentity" in query:
            return {"data": {"viewer": {"login": "bot"}, "repository": {
                "pullRequest": {"id": "PR-node"}
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
                "url": f"https://example/general-{page or 'first'}",
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


def test_managed_state_rejects_incomplete_cursor_and_copied_starter_marker(monkeypatch, tmp_path):
    client = GhReviewClient(action_root=tmp_path)

    def fake_graphql(query, variables):
        if "ManagedReviewIdentity" in query:
            return {"data": {"viewer": {"login": "bot"}, "repository": {
                "pullRequest": {"id": "PR-node"}
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


def test_managed_query_failure_stops_detailed_mutations_and_preserves_prior_state(tmp_path):
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

    assert [call[0] for call in client.calls] == ["sticky", "query"]
    assert result["notes"] == [{"fingerprint": "prior-fp", "resolution": "open"}]
    assert result["publication_errors"][0]["operation"] == "query_managed_state"


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


@pytest.mark.parametrize("field", [
    "allow_approve", "approve_forks", "is_fork", "baseline_clean",
])
def test_approval_policy_rejects_truthy_non_booleans(field):
    with pytest.raises((TypeError, ValueError), match="boolean"):
        PublisherApprovalPolicy(**{field: "true"})


@pytest.mark.parametrize("repo,head_sha", [
    ("owner/repo/extra", "a" * 40),
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
