"""Search discovery must advertise a usable, independently authorized route."""
import json

import pytest

from pr_reviewer.conversation import web_tool_schemas
from pr_reviewer.specialist_runtime.web_evidence import (
    SearchCandidate, SearchResultRegistry, SourcePolicy, discover,
)
from pr_reviewer.tool_executors import execute_tool_request


class Provider:
    def __init__(self, url):
        self.url = url

    def search(self, query, *, limit):
        return [SearchCandidate("Official", self.url, "Details", result_id="forged")]


@pytest.mark.parametrize("url,route", [
    ("https://docs.example.com/guide", "web"),
    ("https://github.com/other/repo/issues/12", "github_metadata"),
    ("https://github.com/other/repo/pull/12#issuecomment-234", "github_metadata"),
    ("https://api.github.com/repos/other/repo/contents/README.md?ref=main", "github_file"),
    ("https://raw.githubusercontent.com/other/repo/" + "a" * 40 + "/README.md", "github_file"),
])
def test_approved_results_have_trusted_retrieval_ids(url, route):
    registry = SearchResultRegistry()
    result = discover(
        "contract", Provider(url), SourcePolicy.from_hosts(["docs.example.com"]),
        result_registry=registry, allowed_repos=("other/repo",), current_repo="own/repo",
    ).as_dict()["approved"][0]
    assert result["fetch_method"] == "result_id"
    assert result["result_id"] != "forged"
    assert result["url"] == url
    assert registry.resolve_target(result["result_id"]).route == route
    assert "endpoint" not in result
    assert "resolved_sha" not in result


def test_repository_only_search_does_not_enable_generic_web_fetch():
    names = {s["name"] for s in web_tool_schemas(
        "https://search.example.com/search", SourcePolicy(()), allowed_repos=("other/repo",),
    )}
    assert {"web_search", "web_fetch_search_result"} <= names
    assert "web_fetch" not in names


@pytest.mark.parametrize("url,repos", [
    ("https://github.com/other/repo/blob/" + "a" * 40 + "/file.py", ("*",)),
    ("https://github.com/own/repo/blob/" + "a" * 40 + "/file.py", ("own/repo",)),
    ("https://github.com/other/repo/issues/1?unexpected=1", ("other/repo",)),
    ("https://github.com/other/repo/issues/1#unknown", ("other/repo",)),
])
def test_unsupported_or_unauthorized_routes_do_not_advertise_retrieval(url, repos):
    value = discover("contract", Provider(url), SourcePolicy(()),
        result_registry=SearchResultRegistry(), allowed_repos=repos, current_repo="own/repo").as_dict()
    assert value["approved"] == []
    assert value["unapproved"][0]["fetch_allowed"] is False
    assert "snippet" not in value["unapproved"][0]


def test_search_issue_roundtrip_uses_repository_api(monkeypatch, tmp_path):
    registry = SearchResultRegistry()
    result = discover("contract", Provider("https://github.com/other/repo/issues/12"),
        SourcePolicy(()), result_registry=registry, allowed_repos=("other/repo",),
        current_repo="own/repo").as_dict()["approved"][0]
    calls = []
    def api(endpoint, *args):
        calls.append(endpoint)
        return {"data": {"number": 12, "body": "Verified contract"}}
    monkeypatch.setattr("pr_reviewer.platform.gh_api", api)
    value = execute_tool_request("web_fetch_search_result", {"result_id": result["result_id"]},
        str(tmp_path), ("other/repo",), "own/repo", (), 12000, 10,
        source_policy=SourcePolicy(()), search_result_registry=registry)
    assert value["status"] == "ok"
    assert "Verified contract" in json.dumps(value)
    assert calls == ["repos/other/repo/issues/12"]


