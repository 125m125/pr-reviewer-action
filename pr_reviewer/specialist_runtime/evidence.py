"""Content-addressed, provenance-backed evidence for specialist sessions."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path, PurePosixPath
import sys
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


# The shared redaction implementation intentionally remains the single source
# of secret-masking behavior for tool output and specialist evidence.
_SCRIPTS_DIR = str(Path(__file__).parents[2] / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from redact import mask_and_truncate, mask_secrets  # noqa: E402


_SUCCESS_STATUSES = frozenset({"ok", "success", "completed"})


def _normalized_path(value: object) -> str:
    path = str(value).strip().replace("\\", "/")
    if not path:
        return ""
    normalized = str(PurePosixPath(path))
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _normalized_url(value: object) -> str:
    parsed = urlsplit(str(value).strip())
    if not parsed.scheme or not parsed.netloc:
        return str(value).strip()
    host = parsed.hostname.lower() if parsed.hostname else ""
    netloc = host
    if parsed.port:
        netloc += f":{parsed.port}"
    if parsed.username:
        netloc = f"{parsed.username}@{netloc}"
    path = _normalized_path(parsed.path) if parsed.path else ""
    if parsed.path.startswith("/"):
        path = "/" + path.lstrip("/")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))


def _normalize_value(value: Any, key: str = "") -> Any:
    if isinstance(value, Mapping):
        return {str(item_key): _normalize_value(item_value, str(item_key))
                for item_key, item_value in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    if isinstance(value, str):
        if key in {"path", "file", "repository_path"}:
            return _normalized_path(value)
        if key in {"url", "endpoint"}:
            return _normalized_url(value)
    return value


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


def canonical_evidence_key(
    tool: str,
    arguments: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    source: str | None = None,
    max_content_bytes: int = 64 * 1024,
) -> str:
    """Return a deterministic identity for a bounded, safely stored result."""
    content, _, _ = _bounded_content(_result_content(result), max_content_bytes)
    identity = {
        "tool": str(tool).strip(),
        "arguments": _normalize_value(arguments),
        "source": _source_identity(arguments, source),
        "status": _result_status(result),
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
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
    status: str
    content: str
    content_hash: str
    mime_type: str | None
    truncated: bool
    redacted: bool
    imported_by: tuple[str, ...]

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
    ) -> EvidenceRecord:
        """Store a result, reusing only prior successful canonical evidence."""
        session_id = str(session_id).strip()
        if not session_id:
            raise ValueError("session_id must be non-empty")
        if not isinstance(arguments, Mapping) or not isinstance(result, Mapping):
            raise TypeError("arguments and result must be mappings")

        status = _result_status(result)
        content, redacted, truncated = _bounded_content(
            _result_content(result), self._max_content_bytes
        )
        canonical_key = canonical_evidence_key(
            tool, arguments, result, source=source, max_content_bytes=self._max_content_bytes
        )
        if status in _SUCCESS_STATUSES and canonical_key in self._successful_canonical:
            evidence_id = self._successful_canonical[canonical_key]
            existing = self._records[evidence_id]
            imported_by = tuple(sorted(set(existing.imported_by) | {session_id}))
            updated = replace(existing, imported_by=imported_by)
            self._records[evidence_id] = updated
            return updated

        source_identity = _source_identity(arguments, source)
        source_path = None
        raw_path = arguments.get("path", arguments.get("file", arguments.get("repository_path")))
        if raw_path is not None:
            source_path = _normalized_path(raw_path)
        evidence_id = canonical_key
        if status not in _SUCCESS_STATUSES:
            self._failed_attempts += 1
            evidence_id = f"{canonical_key}:attempt:{self._failed_attempts}"
        record = EvidenceRecord(
            id=evidence_id,
            canonical_key=canonical_key,
            category=str(category).strip() or "tool-result",
            collector_session_id=session_id,
            model_identity=str(model_identity).strip(),
            tool=str(tool).strip(),
            arguments=_canonical_json(arguments),
            source_identity=source_identity,
            source_path=source_path,
            status=status,
            content=content,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            mime_type=str(mime_type).strip() if mime_type else None,
            truncated=truncated,
            redacted=redacted,
            imported_by=(session_id,),
        )
        self._records[record.id] = record
        if record.is_usable_for_coverage:
            self._successful_canonical[canonical_key] = record.id
        return record

    def import_into_session(self, session_id: str, evidence_id: str) -> EvidenceRecord:
        """Record evidence reuse while retaining the original collector."""
        session_id = str(session_id).strip()
        if not session_id:
            raise ValueError("session_id must be non-empty")
        record = self._records[evidence_id]
        imported_by = tuple(sorted(set(record.imported_by) | {session_id}))
        updated = replace(record, imported_by=imported_by)
        self._records[evidence_id] = updated
        return updated

    def lookup_canonical(self, canonical_key: str) -> EvidenceRecord | None:
        """Find reusable successful evidence by canonical key or evidence ID."""
        evidence_id = self._successful_canonical.get(canonical_key, canonical_key)
        record = self._records.get(evidence_id)
        return record if record and record.is_usable_for_coverage else None

    def snapshot(self) -> EvidenceSnapshot:
        """Freeze the store's current records for a later wave's stable view."""
        return EvidenceSnapshot(tuple(self._records[key] for key in sorted(self._records)))
