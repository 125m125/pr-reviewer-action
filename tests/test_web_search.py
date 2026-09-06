"""Integration tests for policy-filtered web discovery tools."""

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import run_tool_harness as rth  # noqa: E402
from pr_reviewer.conversation import web_tool_schemas  # noqa: E402
from pr_reviewer.specialist_runtime.web_evidence import (  # noqa: E402
    HttpResponse,
    SearchResultRegistry,
    SearxngSearchProvider,
    SecureFetcher,
    SourcePolicy,
)
from pr_reviewer.specialist_runtime.evidence import EvidenceStore  # noqa: E402


SEARCH = "https://search.example.com/search"
POLICY = SourcePolicy.from_hosts(["a.example", "b.example", "u.example"])


class _SearchTransport:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {"results": []}
        self.error = error
        self.urls = []

    def request(self, request):
        self.urls.append(request.url)
        if self.error:
            raise self.error
        return HttpResponse(
            200, {"content-type": "application/json"},
            json.dumps(self.payload).encode("utf-8"),
        )


def _provider(endpoint, payload=None, error=None):
    transport = _SearchTransport(payload, error)
    provider = SearxngSearchProvider(
        endpoint, transport=transport,
        resolver=lambda host, port: ["93.184.216.34"],
    )
    return provider, transport


def test_returns_capped_policy_filtered_discovery():
    payload = {"results": [
        {"title": "A", "url": "https://a.example/x", "content": "snip a"},
        {"title": "B", "url": "https://b.example/y", "content": "snip b"},
        {"title": "C", "url": "https://c.example/z", "content": "snip c"},
    ]}
    provider, transport = _provider(SEARCH, payload)
    res = rth.web_search(
        "talos support matrix", SEARCH, max_results=2, source_policy=POLICY,
        provider=provider,
    )
    assert "error" not in res
    assert res["kind"] == "search_discovery"
    assert len(res["approved"]) == 2
    assert res["approved"][0]["snippet"] == "snip a"
    assert res["unapproved"] == []
    assert res["suppressed_result_count"] == 1
    assert "snip c" not in json.dumps(res)
    # query + json format are sent to the configured endpoint
    assert "format=json" in transport.urls[0] and "q=talos" in transport.urls[0]


def test_appends_query_when_endpoint_already_has_one():
    provider, transport = _provider("https://s.example/search?foo=1")
    rth.web_search(
        "q", "https://s.example/search?foo=1", source_policy=POLICY,
        provider=provider,
    )
    assert "foo=1" in transport.urls[0] and "&q=q" in transport.urls[0]


def test_empty_search_url_is_error():
    res = rth.web_search("anything", "", source_policy=POLICY)
    assert "error" in res and "not configured" in res["error"].lower()


def test_transport_failure_is_error():
    provider, _ = _provider(SEARCH, error=OSError("connection refused"))
    res = rth.web_search("q", SEARCH, source_policy=POLICY, provider=provider)
    assert "error" in res


def test_execute_tool_request_dispatches_web_search():
    payload = {"results": [{"title": "T", "url": "https://u.example", "content": "c"}]}
    provider, _ = _provider(SEARCH, payload)
    tr = rth.execute_tool_request(
        "web_search", {"query": "k8s support matrix"},
        ".", set(), "o/r", ["u.example"], 12000, 20, SEARCH, 5,
        source_policy=SourcePolicy.from_hosts(["u.example"]),
        search_provider=provider,
    )
    assert tr["status"] == "ok"
    assert tr["result"]["kind"] == "search_discovery"
    assert tr["result"]["approved"][0]["host"] == "u.example"
    assert tr["result"]["evidentiary"] is False


def test_opaque_allowlisted_result_can_be_fetched_by_handle_without_url_disclosure():
    token = "0123456789abcdef" * 4
    url = f"https://u.example/docs/{token}?page=2&cursor={token}"
    provider, _ = _provider(SEARCH, {
        "results": [{"title": "T", "url": url, "content": "discovered"}],
    })
    registry = SearchResultRegistry()
    policy = SourcePolicy.from_hosts(["u.example"])
    search = rth.execute_tool_request(
        "web_search", {"query": "opaque docs"},
        ".", set(), "o/r", ["u.example"], 12000, 20, SEARCH, 5,
        source_policy=policy,
        search_provider=provider,
        search_result_registry=registry,
    )
    result_id = search["result"]["approved"][0]["result_id"]
    fetch_transport = _SearchTransport()
    fetch_transport.payload = None
    fetch_transport.request = lambda request: (
        fetch_transport.urls.append(request.url)
        or HttpResponse(
            200,
            {"content-type": "text/plain"},
            f"fetched {token} {url}".encode(),
        )
    )
    evidence_store = EvidenceStore()
    fetcher = SecureFetcher(
        policy,
        transport=fetch_transport,
        resolver=lambda host, port: ["93.184.216.34"],
        evidence_store=evidence_store,
    )

    fetched = rth.execute_tool_request(
        "web_fetch_search_result", {"result_id": result_id},
        ".", set(), "o/r", ["u.example"], 12000, 20, SEARCH, 5,
        source_policy=policy,
        secure_fetcher=fetcher,
        search_result_registry=registry,
    )

    assert fetched["status"] == "ok"
    assert fetched["result"]["content"].startswith("fetched")
    assert fetch_transport.urls == [url]
    assert token not in json.dumps(fetched)
    assert fetched["result"]["provenance"]["original_url"].endswith(
        "/[opaque-search-result]"
    )
    retained = evidence_store.snapshot().records[0]
    assert token not in retained.arguments
    assert token not in retained.source_identity
    assert retained.tool == "web_fetch_search_result"


