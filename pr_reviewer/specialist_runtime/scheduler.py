"""Deadline-aware deterministic scheduling for specialist work waves."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Lock, Thread
import time
from typing import Protocol

from .budget import RunDeadline, SessionLease
from .coverage import CoverageSnapshot
from .evidence import EvidenceSnapshot
from .session import SessionResult
from .types import ObligationStatus, RunPhase


_PRIORITY_RANK = {"critical": 0, "high": 1, "normal": 2, "low": 3}
_EXPLORATION_PHASES = frozenset({RunPhase.INITIAL, RunPhase.FOLLOWUP})
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
    session: ScheduledSession | None = None


@dataclass(frozen=True)
class _CompletedSession:
    session: ScheduledSession
    result: SessionResult


@dataclass(frozen=True)
class _FailedSession:
    session: ScheduledSession
    error: str


@dataclass(frozen=True)
class WaveFailure:
    assignment_id: str
    error: str
    session: ScheduledSession | None = None


@dataclass(frozen=True)
class WaveResult:
    """Deterministic projection of a completed or deadline-truncated wave."""

    phase: RunPhase
    results: tuple[AssignmentResult, ...]
    failures: tuple[WaveFailure, ...]
    not_started: tuple[str, ...]
    cancelled_assignment_ids: tuple[str, ...]
    in_flight_assignment_ids: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    coverage_projection: tuple[tuple[str, ObligationStatus], ...]


@dataclass(frozen=True)
class _WorkerDeclined:
    """A submitted worker observed cutoff before constructing its session."""


class _DaemonExecutor:
    """Small bounded executor whose abandoned workers cannot hold process exit."""

    def __init__(self, max_workers: int) -> None:
        self._queue: Queue[tuple[Future[object], Callable[..., object], tuple[object, ...]] | None] = Queue()
        self._lock = Lock()
        self._shutdown = False
        self._threads = tuple(
            Thread(
                target=self._worker,
                name=f"ThreadPoolExecutor-daemon-{index}",
                daemon=True,
            )
            for index in range(max_workers)
        )
        for thread in self._threads:
            thread.start()

    def submit(self, callback: Callable[..., object], *args: object) -> Future[object]:
        future: Future[object] = Future()
        with self._lock:
            if self._shutdown:
                raise RuntimeError("executor is shut down")
            self._queue.put((future, callback, args))
        return future

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            future, callback, args = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = callback(*args)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        with self._lock:
            self._shutdown = True
            if cancel_futures:
                while True:
                    try:
                        item = self._queue.get_nowait()
                    except Empty:
                        break
                    if item is not None:
                        item[0].cancel()
            for _ in self._threads:
                self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join()


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

    def _run_assignment(
        self, assignment: object, lease: SessionLease,
    ) -> _CompletedSession | _FailedSession | _WorkerDeclined:
        # Factory construction belongs inside the worker so each worker owns all
        # mutable session state. Only the frozen wave snapshot crosses workers.
        if not self._can_launch(lease):
            return _WorkerDeclined()
        session = self.session_factory(assignment, lease, self.wave_snapshot)
        if not self._can_launch(lease):
            return _WorkerDeclined()
        try:
            result = session.explore()
        except BaseException as exc:
            try:
                error = f"{type(exc).__name__}: {exc}"
            except BaseException:
                error = f"{type(exc).__name__}: [unprintable error]"
            return _FailedSession(session=session, error=error[:1000])
        return _CompletedSession(session=session, result=result)

    def run_wave(
        self,
        assignments: Iterable[object],
        phase: RunPhase = RunPhase.INITIAL,
    ) -> WaveResult:
        normalized_phase = RunPhase(phase)
        if normalized_phase not in _EXPLORATION_PHASES:
            raise ValueError("scheduler waves require initial or followup phase")
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
        active: dict[
            Future[_CompletedSession | _FailedSession | _WorkerDeclined], object
        ] = {}
        completed: dict[str, _CompletedSession] = {}
        failed: dict[str, str] = {}
        failed_sessions: dict[str, ScheduledSession] = {}
        declined: set[str] = set()
        cancelled: set[str] = set()
        in_flight: set[str] = set()

        def collect(
            future: Future[_CompletedSession | _FailedSession | _WorkerDeclined],
        ) -> None:
            assignment = active.pop(future)
            assignment_id = _assignment_id(assignment)
            if future.cancelled():
                cancelled.add(assignment_id)
                return
            try:
                outcome = future.result()
            except BaseException as exc:  # isolate every specialist failure
                failed[assignment_id] = f"{type(exc).__name__}: {exc}"
                return
            if isinstance(outcome, _WorkerDeclined):
                declined.add(assignment_id)
            elif isinstance(outcome, _FailedSession):
                failed[assignment_id] = outcome.error
                failed_sessions[assignment_id] = outcome.session
            else:
                completed[assignment_id] = outcome

        executor = _DaemonExecutor(self.concurrency)
        cutoff_reached = False
        try:
            # Admission is stable and may queue more work than the executor can
            # immediately run. Every queued worker independently rechecks cutoff
            # before constructing mutable session state.
            while remaining:
                if not self._can_launch(lease):
                    cutoff_reached = True
                    break
                assignment = remaining[0]
                assignment_id = _assignment_id(assignment)
                self._emit("session_admitted", {"assignment_id": assignment_id})
                # Event sinks are synchronous admission work and may consume the
                # last available phase time. Recheck immediately before submit.
                if not self._can_launch(lease):
                    cutoff_reached = True
                    break
                future = executor.submit(self._run_assignment, assignment, lease)
                active[future] = assignment
                remaining.popleft()
                self._emit("session_queued", {"assignment_id": assignment_id})
                if not self._can_launch(lease):
                    cutoff_reached = True
                    break

            while active and not cutoff_reached:
                if not self._can_launch(lease):
                    cutoff_reached = True
                    break
                timeout = lease.remaining(now=self.clock())
                if timeout <= 0:
                    cutoff_reached = True
                    break
                done, _ = wait(active, timeout=timeout, return_when=FIRST_COMPLETED)
                if not done:
                    cutoff_reached = True
                    break
                for future in done:
                    collect(future)
                if not self._can_launch(lease):
                    cutoff_reached = True

            if cutoff_reached:
                # Capture already-completed futures first, cancel only futures
                # that have not begun, then classify non-cancellable work as
                # in-flight without waiting into the finalization reserve.
                for future in tuple(active):
                    if future.done():
                        collect(future)
                for future in tuple(active):
                    if future.cancel():
                        collect(future)
                for future in tuple(active):
                    if future.done():
                        collect(future)
                    else:
                        in_flight.add(_assignment_id(active.pop(future)))
        finally:
            executor.shutdown(wait=not cutoff_reached, cancel_futures=cutoff_reached)

        stable_results = tuple(
            AssignmentResult(
                assignment_id,
                completed[assignment_id].result,
                completed[assignment_id].session,
            )
            for assignment_id in ordered_ids
            if assignment_id in completed
        )
        stable_failures = tuple(
            WaveFailure(
                assignment_id,
                failed[assignment_id],
                failed_sessions.get(assignment_id),
            )
            for assignment_id in ordered_ids
            if assignment_id in failed
        )
        never_submitted = {_assignment_id(item) for item in remaining}
        not_started_set = never_submitted | declined
        not_started = tuple(item for item in ordered_ids if item in not_started_set)
        cancelled_assignment_ids = tuple(item for item in ordered_ids if item in cancelled)
        in_flight_assignment_ids = tuple(item for item in ordered_ids if item in in_flight)

        evidence_ids = set(self.wave_snapshot.evidence.evidence_ids)
        coverage = dict(self.wave_snapshot.coverage.obligation_statuses)
        for item in stable_results:
            checkpoint = item.session_result.checkpoint
            evidence_ids.update(checkpoint.evidence_ids)
            for obligation_id, status in checkpoint.obligation_statuses:
                coverage[obligation_id] = _stronger_status(
                    coverage.get(obligation_id), ObligationStatus(status)
                )

        result_by_id = {item.assignment_id: item for item in stable_results}
        failure_by_id = {item.assignment_id: item for item in stable_failures}
        for assignment_id in ordered_ids:
            if assignment_id in result_by_id:
                self._emit("session_completed", {"assignment_id": assignment_id})
            elif assignment_id in failure_by_id:
                self._emit("session_failed", {
                    "assignment_id": assignment_id,
                    "error": failure_by_id[assignment_id].error,
                })
            elif assignment_id in not_started_set:
                self._emit("session_not_started", {"assignment_id": assignment_id})
            elif assignment_id in cancelled:
                self._emit("session_cancelled", {"assignment_id": assignment_id})
            elif assignment_id in in_flight:
                self._emit("session_in_flight", {"assignment_id": assignment_id})
        self._emit("wave_completed", {
            "phase": normalized_phase.value,
            "completed": tuple(item.assignment_id for item in stable_results),
            "failed": tuple(item.assignment_id for item in stable_failures),
            "not_started": not_started,
            "cancelled": cancelled_assignment_ids,
            "in_flight": in_flight_assignment_ids,
        })
        return WaveResult(
            phase=normalized_phase,
            results=stable_results,
            failures=stable_failures,
            not_started=not_started,
            cancelled_assignment_ids=cancelled_assignment_ids,
            in_flight_assignment_ids=in_flight_assignment_ids,
            evidence_ids=tuple(sorted(evidence_ids)),
            coverage_projection=tuple(sorted(coverage.items())),
        )
