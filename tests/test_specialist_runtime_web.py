"""Security boundary tests for allowlisted web discovery and evidence."""

from __future__ import annotations

from dataclasses import dataclass
import random
import string
import threading
import time

import pytest

from pr_reviewer.specialist_runtime import web_evidence as web
from pr_reviewer.specialist_runtime.policy import SourceRule
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.web_evidence import (
    HttpRequest,
    HttpResponse,
    SearchCandidate,
    SearxngSearchProvider,
    SecureFetcher,
    SourceDenied,
    SourcePolicy,
    StdlibHttpTransport,
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


def test_source_access_request_preserves_validated_non_default_port():
    discovery = discover(
        "schema behavior",
        FakeSearchProvider([
            SearchCandidate(None, "https://docs.example.com:8443/schema/v1")
        ]),
        source_policy(),
    )
    request = source_access_request(
        discovery.unapproved[0],
        "OB-schema",
        "confirm schema",
    )

    assert discovery.unapproved[0].host == "docs.example.com"
    assert request.host == "docs.example.com"
    assert request.candidate_url == "https://docs.example.com:8443/schema/v1"


def test_source_access_request_canonicalizes_default_https_port():
    plain = source_access_request(
        SearchCandidate(None, "https://docs.example.com/schema/v1"),
        "OB-schema",
        "confirm schema",
    )
    explicit_default = source_access_request(
        SearchCandidate(None, "https://docs.example.com:443/schema/v1"),
        "OB-schema",
        "confirm schema",
    )

    assert explicit_default.candidate_url == plain.candidate_url


@pytest.mark.parametrize(
    "candidate_url",
    (
        "http://docs.example.com:8443/schema/v1",
        "ftp://docs.example.com/schema/v1",
        "//docs.example.com:8443/schema/v1",
        "docs.example.com/schema/v1",
        "https://user:pass@docs.example.com/schema/v1",
        "https://docs.example.com:/schema/v1",
        "https://docs.example.com:bad/schema/v1",
        "https://docs.example.com:0/schema/v1",
        "https://docs.example.com:99999/schema/v1",
    ),
)
def test_source_access_request_rejects_unsafe_or_invalid_authority(candidate_url):
    with pytest.raises(ValueError, match="requires a valid URL authority"):
        source_access_request(
            SearchCandidate(None, candidate_url),
            "OB-schema",
            "confirm schema",
        )


def test_source_access_request_preserves_case_normalized_https_scheme():
    request = source_access_request(
        SearchCandidate(None, "HTTPS://docs.example.com:8443/schema/v1"),
        "OB-schema",
        "confirm schema",
    )

    assert request.candidate_url == "https://docs.example.com:8443/schema/v1"


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


_RANDOM_BASE36_RANDOM = random.Random(221)
_RANDOM_BASE36_TOKEN = "".join(
    _RANDOM_BASE36_RANDOM.choices(string.ascii_lowercase + string.digits, k=64)
)


@pytest.mark.parametrize("token", [
    pytest.param(_RANDOM_BASE36_TOKEN, id="random-lowercase-base36"),
    pytest.param("0123456789abcdef" * 4, id="hex"),
    pytest.param("QWERTYUIOPASDFGHJKLZXCVBNM" * 2, id="uppercase"),
    pytest.param("aZ9mQ2xK7vP4nR8sT1uW5yB3cD6eF0gH" * 2, id="mixed"),
])
def test_entropy_detection_is_independent_of_alphabet_composition(token):
    with pytest.raises(SourceDenied, match="high-entropy"):
        discover(f"lookup {token}", FakeSearchProvider([]), source_policy())

    assert source_policy().classify(
        f"https://docs.example.com/api/{token}"
    ).approved is False

    denied = discover(
        "api behavior",
        FakeSearchProvider([SearchCandidate(
            "candidate", f"https://evil.example/{token}", "snippet",
        )]),
        source_policy(),
    ).unapproved[0]
    assert token not in denied.path
    assert denied.path == "/[REDACTED]"


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


@pytest.mark.parametrize("endpoint", [
    "http://search.example.com/search",
    "https://user:pass@search.example.com/search",
    "https://search.example.com:8443/search",
    "https://search.example.com/search#fragment",
])
def test_search_provider_rejects_unsafe_endpoint_syntax(endpoint):
    with pytest.raises(SourceDenied):
        SearxngSearchProvider(endpoint, transport=FakeHttpTransport({}), resolver=public_resolver)


def test_search_provider_uses_dns_pinned_transport_and_canonical_endpoint():
    expected = "https://search.example.com/search?q=api+support&format=json"
    transport = FakeHttpTransport({
        expected: HttpResponse(200, {"content-type": "application/json"}, b'{"results": []}'),
    })
    provider = SearxngSearchProvider(
        "https://SEARCH.EXAMPLE.COM:443/search",
        transport=transport,
        resolver=public_resolver,
    )

    assert provider.search("api support", limit=5) == ()
    assert transport.requests[0].url == expected
    assert transport.requests[0].resolved_ip == PUBLIC_IP


def test_search_redirect_cannot_leak_query_to_another_host():
    source = "https://search.example.com/search?q=secretless+query&format=json"
    transport = FakeHttpTransport.redirecting(source, "https://evil.example/collect")
    provider = SearxngSearchProvider(
        "https://search.example.com/search",
        transport=transport,
        resolver=public_resolver,
    )

    with pytest.raises(SourceDenied, match="redirect"):
        provider.search("secretless query", limit=5)

    assert len(transport.requests) == 1


def test_search_rejects_private_provider_resolution_before_transport():
    transport = FakeHttpTransport({})
    provider = SearxngSearchProvider(
        "https://search.example.com/search",
        transport=transport,
        resolver=lambda host, port: ["127.0.0.1"],
    )

    with pytest.raises(SourceDenied, match="address"):
        provider.search("api", limit=5)

    assert transport.requests == []


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class SlowDripTransport:
    def __init__(self, clock: FakeClock, response: HttpResponse) -> None:
        self.clock = clock
        self.response = response
        self.requests: list[HttpRequest] = []

    def request(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        for _ in range(4):
            self.clock.advance(0.3)
        return self.response


def test_fetch_has_one_hard_deadline_across_slow_body():
    clock = FakeClock()
    url = "https://docs.example.com/api"
    transport = SlowDripTransport(
        clock, HttpResponse(200, {"content-type": "text/plain"}, b"slow body"),
    )
    fetcher = SecureFetcher(
        source_policy(), transport=transport, resolver=public_resolver,
        timeout=1.0, monotonic=clock,
    )

    with pytest.raises(SourceDenied, match="deadline"):
        fetcher.fetch(url)

    assert transport.requests[0].timeout <= 1.0


def test_fetch_passes_remaining_deadline_to_timeout_aware_resolver():
    clock = FakeClock()
    url = "https://docs.example.com/api"
    transport = FakeHttpTransport({
        url: HttpResponse(200, {"content-type": "text/plain"}, b"ok"),
    })
    seen_timeouts = []

    def resolver(host: str, port: int, timeout: float) -> list[str]:
        seen_timeouts.append(timeout)
        return [PUBLIC_IP]

    SecureFetcher(
        source_policy(), transport=transport, resolver=resolver,
        timeout=2.0, monotonic=clock,
    ).fetch(url)

    assert seen_timeouts == [pytest.approx(2.0)]


def test_search_has_one_hard_deadline_across_slow_body():
    clock = FakeClock()
    url = "https://search.example.com/search?q=api&format=json"
    transport = SlowDripTransport(
        clock, HttpResponse(200, {"content-type": "application/json"}, b'{"results": []}'),
    )
    provider = SearxngSearchProvider(
        "https://search.example.com/search", transport=transport,
        resolver=public_resolver, request_timeout=1.0, monotonic=clock,
    )

    with pytest.raises(SourceDenied, match="deadline"):
        provider.search("api", limit=5)


def test_fetch_sends_exact_canonical_target_that_policy_authorized():
    canonical = "https://docs.example.com/api/a?view=1"
    transport = FakeHttpTransport({
        canonical: HttpResponse(200, {"content-type": "text/plain"}, b"ok"),
    })
    fetcher = SecureFetcher(
        source_policy(), transport=transport, resolver=public_resolver,
    )

    result = fetcher.fetch(
        "https://DOCS.EXAMPLE.COM:443/api/%2561?view=%2531"
    )

    assert transport.requests[0].url == canonical
    assert result.provenance.final_url == canonical


@pytest.mark.parametrize("unsafe_path", [
    "/api/%2525252e%2525252e/admin",
    "/api/encoded%25252fdelimiter",
])
def test_source_policy_fails_closed_when_canonical_decode_is_not_stable(unsafe_path):
    decision = source_policy().classify(f"https://docs.example.com{unsafe_path}")

    assert decision.approved is False


def test_source_policy_rejects_invalid_percent_encoding():
    assert source_policy().classify(
        "https://docs.example.com/api/%ZZ"
    ).approved is False


def test_denied_metadata_strips_authority_query_and_suspicious_path():
    provider = FakeSearchProvider([SearchCandidate(
        "hostile title",
        "https://user:pass@evil.example/ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA?q=secret#frag",
        "hostile snippet",
    )])

    discovery = discover("api", provider, source_policy())
    denied = discovery.unapproved[0]
    serialized = discovery.to_tool_result()

    assert denied.host == ""
    assert denied.path == "/[REDACTED]"
    assert denied.url == ""
    with pytest.raises(ValueError, match="requires a valid URL authority"):
        source_access_request(denied, "OB-api", "verify behavior")
    assert all(marker not in serialized for marker in (
        "user", "pass", "q=", "secret", "frag", "hostile title", "hostile snippet",
    ))


class _FakeSocket:
    def __init__(self) -> None:
        self.closed = False
        self.timeouts: list[float] = []

    def settimeout(self, timeout: float) -> None:
        self.timeouts.append(timeout)

    def close(self) -> None:
        self.closed = True


class _BlockingHeaderConnection:
    def __init__(self) -> None:
        self.sock = _FakeSocket()
        self.closed = False
        self.release = threading.Event()

    def connect(self) -> None:
        return None

    def request(self, *args, **kwargs) -> None:
        return None

    def getresponse(self):
        self.release.wait()
        raise OSError("connection closed")

    def close(self) -> None:
        self.closed = True
        self.sock.close()
        self.release.set()


class _SlowDripResponse:
    status = 200

    def __init__(self) -> None:
        self.read1_calls = 0

    def getheaders(self):
        return [("content-type", "text/plain")]

    def read1(self, size: int) -> bytes:
        self.read1_calls += 1
        time.sleep(0.025)
        return b"x"


class _BlockingChunkFramingResponse:
    def __init__(self) -> None:
        self.read_calls = 0
        self.read_started = threading.Event()
        self.release = threading.Event()
        self._state_lock = threading.Lock()
        self._active_reads = 0
        self.max_active_reads = 0
        self.concurrent_access = False

    @property
    def status(self) -> int:
        with self._state_lock:
            if self._active_reads:
                self.concurrent_access = True
        return 200

    def getheaders(self):
        with self._state_lock:
            if self._active_reads:
                self.concurrent_access = True
        return [("content-type", "text/plain"), ("transfer-encoding", "chunked")]

    def read1(self, size: int) -> bytes:
        self.read_calls += 1
        if self.read_calls == 1:
            return b"x"
        with self._state_lock:
            self._active_reads += 1
            self.max_active_reads = max(self.max_active_reads, self._active_reads)
        self.read_started.set()
        try:
            self.release.wait()
            return b""
        finally:
            with self._state_lock:
                self._active_reads -= 1


class _ImmediateConnection:
    def __init__(self, response) -> None:
        self.sock = _FakeSocket()
        self.response = response
        self.closed = False

    def connect(self) -> None:
        return None

    def request(self, *args, **kwargs) -> None:
        return None

    def getresponse(self):
        return self.response

    def close(self) -> None:
        self.closed = True
        self.sock.close()


def _transport_request(timeout: float) -> HttpRequest:
    return HttpRequest(
        url="https://docs.example.com/api", resolved_ip=PUBLIC_IP,
        timeout=timeout, deadline=time.monotonic() + timeout, max_bytes=1024,
        headers={},
    )


def test_transport_slow_headers_obey_elapsed_hard_deadline_and_close_socket():
    connection = _BlockingHeaderConnection()
    guard = web._BoundedBlockingCallGuard(max_outstanding=2)
    transport = StdlibHttpTransport(
        connection_factory=lambda *args: connection,
        blocking_guard=guard,
    )

    started = time.monotonic()
    with pytest.raises(SourceDenied, match="deadline"):
        transport.request(_transport_request(0.05))
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert connection.closed is True
    assert connection.sock.closed is True


def test_transport_slow_drip_body_uses_read_one_and_obeys_elapsed_deadline():
    response = _SlowDripResponse()
    connection = _ImmediateConnection(response)
    guard = web._BoundedBlockingCallGuard(max_outstanding=2)
    transport = StdlibHttpTransport(
        connection_factory=lambda *args: connection,
        blocking_guard=guard,
    )

    started = time.monotonic()
    with pytest.raises(SourceDenied, match="deadline"):
        transport.request(_transport_request(0.06))
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert response.read1_calls >= 2
    assert connection.closed is True


def test_transport_blocked_chunk_framing_returns_at_deadline_without_shared_response():
    response = _BlockingChunkFramingResponse()
    connection = _ImmediateConnection(response)
    guard = web._BoundedBlockingCallGuard(max_outstanding=2)
    transport = StdlibHttpTransport(
        connection_factory=lambda *args: connection,
        blocking_guard=guard,
    )
    outcome: dict[str, object] = {}

    def invoke() -> None:
        try:
            outcome["response"] = transport.request(_transport_request(0.05))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            outcome["error"] = exc

    caller = threading.Thread(target=invoke, daemon=True)
    caller.start()
    assert response.read_started.wait(0.2)
    try:
        caller.join(0.2)
        assert caller.is_alive() is False
        assert isinstance(outcome.get("error"), SourceDenied)
        assert "deadline" in str(outcome["error"])
        assert connection.closed is True
        assert guard.outstanding == 1
        assert response.max_active_reads == 1
        assert response.concurrent_access is False
    finally:
        response.release.set()
        caller.join(0.2)
    release_deadline = time.monotonic() + 0.2
    while guard.outstanding and time.monotonic() < release_deadline:
        time.sleep(0.005)
    assert guard.outstanding == 0


def test_guard_thread_construction_failure_rolls_back_capacity():
    calls = 0

    def thread_factory(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("thread construction failed")
        return threading.Thread(**kwargs)

    guard = web._BoundedBlockingCallGuard(
        max_outstanding=1, thread_factory=thread_factory,
    )
    with pytest.raises(RuntimeError, match="construction"):
        guard.run(
            lambda: "never", deadline=time.monotonic() + 1,
            monotonic=time.monotonic, name="test construction",
        )

    assert guard.outstanding == 0
    assert guard.run(
        lambda: "ok", deadline=time.monotonic() + 1,
        monotonic=time.monotonic, name="test recovery",
    ) == "ok"
    assert guard.outstanding == 0


def test_guard_thread_start_failure_rolls_back_capacity():
    calls = 0

    class StartFailure:
        def start(self) -> None:
            raise RuntimeError("thread start failed")

    def thread_factory(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return StartFailure()
        return threading.Thread(**kwargs)

    guard = web._BoundedBlockingCallGuard(
        max_outstanding=1, thread_factory=thread_factory,
    )
    with pytest.raises(RuntimeError, match="start"):
        guard.run(
            lambda: "never", deadline=time.monotonic() + 1,
            monotonic=time.monotonic, name="test start",
        )

    assert guard.outstanding == 0
    assert guard.run(
        lambda: "ok", deadline=time.monotonic() + 1,
        monotonic=time.monotonic, name="test recovery",
    ) == "ok"
    assert guard.outstanding == 0


def test_guard_deadline_crossed_after_start_invokes_timeout_callback():
    release = threading.Event()
    timeout_called = threading.Event()
    times = iter((0.0, 2.0))
    guard = web._BoundedBlockingCallGuard(max_outstanding=1)

    def on_timeout() -> None:
        timeout_called.set()
        release.set()

    try:
        with pytest.raises(SourceDenied, match="deadline"):
            guard.run(
                lambda: release.wait(), deadline=1.0,
                monotonic=lambda: next(times), name="test deadline",
                on_timeout=on_timeout,
            )

        assert timeout_called.is_set()
    finally:
        release.set()


def test_two_argument_resolver_timeouts_retain_bounded_slots_and_fail_fast():
    release = threading.Event()
    guard = web._BoundedBlockingCallGuard(max_outstanding=2)

    def resolver(host: str, port: int) -> list[str]:
        release.wait()
        return [PUBLIC_IP]

    fetcher = SecureFetcher(
        source_policy(), transport=FakeHttpTransport({}), resolver=resolver,
        timeout=0.03, blocking_guard=guard,
    )
    try:
        for _ in range(2):
            with pytest.raises(SourceDenied, match="deadline"):
                fetcher.fetch("https://docs.example.com/api")
        assert guard.outstanding == 2

        started = time.monotonic()
        with pytest.raises(SourceDenied, match="capacity"):
            fetcher.fetch("https://docs.example.com/api")
        assert time.monotonic() - started < 0.02
        assert guard.outstanding == 2
    finally:
        release.set()
