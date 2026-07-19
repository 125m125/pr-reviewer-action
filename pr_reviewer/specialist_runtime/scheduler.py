"""Deadline-aware deterministic scheduling for specialist work waves."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
import time
from typing import Protocol

from .budget import RunDeadline, SessionLease
from .coverage import CoverageSnapshot
from .evidence import EvidenceSnapshot
from .session import SessionResult
from .types import ObligationStatus, RunPhase


_PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
_STATUS_RANK = {
    ObligationStatus.COVERED: 3,
    ObligationStatus.UNRESOLVED: 2,
    ObligationStatus.PENDING: 1,
}


@dataclass(frozen=True)
class WaveSnapshot:
    """The exact immutable evidence and coverage view shared by one wave."""

    evidence: EvidenceSnapshot
    coverage: CoverageSnapshot


class ScheduledSession(Protocol):
    def explore(self) -> SessionResult: ...


SessionFactory = Callable[[object, SessionLease, WaveSnapshot], ScheduledSession]
EventSink = Callable[[str, Mapping[str, object]], None]


@dataclass(frozen=True)
class AssignmentResult:
    """A session result retained with its stable assignment identity."""

    assignment_id: str
    session_result: SessionResult


@dataclass(frozen=True)
class WaveFailure:
    assignment_id: str
    error: str


@dataclass(frozen=True)
class WaveResult:
    """Deterministic projection of a completed or deadline-truncated wave."""

    phase: RunPhase
    results: tuple[AssignmentResult, ...]
    failures: tuple[WaveFailure, ...]
    not_started: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    coverage_projection: tuple[tuple[str, ObligationStatus], ...]


def _assignment_id(assignment: object) -> str:
    value = getattr(assignment, "id", None)
    if value is None:
        value = getattr(assignment, "assignment_id", None)
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("assignment must have a non-empty id")
    return normalized


def _assignment_order(assignment: object) -> tuple[int, str]:
    priority = getattr(assignment, "priority", "normal")
    if isinstance(priority, str):
        rank = _PRIORITY_RANK.get(priority.strip().lower(), _PRIORITY_RANK["normal"])
    elif isinstance(priority, int) and not isinstance(priority, bool):
        rank = priority
    else:
        rank = _PRIORITY_RANK["normal"]
    return rank, _assignment_id(assignment)


def _stronger_status(
    current: ObligationStatus | None,
    candidate: ObligationStatus,
) -> ObligationStatus:
    if current is None:
        return candidate
    # The value tie-break makes the projection commutative even for lifecycle
    # statuses that specialist checkpoints normally do not emit.
    return max(
        (current, candidate),
        key=lambda status: (_STATUS_RANK.get(status, 0), status.value),
    )


class SessionScheduler:
    """Run isolated specialist sessions without completion-order side effects."""

    def __init__(
        self,
        *,
        deadline: RunDeadline,
        session_factory: SessionFactory,
        wave_snapshot: WaveSnapshot,
        concurrency: int = 1,
        event_sink: EventSink | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency <= 0:
            raise ValueError("concurrency must be a positive integer")
        if not isinstance(wave_snapshot, WaveSnapshot):
            raise TypeError("wave_snapshot must be a WaveSnapshot")
        self.deadline = deadline
        self.session_factory = session_factory
        self.wave_snapshot = wave_snapshot
        self.concurrency = concurrency
        self.event_sink = event_sink
        self.clock = clock

    def _emit(self, kind: str, payload: Mapping[str, object]) -> None:
        if self.event_sink is not None:
            self.event_sink(kind, payload)

    def _can_launch(self, lease: SessionLease) -> bool:
        now = self.clock()
        return lease.active(now=now) and self.deadline.exploration_allowed(now=now)

    def _run_assignment(self, assignment: object, lease: SessionLease) -> SessionResult:
        # Factory construction belongs inside the worker so each worker owns all
        # mutable session state. Only the frozen wave snapshot crosses workers.
        session = self.session_factory(assignment, lease, self.wave_snapshot)
        return session.explore()

    def run_wave(
        self,
        assignments: Iterable[object],
        phase: RunPhase = RunPhase.INITIAL,
    ) -> WaveResult:
        normalized_phase = RunPhase(phase)
        ordered = tuple(sorted(tuple(assignments), key=_assignment_order))
        ordered_ids = tuple(_assignment_id(item) for item in ordered)
        if len(set(ordered_ids)) != len(ordered_ids):
            raise ValueError("assignment IDs must be unique within a wave")
        lease = self.deadline.lease_for(normalized_phase)
        self._emit("wave_started", {
            "phase": normalized_phase.value,
            "assignment_ids": ordered_ids,
            "concurrency": self.concurrency,
        })

        remaining = deque(ordered)
        active: dict[Future[SessionResult], object] = {}
        completed: dict[str, SessionResult] = {}
        failed: dict[str, str] = {}

        executor = ThreadPoolExecutor(max_workers=self.concurrency)
        try:
            while remaining and len(active) < self.concurrency and self._can_launch(lease):
                assignment = remaining.popleft()
                assignment_id = _assignment_id(assignment)
                self._emit("session_started", {"assignment_id": assignment_id})
                active[executor.submit(self._run_assignment, assignment, lease)] = assignment

            cutoff_reached = not self._can_launch(lease)
            while active:
                if cutoff_reached:
                    # A Future may be queued briefly even though at most one is
                    # submitted per worker. Cancel only work that has not begun;
                    # running sessions retain their bounded phase lease.
                    for future in tuple(active):
                        if future.cancel():
                            active.pop(future)
                    if not active:
                        break

                timeout = None if cutoff_reached else lease.remaining(now=self.clock())
                done, _ = wait(active, timeout=timeout, return_when=FIRST_COMPLETED)
                if not done:
                    cutoff_reached = True
                    continue
                for future in done:
                    assignment = active.pop(future)
                    assignment_id = _assignment_id(assignment)
                    try:
                        completed[assignment_id] = future.result()
                    except Exception as exc:  # noqa: BLE001 - isolate one specialist failure
                        failed[assignment_id] = f"{type(exc).__name__}: {exc}"

                if not self._can_launch(lease):
                    cutoff_reached = True
                while (
                    not cutoff_reached
                    and remaining
                    and len(active) < self.concurrency
                    and self._can_launch(lease)
                ):
                    assignment = remaining.popleft()
                    assignment_id = _assignment_id(assignment)
                    self._emit("session_started", {"assignment_id": assignment_id})
                    active[executor.submit(self._run_assignment, assignment, lease)] = assignment
        finally:
            executor.shutdown(wait=True, cancel_futures=False)

        stable_results = tuple(
            AssignmentResult(assignment_id, completed[assignment_id])
            for assignment_id in ordered_ids
            if assignment_id in completed
        )
        stable_failures = tuple(
            WaveFailure(assignment_id, failed[assignment_id])
            for assignment_id in ordered_ids
            if assignment_id in failed
        )
        not_started = tuple(
            assignment_id for assignment_id in ordered_ids
            if assignment_id not in completed and assignment_id not in failed
        )

        evidence_ids = set(self.wave_snapshot.evidence.evidence_ids)
        coverage = dict(self.wave_snapshot.coverage.obligation_statuses)
        for item in stable_results:
            checkpoint = item.session_result.checkpoint
            evidence_ids.update(checkpoint.evidence_ids)
            for obligation_id, status in checkpoint.obligation_statuses:
                coverage[obligation_id] = _stronger_status(
                    coverage.get(obligation_id), ObligationStatus(status)
                )

        for item in stable_results:
            self._emit("session_completed", {"assignment_id": item.assignment_id})
        for item in stable_failures:
            self._emit("session_failed", {
                "assignment_id": item.assignment_id,
                "error": item.error,
            })
        self._emit("wave_completed", {
            "phase": normalized_phase.value,
            "completed": tuple(item.assignment_id for item in stable_results),
            "failed": tuple(item.assignment_id for item in stable_failures),
            "not_started": not_started,
        })
        return WaveResult(
            phase=normalized_phase,
            results=stable_results,
            failures=stable_failures,
            not_started=not_started,
            evidence_ids=tuple(sorted(evidence_ids)),
            coverage_projection=tuple(sorted(coverage.items())),
        )
