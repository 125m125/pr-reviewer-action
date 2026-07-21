"""Integration tests for the specialist review controller."""

from dataclasses import dataclass, replace

import json
import math
import time

from pr_reviewer.specialist_runtime.adjudication import ReviewHandoffContext
from pr_reviewer.specialist_runtime.controller import (
    RoleRequest,
    ReviewController,
    ReviewInputs,
    ReviewResult,
)
from pr_reviewer.specialist_runtime.events import EventJournal
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.coverage import derive_obligations
from pr_reviewer.specialist_runtime.policy import RecipePolicy, ReviewPolicy, RuntimeConfig
from pr_reviewer.specialist_runtime.session import SessionResult
from pr_reviewer.specialist_runtime.types import (
    BudgetLimits,
    BudgetUsage,
    CandidateFinding,
    CoverageObligation,
    PhaseShares,
    SessionCheckpoint,
    SessionState,
)
from pr_reviewer.specialist_runtime.web_evidence import SourceAccessRequest


def test_controller_public_api_is_importable():
    assert ReviewController
    assert ReviewInputs
    assert ReviewResult


def _policy() -> ReviewPolicy:
    return ReviewPolicy.minimal(recipes=(RecipePolicy(
        id="delivery",
        title="Delivery",
        objective="Trace delivery behavior",
        execution="coverage",
        match={"file_roles_any": ("implementation",)},
        expected_evidence=("implementation",),
    ),))


def _inputs(tmp_path) -> ReviewInputs:
    return ReviewInputs(
        repository="owner/repository",
        pr_number=17,
        base_sha="a" * 40,
        head_sha="b" * 40,
        topology={
            "changed_files": ["src/worker.py"],
            "file_roles": ["implementation"],
            "components": [{"id": "worker", "changed_files": ["src/worker.py"]}],
        },
        classification={"risk_flags": []},
        policy=_policy(),
        config=RuntimeConfig(
            review_deadline_sec=100,
            model_request_timeout_sec=1,
            max_sessions=4,
            max_followup_sessions=1,
            session_limits=BudgetLimits(model_turns=8, tool_calls=8, recoveries=1),
        ),
        changed_files=("src/worker.py",),
        artifact_path=tmp_path / "specialist-review-artifact.json",
        artifact_output_root=tmp_path,
        allow_approve=True,
        publishing_mode="review_comment",
    )


def _planner(obligations, topology, config):
    del topology, config
    ids = [item.id for item in obligations if item.mandatory and item.required_evidence]
    required = sorted({category for item in obligations for category in item.required_evidence})
    paths = sorted({path for item in obligations for path in (*item.scope, *item.seed_hints)})
    priority_order = {"critical": 0, "high": 1, "normal": 2, "low": 3}
    priority = min(
        (item.risk_tier for item in obligations if item.id in ids),
        key=lambda value: priority_order.get(value, 2),
    )
    return {"assignments": [{
        "id": "worker-flow",
        "title": "Worker flow",
        "objective": "Review the changed worker behavior",
        "obligation_ids": ids,
        "lenses": ["delivery"],
        "seed_paths": paths,
        "boundary_paths": [],
        "expected_evidence": required,
        "estimated_turns": len(ids),
        "priority": priority,
        "overlap_justification": "",
    }]}


