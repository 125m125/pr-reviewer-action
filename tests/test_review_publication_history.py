"""Publication-history integration tests; all GitHub calls stay in memory."""
import pytest

from test_github_review_notes import _FakeClient, _publish, _note, ReviewNoteKind
from test_access_request_reconciliation import REPO_BODY
from pr_reviewer.github_review_notes import GhReviewClient
from pr_reviewer.github_review_notes import PublisherApprovalPolicy


RUN = "https://github.com/owner/repo/actions/runs/123"


class AccessClient(_FakeClient):
    def __init__(self, body=REPO_BODY):
        super().__init__({"general_comments": [{
            "id": 123, "node_id": "access-node", "url": "https://github.com/owner/repo/issues/17#issuecomment-123",
            "body": body, "fingerprint": "source_access_request:abc",
        }]})

    def annotate_access_request(self, repo, pr_number, prior, body):
        self.calls.append(("annotate", prior["id"], body))
        self.managed_state["general_comments"][0]["body"] = body
        return {"id": prior["id"], "url": prior["url"]}


def test_granted_access_keeps_original_and_minimizes_after_publication(tmp_path):
    client = AccessClient()
    result, _ = _publish(tmp_path, client, allowed_github_repositories=("vendor/library",), run_url=RUN)
    assert result["review_completed"] and not result["publication_errors"]
    operations = [call[0] for call in client.calls]
    assert operations.index("submit") < operations.index("annotate") < operations.index("minimize")
    assert next(call[2] for call in client.calls if call[0] == "annotate").endswith(REPO_BODY)
    assert ("minimize", "access-node", "RESOLVED") in client.calls
    assert "general_reply" not in operations


def test_missing_request_does_not_mean_access_was_granted(tmp_path):
    client = AccessClient()
    result, _ = _publish(tmp_path, client, run_url=RUN)
    assert result["review_completed"]
    assert not any(call[0] in {"annotate", "general_reply", "minimize"} for call in client.calls)


def test_comment_mode_also_reconciles_old_access_requests(tmp_path):
    client = AccessClient()
    result, _ = _publish(tmp_path, client, mode="comment", allowed_github_repositories=("vendor/library",), run_url=RUN)
    assert result["review_completed"]
    assert ("minimize", "access-node", "RESOLVED") in client.calls
    assert not any(call[0] == "submit" for call in client.calls)


def test_failed_minimization_can_recover_on_retry(tmp_path):
    class Flaky(AccessClient):
        fail = True
        def minimize_comment(self, node_id, classifier):
            if self.fail:
                raise RuntimeError("temporary network failure")
            return super().minimize_comment(node_id, classifier)
    client = Flaky()
    first, _ = _publish(tmp_path, client, allowed_github_repositories=("vendor/library",), run_url=RUN)
    assert first["publication_errors"]
    submitted = next(call for call in client.calls if call[0] == "submit")
    client.managed_state["reviews"] = [{"id": "review-id", "url": first["review"]["url"], "state": "COMMENTED", "body": submitted[3]}]
    client.fail = False
    second, _ = _publish(tmp_path, client, allowed_github_repositories=("vendor/library",), run_url=RUN)
    assert second["review_completed"] and not second["publication_errors"]


def test_annotation_failure_never_minimizes_access_request(tmp_path):
    class Failing(AccessClient):
        def annotate_access_request(self, *args):
            return {"id": 999, "url": "https://github.com/owner/repo/issues/17#issuecomment-999"}
    client = Failing()
    result, _ = _publish(tmp_path, client, allowed_github_repositories=("vendor/library",), run_url=RUN)
    assert result["review_completed"]
    assert result["publication_errors"]
    assert not any(call[0] == "minimize" for call in client.calls)


def test_cleanup_opt_out_does_not_disable_access_reconciliation(tmp_path):
    client = AccessClient()
    client.managed_state["reviews"] = [{"id": "old", "body": "<!-- ai-pr-reviewer-specialist:old -->", "state": "COMMENTED"}]
    _publish(tmp_path, client, allowed_github_repositories=("vendor/library",), run_url=RUN, cleanup_previous_reviews=False)
    assert ("minimize", "access-node", "RESOLVED") in client.calls
    assert ("minimize", "old", "OUTDATED") not in client.calls


def test_access_minimization_retry_does_not_repeat_annotation(tmp_path):
    client = AccessClient()
    first, _ = _publish(tmp_path, client, allowed_github_repositories=("vendor/library",), run_url=RUN)
    submitted = next(call for call in client.calls if call[0] == "submit")
    client.managed_state["reviews"] = [{"id": "review-id", "url": first["review"]["url"], "state": "COMMENTED", "body": submitted[3]}]
    client.calls.clear()
    second, _ = _publish(tmp_path, client, allowed_github_repositories=("vendor/library",), run_url=RUN)
    assert second["review_completed"]
    assert not any(call[0] in {"submit", "annotate"} for call in client.calls)
    assert ("minimize", "access-node", "RESOLVED") in client.calls


