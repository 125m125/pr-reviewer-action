"""Integration tests for the specialist review controller."""

from dataclasses import dataclass, replace

import json
import time

from pr_reviewer.specialist_runtime.adjudication import ReviewHandoffContext
from pr_reviewer.specialist_runtime.controller import (
    ReviewController,
    ReviewInputs,
    ReviewResult,
)
from pr_reviewer.specialist_runtime.events import EventJournal
from pr_reviewer.specialist_runtime.coverage import derive_obligations
from pr_reviewer.specialist_runtime.policy import RecipePolicy, ReviewPolicy, RuntimeConfig
from pr_reviewer.specialist_runtime.session import SessionResult
from pr_reviewer.specialist_runtime.types import (
    BudgetLimits,
    BudgetUsage,
    CandidateFinding,
    CoverageObligation,
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

    def __post_init__(self):
        self.session_id = self.assignment.id
        self.candidate_findings = ()

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
    def factory(assignment, lease, snapshot, evidence_store, coverage, obligations):
        del lease, snapshot, coverage
        return _SuccessfulSession(assignment, evidence_store, obligations)

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


def _factory(assignment, lease, snapshot, evidence_store, coverage, obligations):
    del lease, snapshot, coverage
    return _SuccessfulSession(assignment, evidence_store, obligations)


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

    def factory(assignment, lease, snapshot, evidence_store, coverage, obligations):
        del lease, snapshot, coverage
        attempts.append(assignment.id)
        if "followup" not in assignment.id:
            raise RuntimeError("specialist transport failed")
        return _SuccessfulSession(assignment, evidence_store, obligations)

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

    def __post_init__(self):
        self.session_id = self.assignment.id
        self.calls = 0
        self.feedback = ()
        self.candidate_findings = ()

    def apply_coverage_feedback(self, gaps):
        self.feedback = tuple(gaps)

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

    def factory(assignment, lease, snapshot, evidence_store, coverage, obligations):
        del lease, snapshot, coverage
        session = _ResumeSession(assignment, evidence_store, obligations)
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