@dataclass
class _SuccessfulSession:
    assignment: object
    evidence_store: object
    obligations: tuple[object, ...]
    expected_session_id: str

    def __post_init__(self):
        self.session_id = self.expected_session_id
        self.candidate_findings = ()

    def update_lease(self, lease):
        self.lease = lease

    def explore(self):
        evidence_ids = []
        for category in sorted({
            category
            for obligation in self.obligations
            if obligation.id in self.assignment.obligation_ids
            for category in obligation.required_evidence
        }):
            record = self.evidence_store.add_tool_result(
                session_id=self.session_id,
                tool="read_file",
                arguments={"path": "src/worker.py", "category": category},
                result={"status": "ok", "content": "def process(): pass"},
                category=category,
            )
            evidence_ids.append(record.id)
        first_obligation = self.assignment.obligation_ids[0]
        self.candidate_findings = (CandidateFinding(
            candidate_id="candidate-delivery",
            root_cause_fingerprint="model-value",
            claim="A retry can process one delivery twice",
            affected_location="src/worker.py:7",
            causal_chain="The retry path repeats processing after an ambiguous result.",
            severity="minor",
            category="failure_recovery",
            supporting_evidence_ids=(evidence_ids[0],),
            related_obligation_ids=(first_obligation,),
            collector_session_id=self.session_id,
            model_identity="specialist-test",
            user_visible_consequence="One delivery can be applied twice.",
            manual_validation="Force the retry path and verify one processing result.",
        ),)
        checkpoint = SessionCheckpoint(
            session_id=self.session_id,
            state=SessionState.CHECKPOINT,
            evidence_ids=tuple(evidence_ids),
            candidate_finding_ids=("candidate-delivery",),
        )
        return SessionResult(
            session_id=self.session_id,
            state=SessionState.CHECKPOINT,
            checkpoint=checkpoint,
            budget=BudgetUsage(model_turns=1, tool_calls=len(evidence_ids)),
        )

    def finalize(self):
        result = self.explore()
        return SessionResult(
            session_id=result.session_id,
            state=SessionState.COMPLETE,
            checkpoint=result.checkpoint,
            budget=result.budget,
            report={"summary": "Worker delivery reviewed", "recommendation": "approve"},
        )


def test_controller_runs_obligations_assignments_sessions_and_finalizer(tmp_path):
    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, coverage
        return _SuccessfulSession(
            assignment, evidence_store, obligations, expected_session_id,
        )

    controller = ReviewController(
        planner=_planner,
        session_factory=factory,
        critic=lambda candidates, state: {
            "decisions": [
                {"candidate_id": item.candidate_id, "action": "keep"}
                for item in candidates
            ]
        },
        finalizer=lambda state: ReviewHandoffContext(
            recommendation="approve",
            status="complete",
            recipe_ids=("delivery",),
        ),
        clock=lambda: 0.0,
    )

    result = controller.run(_inputs(tmp_path))

    assert result.artifact["evaluation_status"] == "complete"
    assert result.artifact["recipes"]["delivery"]["status"] == "covered"
    assert result.handoff.markdown.startswith("## AI Review Handoff")
    assert result.notes[0].evidence_ids
    assert result.verdict in {"approve", "request_changes"}
    assert result.artifact_path == tmp_path / "specialist-review-artifact.json"
    assert result.artifact_path.read_bytes().startswith(b'{"accepted_candidates"')
    assert [event.payload["phase"] for event in result.events if event.kind == "phase_changed"] == [
        "precheck", "planning", "initial", "followup", "finalization",
        "publish_ready", "complete",
    ]


def _factory(
    assignment, lease, snapshot, evidence_store, coverage, obligations,
    expected_session_id,
):
    del lease, snapshot, coverage
    return _SuccessfulSession(
        assignment, evidence_store, obligations, expected_session_id,
    )


def _finalizer(state):
    del state
    return ReviewHandoffContext(
        recommendation="approve", status="complete", recipe_ids=("delivery",),
    )


def _controller(**overrides):
    values = {
        "planner": _planner,
        "session_factory": _factory,
        "critic": lambda candidates, state: {
            "decisions": [
                {"candidate_id": item.candidate_id, "action": "keep"}
                for item in candidates
            ]
        },
        "finalizer": _finalizer,
        "clock": lambda: 0.0,
    }
    values.update(overrides)
    return ReviewController(**values)


def test_planner_failure_uses_deterministic_assignment_plan(tmp_path):
    def broken_planner(*args):
        raise RuntimeError("planner unavailable")

    result = _controller(planner=broken_planner).run(_inputs(tmp_path))

    assert result.artifact["assignments"][0]["id"].startswith("fallback-")
    assert result.artifact["evaluation_status"] == "degraded"
    assert any(
        item["component"] == "planner" for item in result.artifact["degradation"]
    )
    assert result.publishing_ready is True


