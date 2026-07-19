from __future__ import annotations

from dataclasses import dataclass
from threading import Condition

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
    assert started == []


def test_high_risk_starts_first_and_cutoff_stops_pending_work():
    clock = FakeClock(20.0)
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

        return FakeSession(
            results[item.id],
            before_result=advance_to_cutoff if item.id == "critical" else lambda: None,
        )

    scheduler = SessionScheduler(
        deadline=deadline(), session_factory=factory,
        wave_snapshot=empty_snapshot(), concurrency=1, clock=clock,
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
