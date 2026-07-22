from __future__ import annotations

from dataclasses import dataclass
from threading import Condition, Event, Thread, current_thread

import pytest

from pr_reviewer.specialist_runtime.assignments import Assignment
from pr_reviewer.specialist_runtime.budget import RunDeadline
from pr_reviewer.specialist_runtime.coverage import CoverageSnapshot
from pr_reviewer.specialist_runtime.evidence import EvidenceSnapshot
from pr_reviewer.specialist_runtime.scheduler import SessionScheduler, WaveSnapshot
from pr_reviewer.specialist_runtime.session import SessionResult
from pr_reviewer.specialist_runtime.types import (
    BudgetUsage,
    ObligationStatus,
    PhaseShares,
    RunPhase,
    SessionCheckpoint,
    SessionState,
)


@dataclass
class FakeClock:
    value: float

    def __call__(self) -> float:
        return self.value


def assignment(assignment_id: str, priority: str = "normal") -> Assignment:
    return Assignment(
        id=assignment_id,
        title=assignment_id,
        objective=f"Review {assignment_id}",
        obligation_ids=(f"OB-{assignment_id}",),
        recipe_ids=(),
        lenses=("correctness",),
        seed_paths=(f"{assignment_id}.py",),
        boundary_paths=(),
        expected_evidence=("implementation",),
        estimated_turns=1,
        priority=priority,
    )


def session_result(
    session_id: str,
    *,
    evidence_ids: tuple[str, ...],
    statuses: tuple[tuple[str, ObligationStatus], ...],
) -> SessionResult:
    return SessionResult(
        session_id=session_id,
        state=SessionState.CHECKPOINT,
        checkpoint=SessionCheckpoint(
            session_id=session_id,
            state=SessionState.CHECKPOINT,
            evidence_ids=evidence_ids,
            obligation_statuses=statuses,
        ),
        budget=BudgetUsage(),
    )


class OrderedCompletion:
    """Release concurrent fake sessions in an exact order without sleeping."""

    def __init__(self, order: tuple[str, ...]) -> None:
        self._order = order
        self._started: set[str] = set()
        self._next = 0
        self._condition = Condition()

    def complete(self, session_id: str) -> None:
        with self._condition:
            self._started.add(session_id)
            self._condition.notify_all()
            self._condition.wait_for(lambda: len(self._started) == len(self._order))
            self._condition.wait_for(lambda: self._order[self._next] == session_id)
            self._next += 1
            self._condition.notify_all()


class FakeSession:
    def __init__(self, result: SessionResult, *, before_result=lambda: None) -> None:
        self._result = result
        self._before_result = before_result

    def explore(self) -> SessionResult:
        self._before_result()
        return self._result


def deadline() -> RunDeadline:
    return RunDeadline(0.0, 100.0, PhaseShares())


def empty_snapshot() -> WaveSnapshot:
    return WaveSnapshot(
        evidence=EvidenceSnapshot(()),
        coverage=CoverageSnapshot((), (), ()),
    )


def scheduler_with_completion_order(order: tuple[str, ...]) -> SessionScheduler:
    completion = OrderedCompletion(order)
    results = {
        "S1": session_result(
            "S1",
            evidence_ids=("E-shared", "E-1"),
            statuses=(("OB-shared", ObligationStatus.UNRESOLVED),),
        ),
        "S2": session_result(
            "S2",
            evidence_ids=("E-2", "E-shared"),
            statuses=(("OB-shared", ObligationStatus.COVERED),),
        ),
    }

    def factory(item, lease, snapshot):
        return FakeSession(
            results[item.id], before_result=lambda: completion.complete(item.id)
        )

    return SessionScheduler(
        deadline=deadline(),
        session_factory=factory,
        wave_snapshot=empty_snapshot(),
        concurrency=2,
        clock=FakeClock(20.0),
    )