def test_specialist_failure_gets_one_bounded_followup_reassignment(tmp_path):
    attempts = []

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, coverage
        attempts.append(assignment.id)
        if "followup" not in assignment.id:
            raise RuntimeError("specialist transport failed")
        return _SuccessfulSession(
            assignment, evidence_store, obligations, expected_session_id,
        )

    result = _controller(session_factory=factory).run(_inputs(tmp_path))

    assert len(attempts) == 2
    assert "followup" in attempts[1]
    assert result.artifact["recipes"]["delivery"]["status"] == "covered"
    assert any(
        item["component"].startswith("specialist:")
        for item in result.artifact["degradation"]
    )


@dataclass
class _ResumeSession:
    assignment: object
    evidence_store: object
    obligations: tuple[object, ...]
    expected_session_id: str

    def __post_init__(self):
        self.session_id = self.expected_session_id
        self.calls = 0
        self.feedback = ()
        self.candidate_findings = ()

    def apply_coverage_feedback(self, gaps):
        self.feedback = tuple(gaps)

    def update_lease(self, lease):
        self.lease = lease

    def explore(self):
        self.calls += 1
        evidence_ids = ()
        if self.calls > 1:
            record = self.evidence_store.add_tool_result(
                session_id=self.session_id,
                tool="read_file",
                arguments={"path": "src/worker.py"},
                result={"status": "ok", "content": "def process(): pass"},
                category="implementation",
            )
            evidence_ids = (record.id,)
        checkpoint = SessionCheckpoint(
            session_id=self.session_id,
            state=SessionState.CHECKPOINT,
            evidence_ids=evidence_ids,
            unknowns=() if evidence_ids else self.assignment.obligation_ids,
        )
        return SessionResult(
            session_id=self.session_id,
            state=SessionState.CHECKPOINT,
            checkpoint=checkpoint,
            budget=BudgetUsage(model_turns=self.calls, tool_calls=len(evidence_ids)),
        )

    def finalize(self):
        result = self.explore()
        return replace(result, state=SessionState.COMPLETE, report={"summary": "done"})


def test_negotiator_failure_uses_live_budget_fallback_resume(tmp_path):
    sessions = []

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, coverage
        session = _ResumeSession(
            assignment, evidence_store, obligations, expected_session_id,
        )
        sessions.append(session)
        return session

    def broken_negotiator(state):
        assert state.session_resources[0].remaining_model_turns == 7
        raise RuntimeError("negotiator invalid response")

    result = _controller(
        session_factory=factory, negotiator=broken_negotiator,
    ).run(_inputs(tmp_path))

    assert len(sessions) == 1
    assert sessions[0].feedback
    assert result.artifact["recipes"]["delivery"]["status"] == "covered"
    assert any(
        event.kind == "negotiation_action" and event.payload["kind"] == "resume"
        for event in result.events
    )


def test_critic_failure_rejects_ambiguous_candidate(tmp_path):
    ambiguous = CandidateFinding(
        candidate_id="ambiguous",
        root_cause_fingerprint="model",
        claim="Maybe broken",
        affected_location="src/worker.py:7",
        severity="major",
        confidence_rationale="token=private-chain-of-thought",
    )

    def broken_critic(*args):
        raise RuntimeError("critic unavailable")

    result = _controller(critic=broken_critic).run(replace(
        _inputs(tmp_path), candidate_findings=(ambiguous,),
    ))

    dispositions = [
        item for item in result.artifact["candidate_dispositions"]
        if item["candidate_id"] == "ambiguous"
    ]
    assert dispositions == [{
        "action": "reject",
        "candidate_id": "ambiguous",
        "reason": "critic-rejected",
        "target_id": None,
    }]
    assert all(
        item["candidate_id"] != "ambiguous"
        for item in result.artifact["accepted_candidates"]
    )
    assert "private-chain-of-thought" not in json.dumps(result.artifact)
    assert [
        item["candidate_id"] for item in result.artifact["rejected_candidates"]
    ] == ["ambiguous"]
    assert all(
        "confidence_rationale" not in item
        for item in result.artifact["rejected_candidates"]
    )
    assert result.verdict != "request_changes" or result.verdict_source != "supported-findings"