def test_symbolic_ref_is_resolved_once_and_rechecked_for_permission(monkeypatch):
    from pr_reviewer.tool_executors import read_remote_file
    from pr_reviewer.specialist_runtime.evidence import EvidenceStore
    calls = []
    def api(endpoint, *args):
        calls.append(endpoint)
        if "/commits/" in endpoint:
            return {"data": {"sha": "a" * 40}}
        return {"data": {"type": "file", "size": 4}}
    monkeypatch.setattr("pr_reviewer.platform.gh_api", api)
    monkeypatch.setattr("pr_reviewer.platform.gh_raw_file", lambda *args: {"content": b"text"})
    registry = SearchResultRegistry()
    for path in ("a.py", "b.py"):
        result = read_remote_file("other/repo", path, "feature/topic", ("other/repo",),
            "own/repo", ref_cache=registry)
        assert result["requested_ref"] == "feature/topic"
        assert result["resolved_sha"] == "a" * 40
        assert result["ref"] == "a" * 40
        record = EvidenceStore().add_tool_result(session_id="s", tool="read_remote_file",
            arguments={"repository": "other/repo", "path": path, "ref": "feature/topic"},
            result={"status": "ok", "result": result})
        assert record.source_path == "@remote/other/repo@" + "a" * 40 + "/" + path
    assert sum("/commits/" in p for p in calls) == 1
    assert all("?ref=" + "a" * 40 in p for p in calls if "/contents/" in p)
    before = len(calls)
    denied = read_remote_file("other/repo", "a.py", "feature/topic", (), "own/repo", ref_cache=registry)
    assert "Repo not allowed" in denied["error"]
    assert len(calls) == before


def test_result_ids_are_not_reusable_across_sessions():
    first, second = SearchResultRegistry(), SearchResultRegistry()
    foreign = first.register("https://docs.example.com/one")
    second.register("https://docs.example.com/two")
    with pytest.raises(ValueError, match="unknown search result"):
        second.resolve_target(foreign)


@pytest.mark.parametrize("suffix", [
    "releases/tags/%252e%252e", "releases/tags/a%2fb", "issues/1?token=secret",
    "contents/README.md?ref=../main", "contents/README.md?ref=token%3Dsecret",
    "contents/README.md?ref=ghp_abcdefghijklmnopqrstuvwxyz1234567890",
    "releases/tags/ghp_abcdefghijklmnopqrstuvwxyz1234567890",
])
def test_unsafe_repository_search_targets_are_denied(suffix):
    result = discover("contract", Provider("https://api.github.com/repos/other/repo/" + suffix),
        SourcePolicy(()), result_registry=SearchResultRegistry(), allowed_repos=("other/repo",),
        current_repo="own/repo").as_dict()
    assert not result["approved"]


def test_search_metadata_keeps_resource_identity():
    result = discover("contract", Provider("https://github.com/other/repo/pull/12"),
        SourcePolicy(()), result_registry=SearchResultRegistry(), allowed_repos=("other/repo",),
        current_repo="own/repo").as_dict()["approved"][0]
    assert result["repository"] == "other/repo"
    assert result["resource_type"] == "pulls"
    assert result["resource_id"] == "12"


@pytest.mark.parametrize("ref", ["a" * 40, "v2.4.1", "feature/topic"])
def test_file_search_roundtrip_retains_immutable_identity(monkeypatch, tmp_path, ref):
    from urllib.parse import quote
    from pr_reviewer.specialist_runtime.evidence import EvidenceStore
    calls = []
    def api(endpoint, *args):
        calls.append(endpoint)
        return {"data": {"sha": "a" * 40} if "/commits/" in endpoint else {"type": "file", "size": 10}}
    monkeypatch.setattr("pr_reviewer.platform.gh_api", api)
    monkeypatch.setattr("pr_reviewer.platform.gh_raw_file", lambda *args: {"content": b"one\ntwo\n"})
    registry = SearchResultRegistry()
    url = "https://api.github.com/repos/other/repo/contents/README.md?ref=" + quote(ref, safe="") + "#L2"
    hit = discover("contract", Provider(url), SourcePolicy(()), result_registry=registry,
        allowed_repos=("other/repo",), current_repo="own/repo").as_dict()["approved"][0]
    assert calls == []
    result = execute_tool_request("web_fetch_search_result", {"result_id": hit["result_id"]},
        str(tmp_path), ("other/repo",), "own/repo", (), 12000, 10, search_result_registry=registry)
    assert result["status"] == "ok", result
    assert result["result"]["content"] == "LINE 2 | two\n"
    assert result["result"]["resolved_sha"] == "a" * 40
    assert result["result"]["requested_ref"] == ref
    assert len(calls) == (1 if ref == "a" * 40 else 2)
    record = EvidenceStore().add_tool_result(session_id="s", tool="web_fetch_search_result",
        arguments={"result_id": hit["result_id"]}, result=result)
    assert record.tool == "read_remote_file"
    assert record.source_path == "@remote/other/repo@" + "a" * 40 + "/README.md"