def test_wave_merge_is_independent_of_completion_order():
    assignments = (assignment("S2", "normal"), assignment("S1", "critical"))

    slow_first = scheduler_with_completion_order(("S2", "S1")).run_wave(
        assignments, RunPhase.INITIAL
    )
    fast_first = scheduler_with_completion_order(("S1", "S2")).run_wave(
        assignments, RunPhase.INITIAL
    )

    assert slow_first.coverage_projection == fast_first.coverage_projection == (
        ("OB-shared", ObligationStatus.COVERED),
    )
    assert slow_first.evidence_ids == fast_first.evidence_ids == (
        "E-1", "E-2", "E-shared"
    )
    assert tuple(item.assignment_id for item in slow_first.results) == ("S1", "S2")


def test_finalization_reserve_blocks_new_exploration():
    assignments = (assignment("S1", "critical"), assignment("S2", "low"))
    started = []

    def factory(item, lease, snapshot):
        started.append(item.id)
        raise AssertionError("factory must not run in finalization reserve")

    scheduler = SessionScheduler(
        deadline=deadline(), session_factory=factory,
        wave_snapshot=empty_snapshot(), concurrency=2, clock=FakeClock(90.0),
    )

    result = scheduler.run_wave(assignments, RunPhase.INITIAL)

    assert result.not_started == ("S1", "S2")
    assert result.cancelled_assignment_ids == ()
    assert result.in_flight_assignment_ids == ()
    assert started == []


def test_high_risk_starts_first_and_cutoff_stops_pending_work():
    clock = FakeClock(20.0)
    cutoff_set = Event()
    started = []
    results = {
        name: session_result(
            name, evidence_ids=(f"E-{name}",),
            statuses=((f"OB-{name}", ObligationStatus.COVERED),),
        )
        for name in ("critical", "normal", "low")
    }

    def factory(item, lease, snapshot):
        started.append(item.id)

        def advance_to_cutoff():
            clock.value = deadline().cutoff_for(RunPhase.INITIAL)
            cutoff_set.set()

        return FakeSession(
            results[item.id],
            before_result=advance_to_cutoff if item.id == "critical" else lambda: None,
        )

    scheduler = SessionScheduler(
        deadline=deadline(), session_factory=factory,
        wave_snapshot=empty_snapshot(), concurrency=1, clock=clock,
        event_sink=lambda kind, payload: (
            cutoff_set.wait(1.0)
            if kind == "session_queued" and payload.get("assignment_id") == "critical"
            else None
        ),
    )

    result = scheduler.run_wave(
        (
            assignment("low", "low"),
            assignment("normal", "normal"),
            assignment("critical", "critical"),
        ),
        RunPhase.INITIAL,
    )

    assert started == ["critical"]
    assert tuple(item.assignment_id for item in result.results) == ("critical",)
    assert result.not_started == ("normal", "low")
    assert result.cancelled_assignment_ids == ()
    assert result.in_flight_assignment_ids == ()


def test_every_session_gets_same_wave_snapshot_and_phase_bounded_lease():
    clock = FakeClock(65.0)
    snapshot = empty_snapshot()
    received = []

    def factory(item, lease, wave_snapshot):
        received.append((wave_snapshot, lease, lease.request_timeout(30.0, now=clock())))
        return FakeSession(session_result(
            item.id, evidence_ids=(),
            statuses=((f"OB-{item.id}", ObligationStatus.PENDING),),
        ))

    scheduler = SessionScheduler(
        deadline=deadline(), session_factory=factory,
        wave_snapshot=snapshot, concurrency=2, clock=clock,
    )

    scheduler.run_wave(
        (assignment("S1", "critical"), assignment("S2", "high")),
        RunPhase.INITIAL,
    )

    assert len(received) == 2
    assert received[0][0] is snapshot and received[1][0] is snapshot
    assert all(item[1].deadline_at == 70.0 for item in received)
    assert [item[2] for item in received] == [5.0, 5.0]