def test_finalizer_failure_builds_deterministic_minimal_sparse_handoff(tmp_path):
    def broken_finalizer(*args):
        raise RuntimeError("finalizer timed out")

    result = _controller(finalizer=broken_finalizer).run(_inputs(tmp_path))

    assert result.handoff.markdown.startswith("## AI Review Handoff")
    assert "review the complete change" in result.handoff.markdown
    assert any(
        item["component"] == "finalizer" for item in result.artifact["degradation"]
    )


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_deadline_stops_exploration_and_preserves_finalization_reserve(tmp_path):
    clock = _Clock()
    sessions_started = []
    finalizer_calls = []

    def planner(*args):
        raw = _planner(*args)
        clock.now = 90.0
        return raw

    def factory(*args):
        sessions_started.append(True)
        return _factory(*args)

    def finalizer(state):
        finalizer_calls.append(state)
        return _finalizer(state)

    result = _controller(
        planner=planner, session_factory=factory, finalizer=finalizer, clock=clock,
    ).run(_inputs(tmp_path))

    assert sessions_started == []
    assert len(finalizer_calls) == 1
    assert result.artifact["timing"]["finalization_reserve_seconds"] == 10
    assert result.artifact["unknowns"]
    assert any(
        item["component"] == "deadline" for item in result.artifact["degradation"]
    )


def test_artifact_write_failure_preserves_prior_file_and_valid_result(tmp_path):
    artifact_path = tmp_path / "specialist-review-artifact.json"
    artifact_path.write_text('{"prior":true}\n', encoding="utf-8")

    def broken_writer(path, artifact):
        del path, artifact
        raise OSError("disk full token=very-secret-value")

    result = _controller(artifact_writer=broken_writer).run(replace(
        _inputs(tmp_path), artifact_path=artifact_path,
    ))

    assert artifact_path.read_text(encoding="utf-8") == '{"prior":true}\n'
    assert result.artifact["artifact_write"]["status"] == "failed"
    assert result.artifact_write_error
    assert "very-secret-value" not in result.artifact_write_error
    assert json.dumps(result.artifact)
    assert result.events[-1].kind == "artifact_write_failed"


def test_event_journal_bounds_redacts_and_survives_observer_failure():
    def broken_sink(event):
        del event
        raise RuntimeError("observer failed")

    journal = EventJournal(broken_sink)
    event = journal.emit("component_failed", {
        "error": "token=very-secret-value " + "x" * 2000,
    })

    assert "very-secret-value" not in event.payload["error"]
    assert len(event.payload["error"]) <= 1000
    assert journal.snapshot() == (event,)
    assert journal.external_errors() == ("RuntimeError: observer failed",)


@dataclass
class _ParallelSession:
    assignment: object
    evidence_store: object
    obligation: CoverageObligation
    delay: float

    def __post_init__(self):
        self.session_id = self.assignment.id
        self.candidate_findings = ()

    def explore(self):
        time.sleep(self.delay)
        record = self.evidence_store.add_tool_result(
            session_id=self.session_id,
            tool="read_file",
            arguments={"path": self.obligation.scope[0]},
            result={"status": "ok", "content": self.assignment.id},
            category="implementation",
        )
        checkpoint = SessionCheckpoint(
            session_id=self.session_id,
            state=SessionState.CHECKPOINT,
            evidence_ids=(record.id,),
        )
        return SessionResult(
            session_id=self.session_id,
            state=SessionState.CHECKPOINT,
            checkpoint=checkpoint,
            budget=BudgetUsage(model_turns=1, tool_calls=1),
        )

    def finalize(self):
        return replace(self.explore(), state=SessionState.COMPLETE)


