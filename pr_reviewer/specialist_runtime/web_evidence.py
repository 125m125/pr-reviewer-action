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
import json
import math
from pathlib import Path
import re
import socket
import ssl
import sys
import time
from typing import Callable, Iterable, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, unquote, urljoin, urlsplit, urlunsplit
import urllib.request

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
        opener: Callable[..., object] | None = None,
        max_response_bytes: int = 1024 * 1024,
    ) -> None:
        parsed = urlsplit(str(endpoint).strip())
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise SourceDenied("search provider endpoint must be a fixed credential-free HTTPS URL")
        if request_timeout <= 0 or max_response_bytes <= 0:
            raise ValueError("search provider limits must be positive")
        self.endpoint = str(endpoint).strip()
        self.request_timeout = request_timeout
        self.opener = opener or urllib.request.urlopen
        self.max_response_bytes = max_response_bytes

    def search(self, query: str, *, limit: int) -> Sequence[SearchCandidate]:
        if limit <= 0:
            return ()
        separator = "&" if urlsplit(self.endpoint).query else "?"
        url = self.endpoint + separator + urlencode({"q": query, "format": "json"})
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "ai-pr-reviewer/search-discovery",
            },
        )
        with self.opener(request, timeout=self.request_timeout) as response:
            raw = response.read(self.max_response_bytes + 1)
        if len(raw) > self.max_response_bytes:
            raise SourceDenied("search provider response exceeded maximum size")
        payload = json.loads(raw.decode("utf-8", errors="replace"))
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

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": "source_access_request",
            "host": self.host,
            "candidate_url": self.candidate_url,
            "obligation_id": self.obligation_id,
            "purpose": self.purpose,
            "authority_reason": self.authority_reason,
        }


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
        path, path_error = _safe_path(parsed.path)
        if parsed.scheme.lower() != "https":
            return SourceDecision(False, host, path, reason="HTTPS is required")
        if not host:
            return SourceDecision(False, host, path, reason="URL requires a host")
        if parsed.username is not None or parsed.password is not None:
            return SourceDecision(False, host, path, reason="URL credentials are forbidden")
        if port not in (None, 443):
            return SourceDecision(False, host, path, reason="port is not approved")
        if path_error:
            return SourceDecision(False, host, path, reason=path_error)
        query_payload = parsed.query
        try:
            for _ in range(3):
                decoded_query = unquote(query_payload, errors="strict")
                query_payload = decoded_query
                if "%" not in query_payload:
                    break
        except (UnicodeDecodeError, ValueError):
            return SourceDecision(False, host, path, reason="unsafe URL query encoding")
        if any(ord(character) < 32 or ord(character) == 127 for character in query_payload):
            return SourceDecision(False, host, path, reason="unsafe URL query")
        payload = f"{path}?{query_payload}"
        if mask_secrets(payload) != payload or _CREDENTIAL_QUERY_RE.search(parsed.query):
            return SourceDecision(False, host, path, reason="unsafe credential-like URL payload")
        if any(_looks_high_entropy(token) for token in _TOKEN_RE.findall(payload)):
            return SourceDecision(False, host, path, reason="unsafe high-entropy URL payload")
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
            )
        return SourceDecision(False, host, path, reason="source is not approved by policy")


def _normalize_hostname(host: str | None) -> str:
    if not host:
        return ""
    try:
        return host.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return ""


def _safe_path(raw_path: str) -> tuple[str, str | None]:
    try:
        path = raw_path or "/"
        for _ in range(3):
            decoded = unquote(path, errors="strict")
            path = decoded
            if "\\" in path or any(ord(character) < 32 or ord(character) == 127 for character in path):
                return path, "unsafe URL path"
            if any(segment in {".", ".."} for segment in path.split("/")):
                return path, "URL path traversal is forbidden"
            if decoded == path and "%" not in path:
                break
    except (UnicodeDecodeError, ValueError):
        return "/", "invalid URL path encoding"
    return path if path.startswith("/") else "/" + path, None