def test_default_concurrency_is_one_and_does_not_share_mutable_session_state():
    active = 0
    max_active = 0
    sessions = []

    class IsolatedSession(FakeSession):
        def explore(self):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                return super().explore()
            finally:
                active -= 1

    def factory(item, lease, snapshot):
        session = IsolatedSession(session_result(
            item.id, evidence_ids=(),
            statuses=((f"OB-{item.id}", ObligationStatus.PENDING),),
        ))
        sessions.append(session)
        return session

    SessionScheduler(
        deadline=deadline(), session_factory=factory,
        wave_snapshot=empty_snapshot(), clock=FakeClock(20.0),
    ).run_wave((assignment("S1"), assignment("S2")), RunPhase.INITIAL)

    assert max_active == 1
    assert len(sessions) == 2
    assert sessions[0] is not sessions[1]


def test_cutoff_crossing_inside_slow_event_sink_prevents_submission_and_false_start():
    clock = FakeClock(20.0)
    events = []
    factories = []

    def sink(kind, payload):
        events.append((kind, payload.get("assignment_id")))
        if kind == "session_admitted":
            clock.value = deadline().cutoff_for(RunPhase.INITIAL)

    def factory(item, lease, snapshot):
        factories.append(item.id)
        raise AssertionError("cutoff assignment must not construct a session")

    result = SessionScheduler(
        deadline=deadline(), session_factory=factory, wave_snapshot=empty_snapshot(),
        concurrency=1, clock=clock, event_sink=sink,
    ).run_wave((assignment("S1", "critical"),), RunPhase.INITIAL)

    assert factories == []
    assert result.not_started == ("S1",)
    assert result.cancelled_assignment_ids == ()
    assert result.in_flight_assignment_ids == ()
    assert events == [
        ("wave_started", None),
        ("session_admitted", "S1"),
        ("session_not_started", "S1"),
        ("wave_completed", None),
    ]
    assert all(kind != "session_started" for kind, _ in events)


def test_submitted_pending_future_is_cancelled_but_running_future_is_in_flight():
    clock = FakeClock(20.0)
    worker_started = Event()
    release_worker = Event()
    worker_finished = Event()
    returned = Event()
    events = []
    outcome = {}

    class BlockingSession(FakeSession):
        def explore(self):
            worker_started.set()
            assert release_worker.wait(1.0)
            try:
                return super().explore()
            finally:
                worker_finished.set()

    def factory(item, lease, snapshot):
        return BlockingSession(session_result(
            item.id, evidence_ids=(),
            statuses=((f"OB-{item.id}", ObligationStatus.PENDING),),
        ))

    def sink(kind, payload):
        events.append((kind, payload.get("assignment_id")))
        if kind == "session_queued" and payload.get("assignment_id") == "S1":
            assert worker_started.wait(1.0)
        if kind == "session_queued" and payload.get("assignment_id") == "S2":
            clock.value = deadline().cutoff_for(RunPhase.INITIAL)

    scheduler = SessionScheduler(
        deadline=deadline(), session_factory=factory, wave_snapshot=empty_snapshot(),
        concurrency=1, clock=clock, event_sink=sink,
    )

    def run():
        try:
            outcome["result"] = scheduler.run_wave(
                (assignment("S1", "critical"), assignment("S2", "low")),
                RunPhase.INITIAL,
            )
        finally:
            returned.set()

    runner = Thread(target=run)
    runner.start()
    returned_before_release = returned.wait(0.5)
    release_worker.set()
    runner.join(1.0)
    assert returned_before_release, "scheduler waited for an in-flight session at cutoff"
    assert not runner.is_alive()
    assert worker_finished.wait(1.0)

    result = outcome["result"]
    assert result.results == ()
    assert result.failures == ()
    assert result.not_started == ()
    assert result.cancelled_assignment_ids == ("S2",)
    assert result.in_flight_assignment_ids == ("S1",)
    assert events == [
        ("wave_started", None),
        ("session_admitted", "S1"),
        ("session_queued", "S1"),
        ("session_admitted", "S2"),
        ("session_queued", "S2"),
        ("session_in_flight", "S1"),
        ("session_cancelled", "S2"),
        ("wave_completed", None),
    ]


