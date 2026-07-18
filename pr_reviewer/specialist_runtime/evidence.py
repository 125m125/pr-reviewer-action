"""Content-addressed, provenance-backed evidence for specialist sessions."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
import time
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


# The shared redaction implementation intentionally remains the single source
# of secret-masking behavior for tool output and specialist evidence.
_SCRIPTS_DIR = str(Path(__file__).parents[2] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from redact import mask_and_truncate, mask_secrets  # noqa: E402


_SUCCESS_STATUSES = frozenset({"ok", "success", "completed"})


def _is_sensitive_key(key: str) -> bool:
    normalized = "".join(character for character in key.lower() if character.isalnum())
    return any(marker in normalized for marker in (
        "apikey", "token", "password", "secret", "accesskey", "auth",
    ))


def _normalized_path(value: object) -> str:
    path = str(value).strip().replace("\\", "/")
    if not path:
        return ""
    normalized = str(PurePosixPath(path))
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _sanitize_url(value: object) -> tuple[str, bool]:
    parsed = urlsplit(str(value).strip())
    if not parsed.scheme or not parsed.netloc:
        return str(value).strip(), False
    host = parsed.hostname.lower() if parsed.hostname else ""
    netloc = host
    try:
        if parsed.port:
            netloc += f":{parsed.port}"
    except ValueError:
        pass
    path = _normalized_path(parsed.path) if parsed.path else ""
    if parsed.path.startswith("/"):
        path = "/" + path.lstrip("/")
    redacted = bool(parsed.username or parsed.password)
    query_pairs: list[tuple[str, str]] = []
    for key, raw_value in parse_qsl(parsed.query, keep_blank_values=True):
        if _is_sensitive_key(key) and raw_value:
            query_pairs.append((key, "[REDACTED]"))
            redacted = True
            continue
        sanitized, did_redact = _sanitize_value(raw_value, key)
        query_pairs.append((key, str(sanitized)))
        redacted = redacted or did_redact
    query = urlencode(sorted(query_pairs))
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, "")), redacted


def _normalized_url(value: object) -> str:
    return _sanitize_url(value)[0]


def _sanitize_value(value: Any, key: str = "") -> tuple[Any, bool]:
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        redacted = False
        for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0])):
            normalized_key = str(item_key)
            sanitized_value, did_redact = _sanitize_value(item_value, normalized_key)
            sanitized[normalized_key] = sanitized_value
            redacted = redacted or did_redact
        return sanitized, redacted
    if isinstance(value, (list, tuple)):
        sanitized_items = [_sanitize_value(item) for item in value]
        return [item for item, _ in sanitized_items], any(redacted for _, redacted in sanitized_items)
    if isinstance(value, str):
        normalized_key = key.lower()
        if normalized_key in {"path", "file", "repository_path"}:
            normalized = _normalized_path(value)
            masked = mask_secrets(normalized)
            return masked, masked != normalized
        if normalized_key in {"url", "endpoint"} or normalized_key.endswith("_url"):
            return _sanitize_url(value)
        if _is_sensitive_key(key) and value:
            return "[REDACTED]", True
        masked = mask_secrets(value)
        return masked, masked != value
    return value, False


def _normalize_value(value: Any, key: str = "") -> Any:
    return _sanitize_value(value, key)[0]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _normalize_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _result_status(result: Mapping[str, Any]) -> str:
    return str(result.get("status", "unknown")).strip().lower() or "unknown"


def _result_content(result: Mapping[str, Any]) -> str:
    nested = result.get("result")
    if isinstance(nested, Mapping) and "content" in nested:
        content = nested["content"]
    elif "content" in result:
        content = result["content"]
    elif "error" in result:
        content = result["error"]
    elif nested is not None:
        content = nested
    else:
        content = result
    return content if isinstance(content, str) else _canonical_json(content)


def _source_identity(arguments: Mapping[str, Any], source: str | None = None) -> str:
    if source is not None and str(source).strip():
        text = str(source).strip()
        return _normalized_url(text) if "://" in text else _normalized_path(text)
    for key in ("path", "file", "repository_path", "url", "endpoint"):
        value = arguments.get(key)
        if value is not None and str(value).strip():
            normalized = _normalize_value(value, key)
            return f"{key}:{normalized}"
    return ""


def _bounded_content(content: str, max_content_bytes: int) -> tuple[str, bool, bool]:
    if max_content_bytes <= 0:
        raise ValueError("max_content_bytes must be positive")
    redacted_content = mask_secrets(content)
    bounded, truncated = mask_and_truncate(content, max_content_bytes)
    return bounded, redacted_content != content, truncated


@dataclass(frozen=True)
class EvidenceProvenance:
    """Immutable source and policy context needed to judge evidence reuse."""

    head_sha: str | None = None
    policy_hash: str | None = None
    policy_rule_id: str | None = None
    source_classification: str | None = None
    original_url: str | None = None
    final_url: str | None = None
    retrieved_at: float | None = None
    max_age_hours: float | None = None


def _sanitize_provenance(
    provenance: EvidenceProvenance | None,
) -> tuple[EvidenceProvenance, bool]:
    provenance = provenance or EvidenceProvenance()
    if not isinstance(provenance, EvidenceProvenance):
        raise TypeError("provenance must be an EvidenceProvenance value")

    scalar_fields = ("head_sha", "policy_hash", "policy_rule_id", "source_classification")
    sanitized_scalars: dict[str, str | None] = {}
    redacted = False
    for field_name in scalar_fields:
        value = getattr(provenance, field_name)
        if value is None:
            sanitized_scalars[field_name] = None
            continue
        sanitized, did_redact = _sanitize_value(str(value), field_name)
        sanitized_scalars[field_name] = str(sanitized)
        redacted = redacted or did_redact

    urls: dict[str, str | None] = {}
    for field_name in ("original_url", "final_url"):
        value = getattr(provenance, field_name)
        if value is None:
            urls[field_name] = None
            continue
        sanitized, did_redact = _sanitize_url(value)
        urls[field_name] = sanitized
        redacted = redacted or did_redact

    return EvidenceProvenance(
        **sanitized_scalars,
        **urls,
        retrieved_at=provenance.retrieved_at,
        max_age_hours=provenance.max_age_hours,
    ), redacted


def _provenance_identity(provenance: EvidenceProvenance) -> dict[str, object]:
    """Return reuse-relevant provenance without volatile retrieval time."""
    return {
        "head_sha": provenance.head_sha,
        "policy_hash": provenance.policy_hash,
        "policy_rule_id": provenance.policy_rule_id,
        "source_classification": provenance.source_classification,
        "original_url": provenance.original_url,
        "final_url": provenance.final_url,
        "max_age_hours": provenance.max_age_hours,
    }


def canonical_evidence_key(
    tool: str,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    source: str | None = None,
    provenance: EvidenceProvenance | None = None,
    max_content_bytes: int = 64 * 1024,
) -> str:
    """Return a deterministic identity for a bounded, safely stored result."""
    content, _, _ = _bounded_content(_result_content(result), max_content_bytes)
    sanitized_provenance, _ = _sanitize_provenance(provenance)
    identity = {
        "tool": str(tool).strip(),
        "arguments": _normalize_value(arguments),
        "source": _source_identity(arguments, source),
        "status": _result_status(result),
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "provenance": _provenance_identity(sanitized_provenance),
    }
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return f"evidence:{digest}"


@dataclass(frozen=True)
class EvidenceRecord:
    """One retained tool result; only successful records support coverage."""

    id: str
    canonical_key: str
    category: str
    collector_session_id: str
    model_identity: str
    tool: str
    arguments: str
    source_identity: str
    source_path: str | None
    provenance: EvidenceProvenance
    status: str
    content: str
    content_hash: str
    mime_type: str | None
    truncated: bool
    redacted: bool
    imported_by: tuple[str, ...]
    supersedes: tuple[str, ...] = ()
    contradicts: tuple[str, ...] = ()

    @property
    def is_usable_for_coverage(self) -> bool:
        return self.status in _SUCCESS_STATUSES


@dataclass(frozen=True)
class EvidenceSnapshot:
    """A detached evidence view fixed at the beginning of a work wave."""

    records: tuple[EvidenceRecord, ...]

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(record.id for record in self.records)

    def get(self, evidence_id: str) -> EvidenceRecord | None:
        return next((record for record in self.records if record.id == evidence_id), None)

    def get_by_path(self, path: str) -> tuple[EvidenceRecord, ...]:
        normalized = _normalized_path(path)
        return tuple(record for record in self.records if record.source_path == normalized)


class EvidenceStore:
    """Mutable canonical store which creates immutable snapshots on demand."""

    def __init__(self, *, max_content_bytes: int = 64 * 1024) -> None:
        if max_content_bytes <= 0:
            raise ValueError("max_content_bytes must be positive")
        self._max_content_bytes = max_content_bytes
        self._records: dict[str, EvidenceRecord] = {}
        self._successful_canonical: dict[str, str] = {}
        self._failed_attempts = 0
        self._refreshes = 0

    def add_tool_result(
        self,
        *,
        session_id: str,
        tool: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        category: str = "tool-result",
        model_identity: str = "",
        source: str | None = None,
        mime_type: str | None = None,
        provenance: EvidenceProvenance | None = None,
        supersedes: tuple[str, ...] | list[str] = (),
        contradicts: tuple[str, ...] | list[str] = (),
        now: float | None = None,
    ) -> EvidenceRecord:
        return self.add(
            session_id=session_id,
            tool=tool,
            arguments=arguments,
            result=result,
            category=category,
            model_identity=model_identity,
            source=source,
            mime_type=mime_type,
            provenance=provenance,
            supersedes=supersedes,
            contradicts=contradicts,
            now=now,
        )

    def add(
        self,
        *,
        session_id: str,
        tool: str,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
        category: str = "tool-result",
        model_identity: str = "",
        source: str | None = None,
        mime_type: str | None = None,
        provenance: EvidenceProvenance | None = None,
        supersedes: tuple[str, ...] | list[str] = (),
        contradicts: tuple[str, ...] | list[str] = (),
        now: float | None = None,
    ) -> EvidenceRecord:
        """Store a result, reusing only prior successful canonical evidence."""
        session_id = str(session_id).strip()
        if not session_id:
            raise ValueError("session_id must be non-empty")
        if not isinstance(arguments, Mapping) or not isinstance(result, Mapping):
            raise TypeError("arguments and result must be mappings")

        status = _result_status(result)
        sanitized_arguments, arguments_redacted = _sanitize_value(arguments)
        sanitized_provenance, provenance_redacted = _sanitize_provenance(provenance)
        if source is None:
            sanitized_source = None
            source_redacted = False
        elif "://" in str(source):
            sanitized_source, source_redacted = _sanitize_url(source)
        else:
            sanitized_source, source_redacted = _sanitize_value(str(source))
            sanitized_source = str(sanitized_source)
        canonical_supersedes = self._canonical_relationship_ids(supersedes)
        canonical_contradicts = self._canonical_relationship_ids(contradicts)
        content, redacted, truncated = _bounded_content(
            _result_content(result), self._max_content_bytes
        )
        canonical_key = canonical_evidence_key(
            tool, sanitized_arguments, result, source=sanitized_source, provenance=sanitized_provenance,
            max_content_bytes=self._max_content_bytes,
        )
        if status in _SUCCESS_STATUSES and canonical_key in self._successful_canonical:
            evidence_id = self._successful_canonical[canonical_key]
            existing = self._records[evidence_id]
            current_time = time.time() if now is None else float(now)
            if self._is_fresh(existing, current_time):
                imported_by = tuple(sorted(set(existing.imported_by) | {session_id}))
                updated = replace(existing, imported_by=imported_by)
                self._records[evidence_id] = updated
                return updated

        source_identity = _source_identity(sanitized_arguments, sanitized_source)
        source_path = None
        raw_path = sanitized_arguments.get(
            "path", sanitized_arguments.get("file", sanitized_arguments.get("repository_path"))
        )
        if raw_path is not None:
            source_path = _normalized_path(raw_path)
        evidence_id = canonical_key
        if status not in _SUCCESS_STATUSES:
            self._failed_attempts += 1
            evidence_id = f"{canonical_key}:attempt:{self._failed_attempts}"
        elif canonical_key in self._successful_canonical:
            self._refreshes += 1
            evidence_id = f"{canonical_key}:refresh:{self._refreshes}"
        record = EvidenceRecord(
            id=evidence_id,
            canonical_key=canonical_key,
            category=str(category).strip() or "tool-result",
            collector_session_id=session_id,
            model_identity=str(model_identity).strip(),
            tool=str(tool).strip(),
            arguments=_canonical_json(sanitized_arguments),
            source_identity=source_identity,
            source_path=source_path,
            provenance=sanitized_provenance,
            status=status,
            content=content,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            mime_type=str(mime_type).strip() if mime_type else None,
            truncated=truncated,
            redacted=redacted or arguments_redacted or provenance_redacted or source_redacted,
            imported_by=(session_id,),
            supersedes=canonical_supersedes,
            contradicts=canonical_contradicts,
        )
        self._records[record.id] = record
        if record.is_usable_for_coverage:
            self._successful_canonical[canonical_key] = record.id
        return record

    @staticmethod
    def _is_fresh(record: EvidenceRecord, now: float) -> bool:
        provenance = record.provenance
        if provenance.max_age_hours is None:
            return True
        if provenance.retrieved_at is None:
            return False
        return now < provenance.retrieved_at + provenance.max_age_hours * 3600

    def _canonical_relationship_ids(self, evidence_ids: tuple[str, ...] | list[str]) -> tuple[str, ...]:
        if isinstance(evidence_ids, str):
            raise ValueError("evidence relationships must be a sequence of known record IDs")
        canonical_ids: list[str] = []
        for evidence_id in evidence_ids:
            if not isinstance(evidence_id, str) or not evidence_id.strip():
                raise ValueError("evidence relationships must be non-empty known record IDs")
            record = self._records.get(evidence_id)
            if record is None:
                raise ValueError("evidence relationship must reference a known record")
            canonical_ids.append(record.canonical_key)
        return tuple(canonical_ids)

    def import_into_session(
        self, session_id: str, evidence_id: str, *, now: float | None = None,
    ) -> EvidenceRecord:
        """Record evidence reuse while retaining the original collector."""
        session_id = str(session_id).strip()
        if not session_id:
            raise ValueError("session_id must be non-empty")
        record = self._records[evidence_id]
        current_time = time.time() if now is None else float(now)
        if not self._is_fresh(record, current_time):
            raise ValueError("evidence is not reusable because it is stale or lacks a retrieval timestamp")
        imported_by = tuple(sorted(set(record.imported_by) | {session_id}))
        updated = replace(record, imported_by=imported_by)
        self._records[evidence_id] = updated
        return updated

    def lookup_canonical(
        self, canonical_key: str, *, now: float | None = None,
    ) -> EvidenceRecord | None:
        """Find reusable successful evidence by canonical key or evidence ID."""
        evidence_id = self._successful_canonical.get(canonical_key, canonical_key)
        record = self._records.get(evidence_id)
        current_time = time.time() if now is None else float(now)
        if not record or not record.is_usable_for_coverage or not self._is_fresh(record, current_time):
            return None
        return record

    def snapshot(self) -> EvidenceSnapshot:
        """Freeze the store's current records for a later wave's stable view."""
        return EvidenceSnapshot(tuple(self._records[key] for key in sorted(self._records)))