def _safe_discovery_url(url: str) -> str:
    """Remove credentials, fragments, and secret-like query values."""
    try:
        parsed = urlsplit(str(url).strip())
        host = _normalize_hostname(parsed.hostname)
        port = parsed.port
    except ValueError:
        return ""
    if not host:
        return ""
    netloc = host + (f":{port}" if port else "")
    query = urlencode([
        (mask_secrets(key)[:100], mask_secrets(value)[:300])
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)[:20]
    ])
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", query, ""))


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
    # Requiring mixed case avoids treating ordinary long documentation slugs
    # or hexadecimal commit IDs as credentials merely because they are long.
    token_like_alphabet = (
        bool(re.search(r"[a-z]", token))
        and bool(re.search(r"[A-Z]", token))
        and bool(re.search(r"[0-9+/=_-]", token))
    )
    return token_like_alphabet and _entropy(token) >= 4.0


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
        safe_url = _safe_discovery_url(candidate.url)
        decision = source_policy.classify(safe_url)
        if decision.approved:
            title, _ = mask_and_truncate(str(candidate.title or ""), 300)
            snippet, _ = mask_and_truncate(str(candidate.snippet or ""), 500)
            normalized = replace(
                candidate,
                title=title,
                url=safe_url,
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
            unapproved.append(SearchCandidate(
                title=None,
                url=safe_url,
                snippet=None,
                host=decision.host,
                path=decision.path,
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
) -> SourceAccessRequest:
    if not candidate.host:
        raise ValueError("source access candidate requires a host")
    if not str(obligation_id).strip() or not str(purpose).strip():
        raise ValueError("source access request requires obligation_id and purpose")
    return SourceAccessRequest(
        host=candidate.host,
        candidate_url=candidate.url,
        obligation_id=mask_secrets(str(obligation_id).strip())[:160],
        purpose=mask_secrets(str(purpose).strip())[:1000],
        authority_reason=mask_secrets(str(authority_reason).strip())[:1000],
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


def _default_resolver(host: str, port: int) -> list[str]:
    addresses = []
    for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        address = item[4][0]
        if address not in addresses:
            addresses.append(address)
    return addresses


def _public_addresses(
    host: str, port: int, resolver: Callable[[str, int], Sequence[str]],
) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            raw_addresses = resolver(host, port)
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

    def __init__(self, *, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl_context or ssl.create_default_context()

    def request(self, request: HttpRequest) -> HttpResponse:
        parsed = urlsplit(request.url)
        port = parsed.port or 443
        connection = _PinnedHTTPSConnection(
            parsed.hostname or "", port, request.resolved_ip,
            request.timeout, self._ssl_context,
        )
        target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        try:
            connection.request("GET", target, headers=dict(request.headers))
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            body = response.read(request.max_bytes + 1)
            return HttpResponse(response.status, headers, body)
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
    ) -> None:
        if timeout <= 0 or max_bytes <= 0 or max_redirects < 0:
            raise ValueError("secure fetch limits are invalid")
        self.policy = policy
        self.transport = transport or StdlibHttpTransport()
        self.resolver = resolver or _default_resolver
        self.evidence_store = evidence_store
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        self.allowed_mime_types = frozenset(
            str(item).strip().lower() for item in allowed_mime_types
        )
        self.clock = clock

    def fetch(
        self,
        url: str,
        *,
        session_id: str = "web-fetch",
        model_identity: str = "",
    ) -> FetchedEvidence:
        original_url = str(url).strip()
        current_url = original_url
        final_decision: SourceDecision | None = None
        response: HttpResponse | None = None
        for redirect_count in range(self.max_redirects + 1):
            decision = self.policy.classify(current_url)
            if not decision.approved:
                boundary = "redirect" if redirect_count else "source"
                raise SourceDenied(f"{boundary} denied: {decision.reason}")
            parsed = urlsplit(current_url)
            port = parsed.port or 443
            addresses = _public_addresses(decision.host, port, self.resolver)
            request = HttpRequest(
                url=current_url,
                resolved_ip=addresses[0],
                timeout=self.timeout,
                max_bytes=self.max_bytes,
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
        return FetchedEvidence(
            content=bounded,
            content_hash=content_hash,
            mime_type=mime_type,
            truncated=truncated,
            provenance=provenance,
            evidence_id=evidence_id,
        )