def test_semantic_artifact_is_stable_across_completion_order_and_clock_origin(tmp_path):
    obligations = (
        CoverageObligation(
            obligation_id="OB-a", origin="test", subject="a",
            required_evidence_categories=("implementation",), scope=("src/a.py",),
        ),
        CoverageObligation(
            obligation_id="OB-b", origin="test", subject="b",
            required_evidence_categories=("implementation",), scope=("src/b.py",),
        ),
    )

    def deriver(*args):
        del args
        return obligations

    def planner(items, topology, config):
        del topology, config
        return {"assignments": [{
            "id": f"session-{item.subject}",
            "title": item.subject,
            "objective": f"Review {item.subject}",
            "obligation_ids": [item.id],
            "lenses": ["implementation"],
            "seed_paths": list(item.scope),
            "boundary_paths": [],
            "expected_evidence": ["implementation"],
            "estimated_turns": 1,
            "priority": "normal",
            "overlap_justification": "",
        } for item in items]}

    def run(directory, delays, clock_origin):
        by_id = {item.id: item for item in obligations}

        def factory(assignment, lease, snapshot, evidence_store, coverage, items):
            del lease, snapshot, coverage, items
            obligation = by_id[assignment.obligation_ids[0]]
            return _ParallelSession(
                assignment, evidence_store, obligation, delays[assignment.id],
            )

        inputs = replace(
            _inputs(directory),
            topology={
                "changed_files": ["src/a.py", "src/b.py"],
                "components": [], "file_roles": ["implementation"],
            },
            changed_files=("src/a.py", "src/b.py"),
            policy=ReviewPolicy.minimal(),
            config=replace(_inputs(directory).config, concurrency=2),
        )
        return ReviewController(
            planner=planner,
            session_factory=factory,
            critic=lambda candidates, state: {"decisions": []},
            finalizer=_finalizer,
            clock=lambda: clock_origin,
            obligation_deriver=deriver,
        ).run(inputs).artifact

    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = run(first_dir, {"session-a": 0.02, "session-b": 0.0}, 0.0)
    second = run(second_dir, {"session-a": 0.0, "session-b": 0.02}, 1000.0)

    assert first == second


def test_controller_retains_and_emits_typed_source_access_requests(tmp_path):
    inputs = _inputs(tmp_path)
    obligation = derive_obligations(
        inputs.topology, inputs.classification, inputs.policy,
    )[0]
    request = SourceAccessRequest(
        host="docs.example.com",
        candidate_url="https://docs.example.com/runtime",
        obligation_id=obligation.id,
        purpose="Verify the external runtime contract",
        authority_reason="The current source policy does not allow this host.",
    )

    result = _controller().run(replace(
        inputs, source_access_requests=(request,),
    ))

    assert result.artifact["source_access_requests"][0]["host"] == "docs.example.com"
    assert any(event.kind == "source_access_request" for event in result.events)


def test_unexpected_controller_failure_returns_notice_without_fabricated_blocker(tmp_path):
    def broken_deriver(*args):
        del args
        raise RuntimeError("topology unavailable")

    result = ReviewController(
        planner=_planner,
        session_factory=_factory,
        critic=lambda candidates, state: {"decisions": []},
        finalizer=_finalizer,
        clock=lambda: 0.0,
        obligation_deriver=broken_deriver,
    ).run(_inputs(tmp_path))

    assert result.verdict == "notice"
    assert result.publishing_ready is False
    assert "Approve" not in result.handoff.markdown
    assert result.artifact["accepted_candidates"] == []
    assert result.artifact["verdict"]["blocking_finding_ids"] == []


def test_planner_receives_typed_phase_lease_request(tmp_path):
    requests = []

    def planner(request):
        requests.append(request)
        return _planner(
            request.context["obligations"],
            request.context["topology"],
            request.context["config"],
        )

    result = _controller(planner=planner).run(replace(
        _inputs(tmp_path),
        pr_metadata={"title": "Preserve retry intent", "body": "No duplicate work"},
    ))

    assert result.artifact["assignment_plan"]["source"] == "model_validated"
    assert len(requests) == 1
    request = requests[0]
    assert isinstance(request, RoleRequest)
    assert request.phase.value == "planning"
    assert request.request_id == "planner:1"
    assert request.timeout_sec <= request.lease.remaining(now=0.0)
    assert request.max_tokens > 0
    assert request.context["policy"] == _policy()
    assert request.context["pr_metadata"]["title"] == "Preserve retry intent"