def test_visible_website_id_preserves_provenance_without_opaque_exception(tmp_path):
    from pr_reviewer.specialist_runtime.evidence import EvidenceStore
    from pr_reviewer.specialist_runtime.web_evidence import SecureFetcher, HttpResponse
    policy = SourcePolicy.from_hosts(["docs.example.com"])
    registry = SearchResultRegistry()
    url = "https://docs.example.com/guide?page=2"
    hit = discover("contract", Provider(url), policy, result_registry=registry).as_dict()["approved"][0]
    class Transport:
        def request(self, request):
            assert request.url == url
            return HttpResponse(200, {"content-type": "text/plain"}, b"source text")
    fetcher = SecureFetcher(policy, transport=Transport(), resolver=lambda *args: ["93.184.216.34"])
    result = execute_tool_request("web_fetch_search_result", {"result_id": hit["result_id"]},
        str(tmp_path), (), "own/repo", (), 12000, 10, source_policy=policy,
        search_result_registry=registry, secure_fetcher=fetcher)
    assert result["status"] == "ok", result
    assert not registry.resolve_target(hit["result_id"]).opaque
    record = EvidenceStore().add_tool_result(session_id="s", tool="web_fetch_search_result",
        arguments={"result_id": hit["result_id"]}, result=result)
    assert record.provenance.original_url == url
    assert record.provenance.policy_hash == policy.policy_hash
    assert record.category == "external-source"


@pytest.mark.parametrize("override", ["url", "endpoint", "path", "ref"])
def test_result_id_rejects_target_overrides(tmp_path, override):
    registry = SearchResultRegistry()
    result_id = registry.register("https://docs.example.com/guide")
    result = execute_tool_request("web_fetch_search_result", {"result_id": result_id, override: "changed"},
        str(tmp_path), (), "own/repo", (), 12000, 10, search_result_registry=registry)
    assert result["status"] == "error"
    assert "no target overrides" in result["result"]["error"]


def test_platform_and_permission_rechecked_and_wrong_tool_redirected(monkeypatch, tmp_path):
    registry = SearchResultRegistry()
    url = "https://github.com/other/repo/issues/12"
    hit = discover("contract", Provider(url), SourcePolicy(()), result_registry=registry,
        allowed_repos=("other/repo",), current_repo="own/repo").as_dict()["approved"][0]
    def forbidden(*args, **kwargs):
        raise AssertionError("No network expected")
    monkeypatch.setattr("pr_reviewer.platform.gh_api", forbidden)
    monkeypatch.setattr("pr_reviewer.tool_executors.web_fetch", forbidden)
    def execute(name, args):
        return execute_tool_request(name, args, str(tmp_path), (), "own/repo", (),
            12000, 10, search_result_registry=registry)
    hint = execute("web_fetch", {"url": url})
    assert hint["result"]["result_id"] == hit["result_id"]
    denied = execute("web_fetch_search_result", {"result_id": hit["result_id"]})
    assert denied["effective_tool"] == "gh_api"
    assert denied["result"]["error"] == "Repo not allowed: other/repo"
    monkeypatch.setenv("PLATFORM", "forgejo")
    denied = execute("web_fetch_search_result", {"result_id": hit["result_id"]})
    assert "platform" in denied["result"]["error"]
    discovery = discover("contract", Provider(url), SourcePolicy(()), result_registry=registry,
        allowed_repos=("other/repo",), current_repo="own/repo").as_dict()
    assert discovery["approved"] == []