def test_worker_declines_session_construction_when_cutoff_crosses_after_submit():
    queued = Event()
    factories = []

    class WorkerGuardClock:
        def __call__(self):
            if current_thread().name.startswith("ThreadPoolExecutor"):
                assert queued.wait(1.0)
                return deadline().cutoff_for(RunPhase.INITIAL)
            return 20.0

    def sink(kind, payload):
        if kind == "session_queued":
            queued.set()

    def factory(item, lease, snapshot):
        factories.append(item.id)
        raise AssertionError("worker cutoff guard must run before factory")

    result = SessionScheduler(
        deadline=deadline(), session_factory=factory, wave_snapshot=empty_snapshot(),
        concurrency=1, clock=WorkerGuardClock(), event_sink=sink,
    ).run_wave((assignment("S1", "critical"),), RunPhase.INITIAL)

    assert factories == []
    assert result.not_started == ("S1",)
    assert result.cancelled_assignment_ids == ()
    assert result.in_flight_assignment_ids == ()


def test_slow_factory_crossing_cutoff_never_starts_session_exploration():
    clock = FakeClock(20.0)
    factory_finished = Event()
    constructed = []
    explored = []
    events = []

    class MustNotExplore(FakeSession):
        def explore(self):
            explored.append("S1")
            return super().explore()

    def factory(item, lease, snapshot):
        constructed.append(item.id)
        clock.value = deadline().cutoff_for(RunPhase.INITIAL)
        factory_finished.set()
        return MustNotExplore(session_result(
            item.id, evidence_ids=(),
            statuses=((f"OB-{item.id}", ObligationStatus.PENDING),),
        ))

    def sink(kind, payload):
        events.append((kind, payload.get("assignment_id")))
        if kind == "session_queued":
            assert factory_finished.wait(1.0)

    result = SessionScheduler(
        deadline=deadline(), session_factory=factory, wave_snapshot=empty_snapshot(),
        concurrency=1, clock=clock, event_sink=sink,
    ).run_wave((assignment("S1", "critical"),), RunPhase.INITIAL)

    assert constructed == ["S1"]
    assert explored == []
    assert result.results == ()
    assert result.failures == ()
    assert result.not_started == ("S1",)
    assert result.cancelled_assignment_ids == ()
    assert result.in_flight_assignment_ids == ()
    assert events == [
        ("wave_started", None),
        ("session_admitted", "S1"),
        ("session_queued", "S1"),
        ("session_not_started", "S1"),
        ("wave_completed", None),
    ]


@pytest.mark.parametrize("phase", [RunPhase.PLANNING, RunPhase.FINALIZATION])
def test_scheduler_rejects_non_exploration_wave_phases(phase):
    factories = []
    scheduler = SessionScheduler(
        deadline=deadline(),
        session_factory=lambda *args: factories.append(args),
        wave_snapshot=empty_snapshot(),
        concurrency=1,
        clock=FakeClock(5.0),
    )

    with pytest.raises(ValueError, match="initial or followup"):
        scheduler.run_wave((assignment("S1", "critical"),), phase)

    assert factories == []


def test_session_exception_is_a_stable_failure_not_not_started():
    events = []

    class BrokenSession:
        def explore(self):
            raise RuntimeError("provider exploded")

    result = SessionScheduler(
        deadline=deadline(), session_factory=lambda *_: BrokenSession(),
        wave_snapshot=empty_snapshot(), concurrency=1, clock=FakeClock(20.0),
        event_sink=lambda kind, payload: events.append(
            (kind, payload.get("assignment_id"))
        ),
    ).run_wave((assignment("S1", "critical"),), RunPhase.INITIAL)

    assert result.results == ()
    assert tuple((item.assignment_id, item.error) for item in result.failures) == (
        ("S1", "RuntimeError: provider exploded"),
    )
    assert result.not_started == ()
    assert result.cancelled_assignment_ids == ()
    assert result.in_flight_assignment_ids == ()
    assert events == [
        ("wave_started", None),
        ("session_admitted", "S1"),
        ("session_queued", "S1"),
        ("session_failed", "S1"),
        ("wave_completed", None),
    ]


