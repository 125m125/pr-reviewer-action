"""Append-only runtime events and deterministic artifact projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from types import MappingProxyType
from typing import Callable, Mapping

from scripts.redact import mask_secrets


_MAX_EVENT_STRING = 1000
_MAX_EVENT_ITEMS = 100
_SENSITIVE_KEYS = frozenset({
    "api_key", "authorization", "cookie", "password", "secret", "token",
})


def _freeze_json(value: object) -> object:
    """Take a deterministic immutable snapshot of JSON-like payload data."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("event payload object keys must be strings")
        return MappingProxyType(
            {key: _freeze_json(value[key]) for key in sorted(value)}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError("event payload values must be JSON-like")


def _clone_json(value: object) -> object:
    """Return a detached mutable JSON-like projection of a frozen payload."""
    if isinstance(value, Mapping):
        return {key: _clone_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_clone_json(item) for item in value]
    return value


def _bounded_json(value: object, *, key: str = "", depth: int = 0) -> object:
    """Redact and bound untrusted event values before they enter the journal."""
    if depth > 8:
        return "[bounded]"
    if key.strip().lower() in _SENSITIVE_KEYS:
        return "[REDACTED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return mask_secrets(value)[:_MAX_EVENT_STRING]
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return _bounded_json(getattr(value, "value"), key=key, depth=depth + 1)
    if isinstance(value, Mapping):
        if any(not isinstance(item, str) for item in value):
            raise TypeError("event payload object keys must be strings")
        result: dict[str, object] = {}
        for raw_key in sorted(value)[:_MAX_EVENT_ITEMS]:
            result[raw_key] = _bounded_json(
                value[raw_key], key=raw_key, depth=depth + 1,
            )
        return result
    if isinstance(value, (set, frozenset)):
        value = tuple(sorted(value, key=str))
    if isinstance(value, (list, tuple)):
        values = tuple(value)
        return [
            _bounded_json(item, depth=depth + 1)
            for item in values[:_MAX_EVENT_ITEMS]
        ]
    return mask_secrets(str(value))[:_MAX_EVENT_STRING]


@dataclass(frozen=True)
class RunEvent:
    sequence: int
    kind: str
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or self.sequence <= 0:
            raise ValueError("event sequence must be a positive integer")
        if not self.kind:
            raise ValueError("event kind must be non-empty")
        frozen_payload = _freeze_json(self.payload)
        if not isinstance(frozen_payload, Mapping):
            raise TypeError("event payload must be a JSON-like object")
        object.__setattr__(self, "payload", frozen_payload)


class EventJournal:
    """Thread-safe append-only owner of stable, bounded runtime events."""

    def __init__(
        self,
        external_sink: Callable[[RunEvent], None] | None = None,
    ) -> None:
        self._events: list[RunEvent] = []
        self._lock = Lock()
        self._external_sink = external_sink
        self._external_errors: list[str] = []

    def emit(self, kind: str, payload: Mapping[str, object] | None = None) -> RunEvent:
        bounded = _bounded_json(dict(payload or {}))
        if not isinstance(bounded, Mapping):
            raise TypeError("event payload must be an object")
        with self._lock:
            event = RunEvent(
                sequence=len(self._events) + 1,
                kind=str(kind).strip(),
                payload=bounded,
            )
            self._events.append(event)
        if self._external_sink is not None:
            try:
                self._external_sink(event)
            except Exception as exc:  # noqa: BLE001 - observation cannot abort a run
                with self._lock:
                    self._external_errors.append(
                        mask_secrets(f"{type(exc).__name__}: {exc}")[:_MAX_EVENT_STRING]
                    )
        return event

    def snapshot(self) -> tuple[RunEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def external_errors(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._external_errors)


class RunArtifactProjector:
    """Project a contiguous event stream into a stable machine artifact."""

    def project(self, events: list[RunEvent] | tuple[RunEvent, ...]) -> dict[str, object]:
        ordered = sorted(events, key=lambda event: event.sequence)
        expected_sequence = 1
        artifact: dict[str, object] = {"events": []}
        projected_events: list[dict[str, object]] = artifact["events"]  # type: ignore[assignment]
        for event in ordered:
            if event.sequence < expected_sequence:
                raise ValueError(f"duplicate event sequence {event.sequence}")
            if event.sequence > expected_sequence:
                raise ValueError(
                    f"missing event sequence {expected_sequence} before {event.sequence}"
                )
            expected_sequence += 1
            payload = _clone_json(event.payload)
            if not isinstance(payload, dict):
                raise TypeError("event payload must be a JSON-like object")
            projected_events.append(
                {"sequence": event.sequence, "kind": event.kind, "payload": payload}
            )
            if event.kind == "run_started":
                artifact.update(_clone_json(event.payload))
            elif event.kind == "phase_changed":
                artifact["phase"] = _clone_json(event.payload.get("phase"))
        return artifact