def test_ref_resolution_failure_not_cached_and_deadline_is_shared(monkeypatch):
    from pr_reviewer.tool_executors import read_remote_file
    now = [100.0]
    calls = []
    resolutions = [None, "a" * 40, "b" * 40]
    def api(endpoint, *args):
        calls.append((endpoint, args[-1]))
        now[0] += 2
        if "/commits/" in endpoint:
            return {"data": {"sha": resolutions.pop(0)}}
        return {"data": {"type": "file", "size": 4}}
    def raw(endpoint, allowed, timeout, size):
        calls.append(("raw", timeout))
        return {"content": b"text"}
    monkeypatch.setattr("pr_reviewer.tool_executors.time.monotonic", lambda: now[0])
    monkeypatch.setattr("pr_reviewer.platform.gh_api", api)
    monkeypatch.setattr("pr_reviewer.platform.gh_raw_file", raw)
    registry = SearchResultRegistry()
    def read(cache):
        return read_remote_file("other/repo", "a.py", "main", ("other/repo",), "own/repo",
            ref_cache=cache, request_timeout=10)
    assert "error" in read(registry)
    assert read(registry)["resolved_sha"] == "a" * 40
    assert [timeout for _, timeout in calls[-3:]] == [10, 8, 6]
    assert read(SearchResultRegistry())["resolved_sha"] == "b" * 40


def test_expired_remote_deadline_never_starts_network(monkeypatch):
    from pr_reviewer.tool_executors import read_remote_file
    def forbidden(*args):
        pytest.fail("Expired request must not contact the API")
    monkeypatch.setattr("pr_reviewer.platform.gh_api", forbidden)
    result = read_remote_file("other/repo", "README.md", "main", ("other/repo",),
        "own/repo", deadline_at=0)
    assert "deadline" in result["error"]


@pytest.mark.parametrize("url", [
    "https://github.com/other/repo/commits/" + "a" * 40,
    "https://api.github.com/repos/other/repo/commit/" + "a" * 40,
    "https://github.com/other/repo/pulls/12",
    "https://api.github.com/repos/other/repo/pull/12",
    "https://github.com/other/repo/releases/tags/v1",
    "https://api.github.com/repos/other/repo/releases/tag/v1",
    "https://github.com/other/repo/blob/" + "a" * 40 + "/literal%2541.txt",
    "https://api.github.com/repos/other/repo/contents/literal%2541.txt?ref=main",
])
def test_search_routes_never_reinterpret_host_spellings_or_nested_escapes(url):
    result = discover("contract", Provider(url), SourcePolicy(()),
        result_registry=SearchResultRegistry(), allowed_repos=("other/repo",),
        current_repo="own/repo").as_dict()
    assert result["approved"] == []
    assert result["unapproved"][0]["denial_reason"]


@pytest.mark.parametrize("url,endpoint", [
    ("https://github.com/other/repo/commit/" + "a" * 40, "commits/" + "a" * 40),
    ("https://api.github.com/repos/other/repo/commits/" + "a" * 40, "commits/" + "a" * 40),
    ("https://github.com/other/repo/pull/12", "pulls/12"),
    ("https://api.github.com/repos/other/repo/pulls/12", "pulls/12"),
    ("https://github.com/other/repo/releases/tag/v1", "releases/tags/v1"),
    ("https://api.github.com/repos/other/repo/releases/tags/v1", "releases/tags/v1"),
])
def test_supported_web_and_api_routes_keep_exact_resource(url, endpoint):
    registry = SearchResultRegistry()
    hit = discover("contract", Provider(url), SourcePolicy(()),
        result_registry=registry, allowed_repos=("other/repo",),
        current_repo="own/repo").as_dict()["approved"][0]
    assert dict(registry.resolve_target(hit["result_id"]).arguments) == {
        "endpoint": "repos/other/repo/" + endpoint,
    }
