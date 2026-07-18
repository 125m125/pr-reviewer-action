"""Append-only runtime events and deterministic artifact projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping


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
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


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
            payload = dict(event.payload)
            projected_events.append(
                {"sequence": event.sequence, "kind": event.kind, "payload": payload}
            )
            if event.kind == "run_started":
                artifact.update(payload)
            elif event.kind == "phase_changed":
                artifact["phase"] = payload.get("phase")
        return artifact
