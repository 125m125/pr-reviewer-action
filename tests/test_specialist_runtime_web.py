"""Security boundary tests for allowlisted web discovery and evidence."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pr_reviewer.specialist_runtime.policy import SourceRule
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.web_evidence import (
    HttpRequest,
    HttpResponse,
    SearchCandidate,
    SecureFetcher,
    SourceDenied,
    SourcePolicy,
    discover,
    source_access_request,
)


@dataclass
class FakeSearchProvider:
    candidates: list[SearchCandidate]

    def __post_init__(self) -> None:
        self.limits: list[int] = []

    def search(self, query: str, *, limit: int) -> list[SearchCandidate]:
        self.limits.append(limit)
        return self.candidates


def source_policy(*rules: SourceRule) -> SourcePolicy:
    if not rules:
        rules = (SourceRule(host="docs.example.com", classification="official"),)
    return SourcePolicy(rules)


def test_search_returns_snippets_only_for_approved_sources():
    provider = FakeSearchProvider([
        SearchCandidate("Official", "https://docs.example.com/api", "trusted snippet"),
        SearchCandidate("Blog", "https://blog.invalid/post", "unapproved content"),
    ])

    result = discover("api behavior", provider, source_policy())

    assert result.approved[0].snippet == "trusted snippet"
    assert result.approved[0].classification == "official"
    assert result.unapproved[0].host == "blog.invalid"
    assert result.unapproved[0].snippet is None
    assert "unapproved content" not in result.to_tool_result()


def test_unapproved_candidate_creates_request_without_fetching():
    provider = FakeSearchProvider([
        SearchCandidate("Blog", "https://blog.invalid/post", "unapproved content"),
    ])
    result = discover("api behavior", provider, source_policy())

    request = source_access_request(
        result.unapproved[0], "OB-model-api", "verify support"
    )

    assert request.host == "blog.invalid"
    assert request.obligation_id == "OB-model-api"
    assert request.purpose == "verify support"
    assert provider.limits == [25]


def test_discovery_scans_bounded_results_and_caps_approved_output():
    provider = FakeSearchProvider([
        SearchCandidate(str(index), f"https://docs.example.com/{index}", "snippet")
        for index in range(10)
    ])

    result = discover(
        "release support", provider, source_policy(),
        search_scan_limit=4, tool_max_search_results=2,
    )

    assert provider.limits == [4]
    assert len(result.approved) == 2
    assert result.suppressed_result_count == 2


def test_source_policy_requires_explicit_subdomain_and_path_match():
    exact = source_policy(SourceRule(
        host="example.com", path_prefixes=("/docs",), classification="documentation"
    ))
    subdomains = source_policy(SourceRule(
        host="example.com", include_subdomains=True,
        path_prefixes=("/docs",), classification="documentation",
    ))

    assert exact.classify("https://example.com/docs/api").approved is True
    assert exact.classify("https://api.example.com/docs/api").approved is False
    assert exact.classify("https://example.com/docs-evil").approved is False
    assert subdomains.classify("https://api.example.com/docs/api").approved is True


@pytest.mark.parametrize("encoded_parent", ["%2e%2e", "%252e%252e"])
def test_source_policy_rejects_encoded_path_traversal(encoded_parent):
    policy = source_policy(SourceRule(
        host="docs.example.com", path_prefixes=("/api",), classification="official",
    ))

    decision = policy.classify(
        f"https://docs.example.com/api/{encoded_parent}/admin"
    )

    assert decision.approved is False
    assert "traversal" in (decision.reason or "").lower()


@pytest.mark.parametrize("target", [
    "https://docs.example.com/api?token=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "https://docs.example.com/api?value=%67hp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "https://docs.example.com/api/ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "https://docs.example.com/api%0d%0aX-Injected%3Ayes",
])
def test_source_policy_rejects_url_borne_secrets_and_controls(target):
    decision = source_policy().classify(target)

    assert decision.approved is False
    assert "unsafe" in (decision.reason or "").lower()


@pytest.mark.parametrize("query", [
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
    "token=ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "upload QWxhZGRpbjpvcGVuIHNlc2FtZQ0123456789abcdef",
])
def test_discovery_rejects_credential_or_high_entropy_queries(query):
    provider = FakeSearchProvider([])

    with pytest.raises(SourceDenied, match="query"):
        discover(query, provider, source_policy())

    assert provider.limits == []


def test_discovery_rejects_overlong_query():
    with pytest.raises(SourceDenied, match="length"):
        discover("x" * 501, FakeSearchProvider([]), source_policy())


PUBLIC_IP = "93.184.216.34"


def public_resolver(host: str, port: int) -> list[str]:
    return [PUBLIC_IP]


class FakeHttpTransport:
    def __init__(self, responses: dict[str, HttpResponse]) -> None:
        self.responses = responses
        self.requests: list[HttpRequest] = []

    @classmethod
    def redirecting(cls, source: str, target: str) -> "FakeHttpTransport":
        return cls({source: HttpResponse(302, {"location": target}, b"")})

    def request(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        return self.responses[request.url]


@pytest.mark.parametrize("target", [
    "https://127.0.0.1/x",
    "https://169.254.169.254/latest/meta-data",
    "https://[::1]/x",
    "http://docs.example.com/x",
    "https://user:pass@docs.example.com/x",
    "https://docs.example.com:444/x",
])
def test_secure_fetch_rejects_private_non_https_or_credentialed_target(target):
    fetcher = SecureFetcher(
        source_policy(), transport=FakeHttpTransport({}), resolver=public_resolver,
    )

    with pytest.raises(SourceDenied):
        fetcher.fetch(target)


def test_redirect_must_remain_allowlisted():
    transport = FakeHttpTransport.redirecting(
        "https://docs.example.com/a", "https://evil.example/b"
    )

    with pytest.raises(SourceDenied, match="redirect"):
        SecureFetcher(
            source_policy(), transport=transport, resolver=public_resolver,
        ).fetch("https://docs.example.com/a")


def test_every_redirect_is_resolved_and_connected_to_checked_ip():
    transport = FakeHttpTransport({
        "https://docs.example.com/a": HttpResponse(
            301, {"Location": "/final"}, b"",
        ),
        "https://docs.example.com/final": HttpResponse(
            200, {"Content-Type": "text/plain; charset=utf-8"}, b"supported",
        ),
    })
    resolutions: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> list[str]:
        resolutions.append((host, port))
        return [PUBLIC_IP]

    result = SecureFetcher(
        source_policy(), transport=transport, resolver=resolver,
    ).fetch("https://docs.example.com/a")

    assert resolutions == [("docs.example.com", 443), ("docs.example.com", 443)]
    assert [request.resolved_ip for request in transport.requests] == [PUBLIC_IP, PUBLIC_IP]
    assert result.content == "supported"
    assert result.provenance.final_url == "https://docs.example.com/final"


@pytest.mark.parametrize("resolved_ip", [
    "127.0.0.1", "10.1.2.3", "169.254.1.1", "224.0.0.1",
    "192.0.2.1", "0.0.0.0", "100.100.100.200", "::1",
])
def test_secure_fetch_rejects_unsafe_resolved_address(resolved_ip):
    fetcher = SecureFetcher(
        source_policy(),
        transport=FakeHttpTransport({}),
        resolver=lambda host, port: [resolved_ip],
    )

    with pytest.raises(SourceDenied, match="address"):
        fetcher.fetch("https://docs.example.com/api")


def test_secure_fetch_rejects_redirect_outside_approved_path():
    policy = source_policy(SourceRule(
        host="docs.example.com", path_prefixes=("/api",), classification="official",
    ))
    transport = FakeHttpTransport.redirecting(
        "https://docs.example.com/api/start", "https://docs.example.com/admin"
    )

    with pytest.raises(SourceDenied, match="redirect"):
        SecureFetcher(policy, transport=transport, resolver=public_resolver).fetch(
            "https://docs.example.com/api/start"
        )


def test_secure_fetch_rejects_disallowed_mime_type():
    url = "https://docs.example.com/archive"
    transport = FakeHttpTransport({
        url: HttpResponse(200, {"content-type": "application/zip"}, b"PK"),
    })

    with pytest.raises(SourceDenied, match="MIME"):
        SecureFetcher(
            source_policy(), transport=transport, resolver=public_resolver,
        ).fetch(url)


def test_secure_fetch_normalizes_masks_truncates_and_records_evidence():
    url = "https://docs.example.com/api"
    token = "ghp_" + "A" * 36
    body = (
        f"<html><body><h1>API support</h1><script>steal()</script>"
        f"<p>token {token}</p><p>{'x' * 100}</p></body></html>"
    ).encode()
    transport = FakeHttpTransport({
        url: HttpResponse(200, {"Content-Type": "text/html; charset=utf-8"}, body),
    })
    store = EvidenceStore(max_content_bytes=4096)
    policy = source_policy()

    result = SecureFetcher(
        policy,
        transport=transport,
        resolver=public_resolver,
        evidence_store=store,
        max_bytes=80,
        clock=lambda: 1234.5,
    ).fetch(url, session_id="specialist-1", model_identity="model-a")

    assert "API support" in result.content
    assert "steal()" not in result.content
    assert token not in result.content
    assert result.truncated is True
    assert result.mime_type == "text/html"
    assert result.provenance.original_url == url
    assert result.provenance.retrieved_at == 1234.5
    assert result.provenance.policy_hash == policy.policy_hash
    assert result.evidence_id
    record = store.snapshot().get(result.evidence_id)
    assert record is not None
    assert record.content_hash == result.content_hash
    assert record.mime_type == "text/html"
    assert record.provenance == result.provenance


def test_secure_fetch_caps_redirects():
    transport = FakeHttpTransport({
        "https://docs.example.com/0": HttpResponse(302, {"location": "/1"}, b""),
        "https://docs.example.com/1": HttpResponse(302, {"location": "/2"}, b""),
    })

    with pytest.raises(SourceDenied, match="redirect limit"):
        SecureFetcher(
            source_policy(), transport=transport, resolver=public_resolver,
            max_redirects=1,
        ).fetch("https://docs.example.com/0")