def test_fetch_search_result_rejects_unknown_session_handle():
    tr = rth.execute_tool_request(
        "web_fetch_search_result", {"result_id": "search-result-99"},
        ".", set(), "o/r", ["u.example"], 12000, 20, SEARCH, 5,
        source_policy=SourcePolicy.from_hosts(["u.example"]),
        search_result_registry=SearchResultRegistry(),
    )

    assert tr["status"] == "error"
    assert "unknown search result" in tr["result"]["error"]


def test_execute_tool_request_web_search_missing_query():
    tr = rth.execute_tool_request(
        "web_search", {}, ".", set(), "o/r", [], 12000, 20, SEARCH, 5,
    )
    assert tr["status"] == "error"


def test_execute_tool_request_web_search_unconfigured_errors():
    # No search_url passed → executor surfaces the not-configured error.
    tr = rth.execute_tool_request(
        "web_search", {"query": "q"}, ".", set(), "o/r", [], 12000, 20,
    )
    assert tr["status"] == "error"


def test_search_schema_requires_endpoint_and_approved_source_policy():
    assert "web_search" not in {
        schema["name"] for schema in web_tool_schemas("", POLICY)
    }
    assert "web_search" not in {
        schema["name"] for schema in web_tool_schemas(SEARCH, SourcePolicy(()))
    }
    assert "web_search" in {
        schema["name"] for schema in web_tool_schemas(SEARCH, POLICY)
    }
    assert "web_fetch_search_result" in {
        schema["name"] for schema in web_tool_schemas(SEARCH, POLICY)
    }


def test_private_http_search_endpoint_requires_explicit_opt_in():
    endpoint = "http://10.0.0.4:8888/search"
    assert not SearxngSearchProvider.is_valid_endpoint(endpoint)
    assert SearxngSearchProvider.is_valid_endpoint(
        endpoint, allow_private_search_url=True,
    )

    transport = _SearchTransport({"results": []})
    provider = SearxngSearchProvider(
        endpoint,
        allow_private_search_url=True,
        transport=transport,
        resolver=lambda host, port: ["10.0.0.4"],
    )
    provider.search("q", limit=1)
    assert transport.urls[0].startswith("http://10.0.0.4:8888/search?")


def test_private_http_search_schema_requires_opt_in():
    endpoint = "http://10.0.0.4:8888/search"
    assert "web_search" not in {
        schema["name"] for schema in web_tool_schemas(endpoint, POLICY)
    }
    assert "web_search" in {
        schema["name"] for schema in web_tool_schemas(
            endpoint, POLICY, allow_private_search_url=True,
        )
    }


@pytest.mark.parametrize("endpoint", [
    "http://search.example.com/search",
    "https://user@search.example.com/search",
    "https://search.example.com:444/search",
    "https://search.example.com/search#fragment",
])
def test_search_schema_rejects_same_malformed_endpoints_as_executor(endpoint):
    names = {schema["name"] for schema in web_tool_schemas(endpoint, POLICY)}
    assert "web_search" not in names


def test_web_tools_fail_closed_without_source_policy():
    names = {schema["name"] for schema in web_tool_schemas(SEARCH, SourcePolicy(()))}
    assert "web_search" not in names
    assert "web_fetch" not in names


def test_current_head_policy_wiring_is_empty_when_file_missing(tmp_path):
    policy = rth.load_current_source_policy(tmp_path, "missing-policy.json")
    assert policy.has_approved_sources is False


def test_current_head_policy_wiring_preserves_path_restrictions(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({
        "version": 2,
        "sources": [{
            "host": "docs.example.com",
            "path_prefixes": ["/api"],
            "classification": "official",
        }],
    }), encoding="utf-8")

    policy = rth.load_current_source_policy(tmp_path, "policy.json")

    assert policy.classify("https://docs.example.com/api/v1").approved is True
    assert policy.classify("https://docs.example.com/blog").approved is False


def test_current_head_policy_wiring_fails_closed_when_invalid(tmp_path):
    (tmp_path / "policy.json").write_text('{"version": 2, "sources": [{"host": "*"}]}')

    policy = rth.load_current_source_policy(tmp_path, "policy.json")

    assert policy.has_approved_sources is False