def test_session_base_exception_is_a_stable_failure_for_reassignment():
    class FatalWorkerSignal(BaseException):
        pass

    class BrokenSession:
        def explore(self):
            raise FatalWorkerSignal("worker interrupted")

    result = SessionScheduler(
        deadline=deadline(), session_factory=lambda *_: BrokenSession(),
        wave_snapshot=empty_snapshot(), concurrency=1, clock=FakeClock(20.0),
    ).run_wave((assignment("S1", "critical"),), RunPhase.INITIAL)

    assert tuple((item.assignment_id, item.error) for item in result.failures) == (
        ("S1", "FatalWorkerSignal: worker interrupted"),
    )


def test_hostile_factory_baseexception_does_not_block_completed_sibling():
    class HostileFactoryError(BaseException):
        def __str__(self):
            raise KeyboardInterrupt("hostile str")

        def __repr__(self):
            raise KeyboardInterrupt("hostile repr")

    def factory(item, lease, snapshot):
        del lease, snapshot
        if item.id == "S1":
            raise HostileFactoryError()
        return FakeSession(session_result(
            item.id,
            evidence_ids=("E-S2",),
            statuses=(("OB-S2", ObligationStatus.COVERED),),
        ))

    result = SessionScheduler(
        deadline=deadline(), session_factory=factory,
        wave_snapshot=empty_snapshot(), concurrency=2, clock=FakeClock(20.0),
    ).run_wave(
        (assignment("S1", "critical"), assignment("S2", "normal")),
        RunPhase.INITIAL,
    )

    assert tuple(item.assignment_id for item in result.results) == ("S2",)
    assert tuple((item.assignment_id, item.error) for item in result.failures) == (
        ("S1", "HostileFactoryError: [unserializable]"),
    )
    assert result.evidence_ids == ("E-S2",)


def test_scheduler_workers_are_daemonized_for_abandoned_in_flight_work():
    daemon_flags = []

    result = SessionScheduler(
        deadline=deadline(),
        session_factory=lambda item, lease, snapshot: FakeSession(
            session_result(
                item.id,
                evidence_ids=(),
                statuses=((f"OB-{item.id}", ObligationStatus.COVERED),),
            ),
            before_result=lambda: daemon_flags.append(current_thread().daemon),
        ),
        wave_snapshot=empty_snapshot(),
        concurrency=1,
        clock=FakeClock(20.0),
    ).run_wave((assignment("S1", "critical"),), RunPhase.INITIAL)

    assert result.failures == ()
    assert daemon_flags == [True]


def test_terminal_event_order_is_risk_id_stable_not_completion_order():
    completion = OrderedCompletion(("S2", "S1"))
    events = []

    def factory(item, lease, snapshot):
        return FakeSession(
            session_result(
                item.id, evidence_ids=(),
                statuses=((f"OB-{item.id}", ObligationStatus.COVERED),),
            ),
            before_result=lambda: completion.complete(item.id),
        )

    result = SessionScheduler(
        deadline=deadline(), session_factory=factory, wave_snapshot=empty_snapshot(),
        concurrency=2, clock=FakeClock(20.0),
        event_sink=lambda kind, payload: events.append(
            (kind, payload.get("assignment_id"))
        ),
    ).run_wave(
        (assignment("S2", "normal"), assignment("S1", "critical")),
        RunPhase.INITIAL,
    )

    assert tuple(item.assignment_id for item in result.results) == ("S1", "S2")
    assert events == [
        ("wave_started", None),
        ("session_admitted", "S1"),
        ("session_queued", "S1"),
        ("session_admitted", "S2"),
        ("session_queued", "S2"),
        ("session_completed", "S1"),
        ("session_completed", "S2"),
        ("wave_completed", None),
    ]