def test_hanging_planner_is_cut_off_and_late_result_is_ignored(tmp_path):
    import threading

    release = threading.Event()
    returned = threading.Event()

    def hanging_planner(request):
        release.wait(2)
        returned.set()
        return _planner(
            request.context["obligations"],
            request.context["topology"],
            request.context["config"],
        )

    inputs = replace(
        _inputs(tmp_path),
        config=replace(
            _inputs(tmp_path).config,
            review_deadline_sec=0.08,
            model_request_timeout_sec=0.01,
        ),
    )
    started = time.monotonic()
    result = _controller(planner=hanging_planner, clock=time.monotonic).run(inputs)
    elapsed = time.monotonic() - started
    before = json.dumps(result.artifact, sort_keys=True)
    release.set()
    assert returned.wait(1)
    time.sleep(0.02)

    assert elapsed < 0.5
    assert result.artifact["assignment_plan"]["source"] == "deterministic_fallback"
    assert json.dumps(result.artifact, sort_keys=True) == before


def test_slow_keyboard_interrupt_event_observer_never_blocks_or_escapes():
    calls = []

    def observer(event):
        calls.append(event.sequence)
        time.sleep(0.2)
        raise KeyboardInterrupt("observer interrupt")

    journal = EventJournal(observer, observer_timeout_sec=0.01)
    started = time.monotonic()
    event = journal.emit("phase_changed", {"phase": "planning"})
    elapsed = time.monotonic() - started

    assert event.sequence == 1
    assert elapsed < 0.1
    time.sleep(0.25)
    assert calls == [1]
    assert "KeyboardInterrupt" in journal.external_errors()[0]


@dataclass
class _LateSession:
    assignment: object
    evidence_store: object
    expected_session_id: str
    started: object
    release: object
    finalized: object

    def __post_init__(self):
        self.session_id = self.expected_session_id
        self.candidate_findings = ()

    def explore(self):
        self.started.set()
        self.release.wait(2)
        record = self.evidence_store.add_tool_result(
            session_id=self.session_id,
            tool="read_file",
            arguments={"path": "src/worker.py"},
            result={"status": "ok", "content": "late evidence"},
            category="implementation",
        )
        return SessionResult(
            session_id=self.session_id,
            state=SessionState.CHECKPOINT,
            checkpoint=SessionCheckpoint(
                session_id=self.session_id,
                state=SessionState.CHECKPOINT,
                evidence_ids=(record.id,),
            ),
            budget=BudgetUsage(model_turns=1, tool_calls=1),
        )

    def finalize(self):
        self.finalized.set()
        raise AssertionError("an in-flight exploring session must not be finalized")


def test_in_flight_worker_is_isolated_and_never_finalized_after_wave_return(tmp_path):
    import threading

    started = threading.Event()
    release = threading.Event()
    finalized = threading.Event()

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, coverage, obligations
        return _LateSession(
            assignment, evidence_store, expected_session_id,
            started, release, finalized,
        )

    inputs = replace(
        _inputs(tmp_path),
        config=replace(
            _inputs(tmp_path).config,
            review_deadline_sec=10,
            model_request_timeout_sec=1,
            phase_shares=PhaseShares(
                planning=0, initial=1, followup=98, finalization=1,
            ),
            max_followup_sessions=0,
        ),
    )
    result = _controller(
        session_factory=factory, clock=time.monotonic,
    ).run(inputs)
    assert started.is_set(), result.artifact["degradation"]
    before = json.dumps(result.artifact, sort_keys=True)
    release.set()
    time.sleep(0.05)

    assert finalized.is_set() is False
    assert json.dumps(result.artifact, sort_keys=True) == before
    assert result.artifact["evidence"] == []
    assert result.artifact["sessions"] == []


