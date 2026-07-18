"""Append-only runtime events and deterministic artifact projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


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
