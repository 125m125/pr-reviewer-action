"""Thread-safe request-attempt accounting closed at deterministic wave cutoffs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import isfinite
from threading import Lock
import time
from typing import Callable


@dataclass(frozen=True)
class RequestAttempt:
    sequence: int
    request_id: str
    session_id: str
    assignment_id: str
    phase: str
    turn: int
    input_tokens: int
    max_output_tokens: int
    started_at: float
    status: str = "started"
    terminal_at: float | None = None
    in_flight: bool = False


class RequestAttemptJournal:
    """Own request transitions and freeze open attempts exactly once at cutoff."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        transition_sink: Callable[[RequestAttempt], None] | None = None,
    ) -> None:
        self._clock = clock
        self._transition_sink = transition_sink
        self._lock = Lock()
        self._records: dict[str, RequestAttempt] = {}
        self._sequence = 0

    def _now(self) -> float:
        try:
            value = float(self._clock())
            return value if isfinite(value) else 0.0
        except BaseException:
            return 0.0

    def cursor(self) -> int:
        with self._lock:
            return self._sequence

    def start(
        self,
        *,
        request_id: str,
        session_id: str,
        assignment_id: str,
        phase: str,
        turn: int,
        input_tokens: int,
        max_output_tokens: int,
    ) -> RequestAttempt:
        with self._lock:
            if request_id in self._records:
                raise ValueError(f"duplicate request attempt ID {request_id}")
            self._sequence += 1
            attempt = RequestAttempt(
                sequence=self._sequence,
                request_id=request_id,
                session_id=session_id,
                assignment_id=assignment_id,
                phase=str(phase),
                turn=turn,
                input_tokens=max(0, int(input_tokens)),
                max_output_tokens=max(0, int(max_output_tokens)),
                started_at=self._now(),
            )
            self._records[request_id] = attempt
        if self._transition_sink is not None:
            self._transition_sink(attempt)
        return attempt

    def finish(self, request_id: str, status: str) -> bool:
        if status not in {"completed", "failed", "timed_out"}:
            raise ValueError("invalid request terminal status")
        with self._lock:
            attempt = self._records.get(request_id)
            if attempt is None or attempt.status != "started":
                return False
            updated = replace(
                attempt,
                status=status,
                terminal_at=self._now(),
                in_flight=False,
            )
            self._records[request_id] = updated
        if self._transition_sink is not None:
            self._transition_sink(updated)
        return True

    def close_since(self, cursor: int) -> tuple[RequestAttempt, ...]:
        with self._lock:
            now = self._now()
            selected = []
            transitioned = []
            for request_id, attempt in tuple(self._records.items()):
                if attempt.sequence <= cursor:
                    continue
                if attempt.status == "started":
                    attempt = replace(
                        attempt,
                        status="timed_out_at_phase_cutoff",
                        terminal_at=now,
                        in_flight=True,
                    )
                    self._records[request_id] = attempt
                    transitioned.append(attempt)
                selected.append(attempt)
            selected = tuple(sorted(selected, key=lambda item: item.sequence))
        if self._transition_sink is not None:
            for attempt in transitioned:
                self._transition_sink(attempt)
        return selected