def test_factory_session_identity_mismatch_fails_closed_without_ghost_state(tmp_path):
    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, coverage
        session = _SuccessfulSession(
            assignment, evidence_store, obligations, expected_session_id,
        )
        session.session_id = expected_session_id + "-forged"
        return session

    result = _controller(session_factory=factory).run(_inputs(tmp_path))

    assert result.artifact["sessions"] == []
    assert result.artifact["evidence"] == []
    assert any(
        "session identity" in item["reason"]
        for item in result.artifact["degradation"]
    )


def test_session_cannot_forge_evidence_collector_identity(tmp_path):
    class ForgedEvidenceSession(_SuccessfulSession):
        def explore(self):
            result = super().explore()
            forged = self.evidence_store.add_tool_result(
                session_id="session:someone-else",
                tool="read_file",
                arguments={"path": "src/forged.py"},
                result={"status": "ok", "content": "forged"},
                category="implementation",
            )
            return replace(result, checkpoint=replace(
                result.checkpoint,
                evidence_ids=(*result.checkpoint.evidence_ids, forged.id),
            ))

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, coverage
        return ForgedEvidenceSession(
            assignment, evidence_store, obligations, expected_session_id,
        )

    result = _controller(
        session_factory=factory,
    ).run(replace(
        _inputs(tmp_path),
        config=replace(_inputs(tmp_path).config, max_followup_sessions=0),
    ))

    assert result.artifact["sessions"] == []
    assert result.artifact["evidence"] == []
    assert any(
        "collector identity" in item["reason"]
        for item in result.artifact["degradation"]
    )


def test_resume_replaces_initial_lease_with_actual_followup_lease(tmp_path):
    leases = []

    class LeasedResume(_ResumeSession):
        def update_lease(self, lease):
            self.lease = lease
            leases.append(lease)

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del snapshot, coverage
        session = LeasedResume(
            assignment, evidence_store, obligations, expected_session_id,
        )
        session.lease = lease
        leases.append(lease)
        return session

    result = _controller(
        session_factory=factory,
        negotiator=lambda state: (_ for _ in ()).throw(RuntimeError("fallback")),
    ).run(_inputs(tmp_path))

    assert result.artifact["recipes"]["delivery"]["status"] == "covered"
    assert [lease.phase.value for lease in leases[:2]] == ["initial", "followup"]
    assert leases[1].deadline_at > leases[0].deadline_at


def test_artifact_is_schema_v2_strict_json_with_matching_reference(tmp_path):
    result = _controller().run(_inputs(tmp_path))

    assert result.artifact["schema_version"] == 2
    reference = next(
        event for event in result.events if event.kind == "artifact_reference"
    )
    assert reference.payload["artifact_id"] == result.artifact["artifact_id"]
    json.dumps(result.artifact, allow_nan=False)


def test_artifact_projection_omits_private_checkpoint_working_state(tmp_path):
    class PrivateCheckpointSession(_SuccessfulSession):
        def explore(self):
            result = super().explore()
            return replace(result, checkpoint=replace(
                result.checkpoint,
                hypotheses=("token=private-hypothesis",),
                unknowns=("password=private-unknown",),
                proposed_next_actions=("authorization: private-action",),
            ))

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, coverage
        return PrivateCheckpointSession(
            assignment, evidence_store, obligations, expected_session_id,
        )

    result = _controller(session_factory=factory).run(_inputs(tmp_path))
    encoded = json.dumps(result.artifact, sort_keys=True)

    assert "private-hypothesis" not in encoded
    assert "private-unknown" not in encoded
    assert "private-action" not in encoded
    assert set(result.artifact["sessions"][0]["checkpoint"]) == {
        "candidate_finding_ids", "evidence_ids", "imported_evidence_ids",
        "obligation_statuses", "session_id", "state",
    }