def test_separate_runs_get_distinct_review_identity(tmp_path):
    client = _FakeClient()
    first, _ = _publish(tmp_path, client, run_url=RUN)
    submitted = next(call for call in client.calls if call[0] == "submit")
    client.managed_state["reviews"] = [{"id": "old-review-id", "url": first["review"]["url"], "state": "COMMENTED", "body": submitted[3]}]
    client.calls.clear()
    second, _ = _publish(tmp_path, client, run_url=RUN + "4")
    assert first["publication_id"] != second["publication_id"]
    assert any(call[0] == "submit" for call in client.calls)
    assert ("minimize", "old-review-id", "OUTDATED") in client.calls


def test_switching_from_single_review_preserves_unassessed_blocking_verdict(tmp_path):
    client = _FakeClient({"reviews": [
        {"id": "old-single", "state": "CHANGES_REQUESTED", "body": "<!-- ai-pr-reviewer -->\nOld review"},
        {"id": "unmanaged", "state": "CHANGES_REQUESTED", "body": "My own unrelated review"},
    ]})
    result, _ = _publish(tmp_path, client)
    assert result["review_completed"]
    assert ("dismiss", "old-single") not in client.calls
    assert ("minimize", "old-single", "OUTDATED") in client.calls
    assert not any(call[0] in {"dismiss", "minimize"} and call[1] == "unmanaged" for call in client.calls)


def test_new_approval_cannot_supersede_uncleared_owned_request_changes(tmp_path):
    client = _FakeClient({"reviews": [{"id": "old", "state": "CHANGES_REQUESTED",
        "body": "<!-- ai-pr-reviewer-specialist:old -->"}]})
    result, _ = _publish(tmp_path, client, mode="review_verdict", verdict="approve",
        approval=PublisherApprovalPolicy(allow_approve=True, is_fork=False,
            effective_scope="full", baseline_clean=True))
    assert result["review_completed"]
    assert next(call[2] for call in client.calls if call[0] == "submit") == "COMMENT"
    assert not any(call[0] == "dismiss" for call in client.calls)


def test_dismissal_failure_does_not_prevent_visual_retirement(tmp_path):
    class DismissalDenied(_FakeClient):
        def dismiss_review(self, review_id):
            raise RuntimeError("dismissal is restricted")
    client = DismissalDenied({"reviews": [{"id": "old-review", "state": "APPROVED", "body": "<!-- ai-pr-reviewer-specialist:old -->"}]})
    result, _ = _publish(tmp_path, client)
    assert result["review_completed"]
    assert ("minimize", "old-review", "OUTDATED") in client.calls
    assert not result["publication_errors"]
    assert any(error["operation"] == "dismiss_previous_review" for error in result["publication_warnings"])


def test_revoked_permission_creates_visible_request_without_overwriting_archive(tmp_path):
    client = AccessClient()
    _publish(tmp_path, client, allowed_github_repositories=("vendor/library",), run_url=RUN)
    archived = client.managed_state["general_comments"][0]
    archived["is_minimized"] = True
    original_body = archived["body"]
    client.calls.clear()
    note = _note("source_access_request:abc", kind=ReviewNoteKind.SOURCE_ACCESS_REQUEST, file=None, line=None, markdown=REPO_BODY)
    result, _ = _publish(tmp_path, client, notes=(note,), run_url=RUN + "4")
    assert result["review_completed"]
    general = next(call for call in client.calls if call[0] == "general")
    assert general[3] is None  # New visible comment, not a PATCH of the archive.
    assert archived["body"] == original_body


@pytest.mark.parametrize("classifier", ["OUTDATED", "RESOLVED"])
def test_minimize_client_confirms_reason(tmp_path, classifier):
    class Client(GhReviewClient):
        def _graphql(self, query, variables):
            assert "minimizeComment" in query
            assert variables == {"id": "owned-node", "reason": classifier}
            return {"data": {"minimizeComment": {"minimizedComment": {"isMinimized": True, "minimizedReason": classifier.lower()}}}}
    assert Client(action_root=tmp_path).minimize_comment("owned-node", classifier)["isMinimized"]


def test_annotation_client_requires_exact_body_and_comment_id(tmp_path):
    class Client(GhReviewClient):
        def _input_call(self, argv, payload):
            assert argv == ["api", "repos/owner/repo/issues/comments/123", "--method", "PATCH"]
            return {"id": 123, "html_url": "https://github.com/owner/repo/issues/17#issuecomment-123", "body": payload["body"]}
    assert Client(action_root=tmp_path).annotate_access_request("owner/repo", 17, {"id": 123}, "updated")["id"] == 123
