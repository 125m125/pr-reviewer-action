"""Allowlisted web discovery and approved external evidence collection.

Search results are discovery hints.  They become evidence only after the URL
passes :class:`SourcePolicy` and is retrieved by the secure fetch boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
import inspect
import json
import math
from pathlib import Path
import queue
import re
import socket
import ssl
import sys
import threading
import time
from typing import Callable, Iterable, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlsplit, urlunsplit

from .evidence import EvidenceProvenance, EvidenceStore
from .policy import ReviewPolicy, SourceRule

_SCRIPTS_DIR = str(Path(__file__).parents[2] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from redact import mask_and_truncate, mask_secrets


DEFAULT_SEARCH_SCAN_LIMIT = 25
DEFAULT_MAX_SEARCH_RESULTS = 5
MAX_SEARCH_QUERY_CHARS = 500


class SourceDenied(ValueError):
    """A URL or query was rejected at the external-source trust boundary."""


@dataclass(frozen=True)
class SourceDecision:
    approved: bool
    host: str
    path: str
    classification: str | None = None
    rule_id: str | None = None
    max_age_hours: int | None = None
    reason: str | None = None
    canonical_url: str | None = None


@dataclass(frozen=True)
class SearchCandidate:
    title: str | None
    url: str
    snippet: str | None = None
    host: str = ""
    path: str = ""
    classification: str | None = None
    denial_reason: str | None = None


class SearchProvider(Protocol):
    """A fixed-endpoint discovery provider."""

    def search(self, query: str, *, limit: int) -> Sequence[SearchCandidate]: ...


class SearxngSearchProvider:
    """SearXNG JSON client whose endpoint is fixed by the operator."""

    def __init__(
        self,
        endpoint: str,
        *,
        request_timeout: float = 20,
        transport: HttpTransport | None = None,
        resolver: Callable[[str, int], Sequence[str]] | None = None,
        max_response_bytes: int = 1024 * 1024,
        max_redirects: int = 3,
        monotonic: Callable[[], float] = time.monotonic,
        blocking_guard: _BoundedBlockingCallGuard | None = None,
    ) -> None:
        canonical, host = _canonical_search_endpoint(endpoint)
        if request_timeout <= 0 or max_response_bytes <= 0 or max_redirects < 0:
            raise ValueError("search provider limits must be positive")
        self.endpoint = canonical
        self.host = host
        self.request_timeout = request_timeout
        self.blocking_guard = blocking_guard or _BLOCKING_CALL_GUARD
        self.transport = transport or StdlibHttpTransport(
            monotonic=monotonic, blocking_guard=self.blocking_guard,
        )
        self.resolver = resolver or _default_resolver
        self.max_response_bytes = max_response_bytes
        self.max_redirects = max_redirects
        self.monotonic = monotonic

    @classmethod
    def is_valid_endpoint(cls, endpoint: str) -> bool:
        try:
            _canonical_search_endpoint(endpoint)
        except (SourceDenied, ValueError):
            return False
        return True

    def search(self, query: str, *, limit: int) -> Sequence[SearchCandidate]:
        if limit <= 0:
            return ()
        parsed = urlsplit(self.endpoint)
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        query_pairs.extend((("q", query), ("format", "json")))
        current_url = urlunsplit((
            "https", self.host, parsed.path or "/", urlencode(query_pairs), "",
        ))
        deadline = self.monotonic() + self.request_timeout
        response: HttpResponse | None = None
        for redirect_count in range(self.max_redirects + 1):
            canonical, host = _canonical_search_endpoint(current_url, required_host=self.host)
            addresses = _public_addresses(
                host, 443, self.resolver, deadline, self.monotonic,
                self.blocking_guard,
            )
            remaining = _remaining(deadline, self.monotonic)
            response = self.transport.request(HttpRequest(
                url=canonical,
                resolved_ip=addresses[0],
                timeout=remaining,
                max_bytes=self.max_response_bytes,
                deadline=deadline,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "ai-pr-reviewer/search-discovery",
                },
            ))
            _remaining(deadline, self.monotonic)
            if response.status not in _REDIRECT_STATUSES:
                break
            location = _header(response.headers, "location").strip()
            if not location or redirect_count >= self.max_redirects:
                raise SourceDenied("search redirect limit exceeded or omitted Location")
            target = urljoin(canonical, location)
            try:
                current_url, _ = _canonical_search_endpoint(target, required_host=self.host)
            except SourceDenied as exc:
                raise SourceDenied(f"search redirect denied: {exc}") from exc
        if response is None or not 200 <= response.status < 300:
            raise SourceDenied("search provider returned a non-success response")
        if _mime_type(response.headers) != "application/json":
            raise SourceDenied("search provider response MIME type is not JSON")
        if _header(response.headers, "content-encoding").strip().lower() not in {"", "identity"}:
            raise SourceDenied("compressed search responses are not accepted")
        raw = response.body
        if len(raw) > self.max_response_bytes:
            raise SourceDenied("search provider response exceeded maximum size")
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        _remaining(deadline, self.monotonic)
        if not isinstance(payload, dict):
            raise SourceDenied("search provider returned invalid JSON")
        candidates = []
        for item in payload.get("results") or []:
            if len(candidates) >= limit:
                break
            if not isinstance(item, dict):
                continue
            candidates.append(SearchCandidate(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("content") or ""),
            ))
        return tuple(candidates)


@dataclass(frozen=True)
class HttpRequest:
    url: str
    resolved_ip: str
    timeout: float
    max_bytes: int
    headers: Mapping[str, str]
    deadline: float | None = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    def request(self, request: HttpRequest) -> HttpResponse: ...


@dataclass(frozen=True)
class FetchedEvidence:
    content: str
    content_hash: str
    mime_type: str
    truncated: bool
    provenance: EvidenceProvenance
    evidence_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": "external_evidence",
            "evidence_id": self.evidence_id,
            "content": self.content,
            "content_hash": self.content_hash,
            "mime_type": self.mime_type,
            "truncated": self.truncated,
            "provenance": {
                "original_url": self.provenance.original_url,
                "final_url": self.provenance.final_url,
                "retrieved_at": self.provenance.retrieved_at,
                "policy_hash": self.provenance.policy_hash,
                "policy_rule_id": self.provenance.policy_rule_id,
                "source_classification": self.provenance.source_classification,
                "max_age_hours": self.provenance.max_age_hours,
                "content_hash": self.content_hash,
                "mime_type": self.mime_type,
                "truncated": self.truncated,
            },
            "evidentiary": True,
        }

    def to_tool_result(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class SearchDiscovery:
    redacted_query: str
    approved: tuple[SearchCandidate, ...]
    unapproved: tuple[SearchCandidate, ...]
    suppressed_result_count: int = 0

    def as_dict(self) -> dict[str, object]:
        def approved(candidate: SearchCandidate) -> dict[str, object]:
            return {
                "title": candidate.title,
                "url": candidate.url,
                "host": candidate.host,
                "path": candidate.path,
                "snippet": candidate.snippet,
                "classification": candidate.classification,
            }

        def unapproved(candidate: SearchCandidate) -> dict[str, object]:
            # Deliberately omit the provider snippet and title: neither came
            # from a source approved to put content in the model context.
            return {
                "url": candidate.url,
                "host": candidate.host,
                "path": candidate.path,
                "denial_reason": candidate.denial_reason,
            }

        return {
            "kind": "search_discovery",
            "query": self.redacted_query,
            "approved": [approved(item) for item in self.approved],
            "unapproved": [unapproved(item) for item in self.unapproved],
            "suppressed_result_count": self.suppressed_result_count,
            "evidentiary": False,
        }

    def to_tool_result(self) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class SourceAccessRequest:
    host: str
    candidate_url: str
    obligation_id: str
    purpose: str
    authority_reason: str = ""
    model_purpose: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": "source_access_request",
            "host": self.host,
            "candidate_url": self.candidate_url,
            "obligation_id": self.obligation_id,
            "purpose": self.purpose,
            "authority_reason": self.authority_reason,
            "model_purpose": self.model_purpose,
        }


@dataclass(frozen=True)
class RepositoryAccessRequest:
    repository: str
    endpoint: str
    obligation_id: str
    purpose: str
    authority_reason: str
    revision: str | None = None
    model_purpose: str = ""

    def as_dict(self) -> dict[str, str | None]:
        return {
            "kind": "repository_access_request",
            "repository": self.repository,
            "endpoint": self.endpoint,
            "revision": self.revision,
            "obligation_id": self.obligation_id,
            "purpose": self.purpose,
            "model_purpose": self.model_purpose,
            "authority_reason": self.authority_reason,
        }


def access_request_identity(
    request: SourceAccessRequest | RepositoryAccessRequest,
) -> tuple[str, ...]:
    """Return the authorization-neutral identity used for deduplication."""
    if isinstance(request, RepositoryAccessRequest):
        return (
            "repository", request.obligation_id, request.repository,
            request.endpoint, request.purpose,
        )
    return (
        "source", request.obligation_id, request.host,
        request.candidate_url, request.purpose,
    )


class SourcePolicy:
    """Classify URLs against immutable current-head source rules."""

    def __init__(
        self, rules: Iterable[SourceRule], *, policy_hash: str | None = None,
    ) -> None:
        self.rules = tuple(rules)
        canonical = [{
            "host": rule.host,
            "include_subdomains": rule.include_subdomains,
            "path_prefixes": list(rule.path_prefixes),
            "classification": rule.classification,
            "max_age_hours": rule.max_age_hours,
            "schemes": list(rule.schemes),
        } for rule in self.rules]
        self.policy_hash = policy_hash or hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_review_policy(
        cls, policy: ReviewPolicy, *, policy_hash: str | None = None,
    ) -> "SourcePolicy":
        return cls(policy.sources, policy_hash=policy_hash)

    @classmethod
    def from_hosts(cls, hosts: Iterable[str]) -> "SourcePolicy":
        rules = []
        for host in hosts:
            normalized = str(host).strip().lower().rstrip(".")
            if normalized:
                rules.append(SourceRule(host=normalized))
        return cls(rules)

    @property
    def has_approved_sources(self) -> bool:
        return bool(self.rules)

    def classify(self, url: str) -> SourceDecision:
        try:
            parsed = urlsplit(str(url).strip())
            port = parsed.port
        except ValueError:
            return SourceDecision(False, "", "", reason="invalid URL")
        host = _normalize_hostname(parsed.hostname)
        path, path_error = _decode_url_component(parsed.path or "/", component="path")
        if parsed.scheme.lower() != "https":
            return SourceDecision(False, host, path, reason="HTTPS is required")
        if not host:
            return SourceDecision(False, host, path, reason="URL requires a host")
        if parsed.username is not None or parsed.password is not None:
            return SourceDecision(False, host, path, reason="URL credentials are forbidden")
        if _authority_has_empty_port(parsed.netloc):
            return SourceDecision(False, host, path, reason="URL port is empty")
        if parsed.fragment:
            return SourceDecision(False, host, path, reason="URL fragments are forbidden")
        if port not in (None, 443):
            return SourceDecision(False, host, path, reason="port is not approved")
        if path_error:
            return SourceDecision(False, host, path, reason=path_error)
        query_payload, query_error = _decode_url_component(
            parsed.query, component="query"
        )
        if query_error:
            return SourceDecision(False, host, path, reason=query_error)
        payload = f"{path}?{query_payload}"
        if mask_secrets(payload) != payload or _CREDENTIAL_QUERY_RE.search(query_payload):
            return SourceDecision(False, host, path, reason="unsafe credential-like URL payload")
        if any(_looks_high_entropy(token) for token in _TOKEN_RE.findall(payload)):
            return SourceDecision(False, host, path, reason="unsafe high-entropy URL payload")
        canonical_url = urlunsplit((
            "https",
            host,
            _encode_path(path),
            _encode_query(query_payload),
            "",
        ))
        for index, rule in enumerate(self.rules):
            host_matches = host == rule.host or (
                rule.include_subdomains and host.endswith("." + rule.host)
            )
            if not host_matches:
                continue
            if rule.path_prefixes and not any(
                path == prefix or path.startswith(prefix.rstrip("/") + "/")
                for prefix in rule.path_prefixes
            ):
                continue
            return SourceDecision(
                True,
                host,
                path,
                classification=rule.classification,
                rule_id=f"source:{index}:{rule.host}",
                max_age_hours=rule.max_age_hours,
                canonical_url=canonical_url,
            )
        return SourceDecision(False, host, path, reason="source is not allowlisted by current policy")


def _normalize_hostname(host: str | None) -> str:
    if not host:
        return ""
    try:
        return host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return ""


def _authority_has_empty_port(netloc: str) -> bool:
    authority = str(netloc).rsplit("@", 1)[-1]
    return authority.endswith(":")


_MAX_URL_DECODE_PASSES = 3
_ENCODED_DELIMITER_RE = re.compile(r"(?i)%(?:2f|5c|3f|23|40)")


def _decode_url_component(value: str, *, component: str) -> tuple[str, str | None]:
    try:
        current = value
        for _ in range(_MAX_URL_DECODE_PASSES):
            if _ENCODED_DELIMITER_RE.search(current):
                return current, f"encoded URL {component} delimiter is forbidden"
            decoded = unquote(current, errors="strict")
            if decoded == current:
                break
            current = decoded
    except (UnicodeDecodeError, ValueError):
        return "", f"invalid URL {component} encoding"
    if "%" in current:
        return current, f"URL {component} encoding did not stabilize"
    if any(ord(character) < 32 or ord(character) == 127 for character in current):
        return current, f"unsafe URL {component} control character"
    if component == "path":
        if "\\" in current:
            return current, "unsafe URL path"
        if any(segment in {".", ".."} for segment in current.split("/")):
            return current, "URL path traversal is forbidden"
        current = current if current.startswith("/") else "/" + current
    return current, None


def _safe_path(raw_path: str) -> tuple[str, str | None]:
    return _decode_url_component(raw_path or "/", component="path")


def _encode_path(path: str) -> str:
    from urllib.parse import quote
    return quote(path, safe="/-._~")


def _encode_query(query: str) -> str:
    from urllib.parse import quote
    return quote(query, safe="=&+-._~")


def _canonical_search_endpoint(
    endpoint: str, *, required_host: str | None = None,
) -> tuple[str, str]:
    try:
        parsed = urlsplit(str(endpoint).strip())
        port = parsed.port
    except ValueError as exc:
        raise SourceDenied("search endpoint URL is invalid") from exc
    host = _normalize_hostname(parsed.hostname)
    if (
        parsed.scheme.lower() != "https"
        or not host
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.fragment)
    ):
        raise SourceDenied(
            "search endpoint must be credential-free HTTPS on port 443 without a fragment"
        )
    if required_host is not None and host != required_host:
        raise SourceDenied("search endpoint host changed")
    path, path_error = _decode_url_component(parsed.path or "/", component="path")
    if path_error:
        raise SourceDenied(path_error)
    try:
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=50)
    except ValueError as exc:
        raise SourceDenied("search endpoint query is invalid") from exc
    canonical_query = urlencode(query_pairs)
    return urlunsplit(("https", host, _encode_path(path), canonical_query, "")), host


def _remaining(deadline: float, monotonic: Callable[[], float]) -> float:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise SourceDenied("web operation hard deadline exceeded")
    return remaining


def _safe_discovery_url(url: str) -> tuple[str, str, str]:
    """Return content-free denied metadata: URL, host, and sanitized path."""
    try:
        parsed = urlsplit(str(url).strip())
        host = _normalize_hostname(parsed.hostname)
        port = parsed.port
    except ValueError:
        return "", "", "/[REDACTED]"
    if (
        parsed.scheme.lower() != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or _authority_has_empty_port(parsed.netloc)
        or (port is not None and not 1 <= port <= 65535)
    ):
        return "", "", "/[REDACTED]"
    host = mask_secrets(host)[:253]
    path, path_error = _decode_url_component(parsed.path or "/", component="path")
    suspicious = path == "/[REDACTED]" or (
        bool(path_error)
        or len(path) > 300
        or mask_secrets(path) != path
        or any(_looks_high_entropy(token) for token in _TOKEN_RE.findall(path))
    )
    safe_path = "/[REDACTED]" if suspicious else _encode_path(path)[:300]
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != 443:
        netloc = f"{netloc}:{port}"
    return f"https://{netloc}{safe_path}", host, safe_path


_CREDENTIAL_QUERY_RE = re.compile(
    r"(?i)(?:authorization\s*:|bearer\s+|(?:api[_-]?key|access[_-]?token|password|secret|token)\s*[=:])"
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_+/=-]{32,}")


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    return -sum(
        (count / len(value)) * math.log2(count / len(value))
        for count in (value.count(character) for character in set(value))
    )


def _looks_high_entropy(token: str) -> bool:
    # Secrets and opaque identifiers are not required to mix character classes:
    # lowercase base36 tokens, uppercase tokens, and hexadecimal hashes are all
    # common.  Length, symbol diversity, and measured entropy form the boundary.
    return len(token) >= 32 and len(set(token)) >= 12 and _entropy(token) >= 3.5


def _validated_query(query: str) -> str:
    raw = str(query or "").strip()
    if not raw:
        raise SourceDenied("search query must be non-empty")
    if len(raw) > MAX_SEARCH_QUERY_CHARS:
        raise SourceDenied("search query exceeds maximum length")
    masked = mask_secrets(raw)
    if masked != raw or _CREDENTIAL_QUERY_RE.search(raw):
        raise SourceDenied("search query contains a credential-like value")
    for token in _TOKEN_RE.findall(raw):
        if _looks_high_entropy(token):
            raise SourceDenied("search query contains a high-entropy token")
    return masked


def discover(
    query: str,
    provider: SearchProvider,
    source_policy: SourcePolicy,
    *,
    search_scan_limit: int = DEFAULT_SEARCH_SCAN_LIMIT,
    tool_max_search_results: int = DEFAULT_MAX_SEARCH_RESULTS,
) -> SearchDiscovery:
    if search_scan_limit <= 0 or tool_max_search_results <= 0:
        raise ValueError("search result limits must be positive")
    clean_query = _validated_query(query)
    candidates = tuple(provider.search(clean_query, limit=search_scan_limit))[
        :search_scan_limit
    ]
    approved: list[SearchCandidate] = []
    unapproved: list[SearchCandidate] = []
    suppressed = 0
    for candidate in candidates:
        decision = source_policy.classify(candidate.url)
        if decision.approved:
            title, _ = mask_and_truncate(str(candidate.title or ""), 300)
            snippet, _ = mask_and_truncate(str(candidate.snippet or ""), 500)
            normalized = replace(
                candidate,
                title=title,
                url=decision.canonical_url or "",
                snippet=snippet,
                host=decision.host,
                path=decision.path,
                classification=decision.classification,
                denial_reason=None,
            )
            if len(approved) < tool_max_search_results:
                approved.append(normalized)
            else:
                suppressed += 1
        else:
            # Never retain provider-controlled content for unapproved sources.
            safe_url, safe_host, safe_path = _safe_discovery_url(candidate.url)
            unapproved.append(SearchCandidate(
                title=None,
                url=safe_url,
                snippet=None,
                host=safe_host,
                path=safe_path,
                denial_reason=decision.reason,
            ))
    return SearchDiscovery(
        redacted_query=clean_query,
        approved=tuple(approved),
        unapproved=tuple(unapproved),
        suppressed_result_count=suppressed,
    )


def source_access_request(
    candidate: SearchCandidate,
    obligation_id: str,
    purpose: str,
    authority_reason: str = "",
    model_purpose: str = "",
) -> SourceAccessRequest:
    safe_url, safe_host, _ = _safe_discovery_url(candidate.url)
    if not safe_host:
        raise ValueError("source access candidate requires a valid URL authority")
    if not str(obligation_id).strip() or not str(purpose).strip():
        raise ValueError("source access request requires obligation_id and purpose")
    return SourceAccessRequest(
        host=safe_host,
        candidate_url=safe_url,
        obligation_id=mask_secrets(str(obligation_id).strip())[:160],
        purpose=mask_secrets(str(purpose).strip())[:1000],
        authority_reason=mask_secrets(str(authority_reason).strip())[:1000],
        model_purpose=_bounded_request_text(model_purpose, 300),
    )


_REPOSITORY_ENDPOINT = re.compile(
    r"^repos/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/.*)?$"
)
_COMMIT_ENDPOINT = re.compile(
    r"^repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/commits/([0-9a-fA-F]{40})$"
)


def _bounded_request_text(value: object, limit: int) -> str:
    return " ".join(mask_secrets(str(value or "")).split())[:limit]


def repository_access_request(
    endpoint: str,
    obligation_id: str,
    assignment_objective: str,
    model_purpose: str = "",
    authority_reason: str = "",
) -> RepositoryAccessRequest:
    """Describe a denied repository lookup without authorizing or fetching it."""
    normalized = str(endpoint or "").strip().strip("/")
    match = _REPOSITORY_ENDPOINT.fullmatch(normalized)
    if match is None:
        raise ValueError("repository API endpoint must identify repos/owner/repo")
    repository = f"{match.group(1)}/{match.group(2)}"
    # Reuse the actual gh_api security boundary so request projection cannot
    # legitimize an endpoint that execution would reject for another reason.
    from pr_reviewer.platform import _validate_endpoint

    validated = _validate_endpoint(normalized, {repository}, "")
    if "error" in validated:
        raise ValueError("repository API endpoint is not safely requestable")
    canonical = str(validated["full_path"]).strip("/")
    obligation = _bounded_request_text(obligation_id, 160)
    objective = _bounded_request_text(assignment_objective, 300)
    if not obligation or not objective:
        raise ValueError("repository access request requires obligation and objective")
    revision_match = _COMMIT_ENDPOINT.fullmatch(canonical)
    revision = revision_match.group(1).lower() if revision_match else None
    if revision:
        purpose = (
            f"Verify existence, provenance, metadata, and bounded changed-file "
            f"information for the exact pinned repository revision {revision} "
            f"in {repository} for this assignment: {objective}"
        )
    else:
        purpose = (
            f"Retrieve bounded read-only GitHub API metadata from {repository} "
            f"for this assignment: {objective}"
        )
    return RepositoryAccessRequest(
        repository=repository,
        endpoint=canonical,
        revision=revision,
        obligation_id=obligation,
        purpose=purpose[:1000],
        model_purpose=_bounded_request_text(model_purpose, 300),
        authority_reason=_bounded_request_text(authority_reason, 500),
    )


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_ALLOWED_MIME_TYPES = frozenset({
    "application/json",
    "application/xml",
    "application/xhtml+xml",
    "text/html",
    "text/markdown",
    "text/plain",
    "text/xml",
})
_METADATA_ADDRESSES = frozenset({
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("100.100.100.200"),
})


class _BoundedBlockingCallGuard:
    """Run uncancellable blocking calls without permitting thread exhaustion.

    A timed-out worker retains its slot until it actually exits.  New calls fail
    closed immediately once all slots are occupied instead of creating an
    unbounded succession of daemon threads.
    """

    def __init__(
        self,
        *,
        max_outstanding: int = 4,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        if max_outstanding <= 0:
            raise ValueError("blocking-call capacity must be positive")
        self._max_outstanding = max_outstanding
        self._slots = threading.BoundedSemaphore(max_outstanding)
        self._state_lock = threading.Lock()
        self._outstanding = 0
        self._thread_factory = thread_factory

    @property
    def outstanding(self) -> int:
        with self._state_lock:
            return self._outstanding

    def run(
        self,
        operation: Callable[[], object],
        *,
        deadline: float,
        monotonic: Callable[[], float],
        name: str,
        on_timeout: Callable[[], None] | None = None,
    ) -> object:
        _remaining(deadline, monotonic)
        if not self._slots.acquire(blocking=False):
            raise SourceDenied("web blocking-call capacity exhausted")
        with self._state_lock:
            self._outstanding += 1
        outcome: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
        release_lock = threading.Lock()
        slot_released = False

        def release_slot_once() -> None:
            nonlocal slot_released
            with release_lock:
                if slot_released:
                    return
                slot_released = True
                with self._state_lock:
                    self._outstanding -= 1
                self._slots.release()

        def worker() -> None:
            try:
                try:
                    outcome.put((True, operation()))
                except BaseException as exc:  # noqa: BLE001 - returned to caller
                    outcome.put((False, exc))
            finally:
                release_slot_once()

        try:
            thread = self._thread_factory(
                target=worker, daemon=True, name=f"secure-web-{name}",
            )
            thread.start()
        except BaseException:
            release_slot_once()
            raise

        def notify_timeout() -> None:
            if on_timeout is not None:
                try:
                    on_timeout()
                except Exception:
                    pass

        try:
            join_timeout = _remaining(deadline, monotonic)
        except SourceDenied as exc:
            notify_timeout()
            raise SourceDenied(
                f"{name} exceeded the web operation hard deadline"
            ) from exc
        thread.join(join_timeout)
        if thread.is_alive():
            notify_timeout()
            raise SourceDenied(f"{name} exceeded the web operation hard deadline")
        succeeded, value = outcome.get_nowait()
        if not succeeded:
            if isinstance(value, BaseException):
                raise value
            raise OSError(f"{name} failed")
        return value


_BLOCKING_CALL_GUARD = _BoundedBlockingCallGuard(max_outstanding=4)


def _default_resolver(host: str, port: int, timeout: float | None = None) -> list[str]:
    del timeout  # the shared blocking-call guard owns the deadline
    resolved = []
    for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        address = item[4][0]
        if address not in resolved:
            resolved.append(address)
    return resolved


def _public_addresses(
    host: str,
    port: int,
    resolver: Callable[..., Sequence[str]],
    deadline: float,
    monotonic: Callable[[], float],
    blocking_guard: _BoundedBlockingCallGuard,
) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        def resolve() -> Sequence[str]:
            timeout = _remaining(deadline, monotonic)
            try:
                inspect.signature(resolver).bind(host, port, timeout)
            except (TypeError, ValueError):
                return resolver(host, port)
            return resolver(host, port, timeout)

        try:
            raw_addresses = blocking_guard.run(
                resolve, deadline=deadline, monotonic=monotonic,
                name="DNS resolution",
            )
        except SourceDenied:
            raise
        except Exception as exc:
            raise SourceDenied("source address resolution failed") from exc
    else:
        raw_addresses = [str(literal)]
    if not raw_addresses:
        raise SourceDenied("source address resolution returned no addresses")
    approved = []
    for raw_address in raw_addresses:
        try:
            address = ipaddress.ip_address(str(raw_address).split("%", 1)[0])
        except ValueError as exc:
            raise SourceDenied("source address resolution returned an invalid address") from exc
        if (
            address in _METADATA_ADDRESSES
            or address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            raise SourceDenied("source address is private or otherwise unsafe")
        approved.append(str(address))
    return tuple(approved)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection whose TCP peer is a previously checked DNS answer."""

    def __init__(
        self, host: str, port: int, resolved_ip: str, timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout, context=context)
        self._resolved_ip = resolved_ip

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._resolved_ip, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class StdlibHttpTransport:
    """Credential-free HTTPS transport pinned to an approved resolved IP."""

    def __init__(
        self,
        *,
        ssl_context: ssl.SSLContext | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        blocking_guard: _BoundedBlockingCallGuard | None = None,
        connection_factory: Callable[..., object] | None = None,
    ) -> None:
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._monotonic = monotonic
        self._blocking_guard = blocking_guard or _BLOCKING_CALL_GUARD
        self._connection_factory = connection_factory or _PinnedHTTPSConnection

    def request(self, request: HttpRequest) -> HttpResponse:
        parsed = urlsplit(request.url)
        port = parsed.port or 443
        deadline = request.deadline or (self._monotonic() + request.timeout)
        connection = self._connection_factory(
            parsed.hostname or "", port, request.resolved_ip,
            _remaining(deadline, self._monotonic), self._ssl_context,
        )
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        try:
            connection.connect()
            connection.sock.settimeout(_remaining(deadline, self._monotonic))
            connection.request("GET", target, headers=dict(request.headers))
            connection.sock.settimeout(_remaining(deadline, self._monotonic))

            def receive_headers() -> tuple[object, dict[str, str]]:
                response = connection.getresponse()
                headers = {
                    key.lower(): value for key, value in response.getheaders()
                }
                return response, headers

            def abort_connection() -> None:
                sock = getattr(connection, "sock", None)
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    try:
                        sock.close()
                    except Exception:
                        pass
                connection.close()

            response, headers = self._blocking_guard.run(
                receive_headers,
                deadline=deadline,
                monotonic=self._monotonic,
                name="response headers",
                on_timeout=abort_connection,
            )
            read_one = getattr(response, "read1", None)
            if not callable(read_one):
                raise SourceDenied("HTTP response does not support bounded read-one semantics")
            chunks = []
            size = 0
            while size <= request.max_bytes:
                connection.sock.settimeout(_remaining(deadline, self._monotonic))
                read_size = min(16 * 1024, request.max_bytes + 1 - size)
                chunk = self._blocking_guard.run(
                    lambda: read_one(read_size),
                    deadline=deadline,
                    monotonic=self._monotonic,
                    name="response body",
                    on_timeout=abort_connection,
                )
                _remaining(deadline, self._monotonic)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            return HttpResponse(response.status, headers, b"".join(chunks))
        except SourceDenied:
            raise
        except (TimeoutError, socket.timeout) as exc:
            raise SourceDenied("web operation hard deadline exceeded") from exc
        finally:
            connection.close()


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag.lower() in {
            "br", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "pre", "tr",
        }:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif not self._ignored_depth and tag.lower() in {
            "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "p", "pre", "tr",
        }:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        lines = [" ".join(line.split()) for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def _header(headers: Mapping[str, str], name: str) -> str:
    lower_name = name.lower()
    return next(
        (str(value) for key, value in headers.items() if str(key).lower() == lower_name),
        "",
    )


def _mime_type(headers: Mapping[str, str]) -> str:
    return _header(headers, "content-type").split(";", 1)[0].strip().lower()


def _decode_body(body: bytes, content_type: str) -> str:
    charset_match = re.search(r"(?i)charset\s*=\s*['\"]?([A-Za-z0-9._-]+)", content_type)
    charset = charset_match.group(1) if charset_match else "utf-8"
    try:
        return body.decode(charset, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class SecureFetcher:
    """Fetch only policy-approved public HTTPS sources, rechecking redirects."""

    def __init__(
        self,
        policy: SourcePolicy,
        *,
        transport: HttpTransport | None = None,
        resolver: Callable[[str, int], Sequence[str]] | None = None,
        evidence_store: EvidenceStore | None = None,
        timeout: float = 20,
        max_redirects: int = 3,
        max_bytes: int = 64 * 1024,
        allowed_mime_types: Iterable[str] = _ALLOWED_MIME_TYPES,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        blocking_guard: _BoundedBlockingCallGuard | None = None,
    ) -> None:
        if timeout <= 0 or max_bytes <= 0 or max_redirects < 0:
            raise ValueError("secure fetch limits are invalid")
        self.policy = policy
        self.blocking_guard = blocking_guard or _BLOCKING_CALL_GUARD
        self.transport = transport or StdlibHttpTransport(
            monotonic=monotonic, blocking_guard=self.blocking_guard,
        )
        self.resolver = resolver or _default_resolver
        self.evidence_store = evidence_store
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self.allowed_mime_types = frozenset(
            str(item).strip().lower() for item in allowed_mime_types
        )
        self.clock = clock
        self.monotonic = monotonic

    def fetch(
        self,
        url: str,
        *,
        session_id: str = "web-fetch",
        model_identity: str = "",
        deadline_at: float | None = None,
    ) -> FetchedEvidence:
        original_url = str(url).strip()
        current_url = original_url
        deadline = self.monotonic() + self.timeout
        if deadline_at is not None:
            deadline = min(deadline, float(deadline_at))
        final_decision: SourceDecision | None = None
        response: HttpResponse | None = None
        for redirect_count in range(self.max_redirects + 1):
            decision = self.policy.classify(current_url)
            if not decision.approved:
                boundary = "redirect" if redirect_count else "source"
                raise SourceDenied(f"{boundary} denied: {decision.reason}")
            current_url = decision.canonical_url or current_url
            parsed = urlsplit(current_url)
            port = parsed.port or 443
            _remaining(deadline, self.monotonic)
            addresses = _public_addresses(
                decision.host, port, self.resolver, deadline, self.monotonic,
                self.blocking_guard,
            )
            remaining = _remaining(deadline, self.monotonic)
            request = HttpRequest(
                url=current_url,
                resolved_ip=addresses[0],
                timeout=remaining,
                max_bytes=self.max_bytes,
                deadline=deadline,
                headers={
                    "Accept": "text/plain, text/html, text/markdown, application/json, application/xml",
                    "Accept-Encoding": "identity",
                    "User-Agent": "ai-pr-reviewer/secure-fetch",
                },
            )
            try:
                response = self.transport.request(request)
            except SourceDenied:
                raise
            except Exception as exc:
                raise SourceDenied("secure fetch transport failed") from exc
            _remaining(deadline, self.monotonic)
            if response.status not in _REDIRECT_STATUSES:
                final_decision = decision
                break
            location = _header(response.headers, "location").strip()
            if not location:
                raise SourceDenied("redirect response omitted Location")
            if redirect_count >= self.max_redirects:
                raise SourceDenied("secure fetch redirect limit exceeded")
            current_url = urljoin(current_url, location)
        if response is None or final_decision is None:
            raise SourceDenied("secure fetch did not produce a response")
        if not 200 <= response.status < 300:
            raise SourceDenied(f"secure fetch returned HTTP {response.status}")
        content_encoding = _header(response.headers, "content-encoding").strip().lower()
        if content_encoding not in {"", "identity"}:
            raise SourceDenied("compressed response bodies are not accepted")
        content_type = _header(response.headers, "content-type")
        mime_type = _mime_type(response.headers)
        if mime_type not in self.allowed_mime_types:
            raise SourceDenied(f"response MIME type is not approved: {mime_type or '(missing)'}")
        raw_truncated = len(response.body) > self.max_bytes
        raw_body = response.body[:self.max_bytes]
        text = _decode_body(raw_body, content_type)
        if mime_type in {"text/html", "application/xhtml+xml"}:
            parser = _HtmlTextExtractor()
            parser.feed(text)
            parser.close()
            text = parser.text()
        bounded, normalized_truncated = mask_and_truncate(text, self.max_bytes)
        truncated = raw_truncated or normalized_truncated
        content_hash = hashlib.sha256(bounded.encode("utf-8")).hexdigest()
        provenance = EvidenceProvenance(
            policy_hash=self.policy.policy_hash,
            policy_rule_id=final_decision.rule_id,
            source_classification=final_decision.classification,
            original_url=original_url,
            final_url=current_url,
            retrieved_at=float(self.clock()),
            max_age_hours=final_decision.max_age_hours,
        )
        evidence_id = None
        if self.evidence_store is not None:
            record = self.evidence_store.add_tool_result(
                session_id=session_id,
                tool="web_fetch",
                arguments={"url": original_url},
                result={"status": "ok", "content": bounded},
                category="external-source",
                model_identity=model_identity,
                source=current_url,
                mime_type=mime_type,
                provenance=provenance,
                now=provenance.retrieved_at,
            )
            evidence_id = record.id
        _remaining(deadline, self.monotonic)
        return FetchedEvidence(
            content=bounded,
            content_hash=content_hash,
            mime_type=mime_type,
            truncated=truncated,
            provenance=provenance,
            evidence_id=evidence_id,
        )