def test_finalizer_cannot_override_controller_owned_handoff_facts(tmp_path):
    def malicious_finalizer(state):
        del state
        return ReviewHandoffContext(
            recommendation="approve",
            status="complete",
            component_ids=("invented",),
            recipe_ids=("invented",),
            unresolved_thread_count=999,
            material_coverage_limited=False,
        )

    result = _controller(
        finalizer=malicious_finalizer,
        planner=lambda *args: (_ for _ in ()).throw(RuntimeError("degraded")),
    ).run(_inputs(tmp_path))

    assert "invented" not in result.handoff.markdown
    assert result.artifact["evaluation_status"] == "degraded"
    assert result.handoff.coverage_warning


def test_output_path_must_be_beneath_controller_owned_root(tmp_path):
    outside = tmp_path.parent / "must-not-overwrite.json"
    outside.write_text('{"owner":"user"}\n', encoding="utf-8")
    inputs = replace(_inputs(tmp_path), artifact_path=outside)

    result = _controller().run(inputs)

    assert outside.read_text(encoding="utf-8") == '{"owner":"user"}\n'
    assert result.artifact_path is None
    assert result.artifact_write_error
    assert result.publishing_ready is False


def test_event_journal_redacts_sensitive_key_variants_and_nonfinite_numbers():
    class Hostile:
        def __str__(self):
            raise KeyboardInterrupt("must not escape")

    event = EventJournal().emit("hostile", {
        "access-token": "value-a",
        "client_secret_value": "value-b",
        "AUTHORIZATION_HEADER": "value-c",
        "nan": math.nan,
        "positive_infinity": math.inf,
        "hostile": Hostile(),
    })
    payload = dict(event.payload)

    assert payload["access-token"] == "[REDACTED]"
    assert payload["client_secret_value"] == "[REDACTED]"
    assert payload["AUTHORIZATION_HEADER"] == "[REDACTED]"
    assert payload["nan"] == "[invalid-number]"
    assert payload["positive_infinity"] == "[invalid-number]"
    assert payload["hostile"] == "[unserializable]"
    json.dumps(payload, allow_nan=False)


def test_event_observer_concurrency_is_bounded():
    import threading

    release = threading.Event()
    observed = []

    def observer(event):
        observed.append(event.sequence)
        release.wait(1)

    journal = EventJournal(observer, observer_timeout_sec=0)
    for index in range(20):
        journal.emit("event", {"index": index})

    time.sleep(0.03)
    try:
        assert len(observed) <= 4
        assert any("capacity" in item for item in journal.external_errors())
    finally:
        release.set()


def test_reusing_controller_does_not_reuse_prior_run_evidence(tmp_path):
    shared = EvidenceStore()
    controller = _controller(evidence_store=shared)

    first = controller.run(_inputs(tmp_path))
    second = controller.run(replace(
        _inputs(tmp_path),
        head_sha="c" * 40,
        artifact_path=tmp_path / "second.json",
    ))

    assert len(first.artifact["evidence"]) == len(second.artifact["evidence"])
    assert shared.snapshot().records == ()


def test_emergency_projection_is_terminal_schema_valid_and_not_publishable(tmp_path):
    class BrokenProjectionController(ReviewController):
        def _artifact(self, state, path):
            del state, path
            raise KeyboardInterrupt("projection interrupted token=private")

    controller = BrokenProjectionController(
        planner=_planner,
        session_factory=_factory,
        critic=lambda candidates, state: {
            "decisions": [
                {"candidate_id": item.candidate_id, "action": "keep"}
                for item in candidates
            ],
        },
        finalizer=_finalizer,
        clock=lambda: 0.0,
    )

    result = controller.run(_inputs(tmp_path))

    assert result.artifact["schema_version"] == 2
    assert result.artifact["evaluation_status"] == "degraded"
    assert result.artifact["publishing"]["ready"] is False
    assert result.publishing_ready is False
    assert set(
        item["status"] for item in result.artifact["coverage"].values()
    ) <= {"covered", "partially_covered", "unresolved", "not_applicable", "suppressed_by_policy"}
    assert "private" not in json.dumps(result.artifact)
    json.dumps(result.artifact, allow_nan=False)
