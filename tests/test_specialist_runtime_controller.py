"""Integration tests for the specialist review controller."""

from dataclasses import dataclass, replace

import hashlib
import json
import math
import os
from pathlib import Path
import time

import pytest

from pr_reviewer.conversation import Conversation
from pr_reviewer.specialist_runtime import cli
from pr_reviewer.specialist_runtime.adjudication import (
    ReviewHandoffContext,
    ReviewOrientationTopic,
)
from pr_reviewer.specialist_runtime.controller import (
    EvidenceSeed,
    FinalizerProposal,
    RoleRequest,
    ReviewController,
    ReviewInputs,
    ReviewResult,
    _RunState,
    _atomic_write_json,
    _directory_fsync_status,
)
from pr_reviewer.specialist_runtime.callbacks import (
    CALLBACK_POOL,
    CallbackCapacityExceeded,
    CallbackTimedOut,
)
from pr_reviewer.specialist_runtime.events import EventJournal
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.coverage import derive_obligations
from pr_reviewer.specialist_runtime.budget import BudgetLedger, RunDeadline
from pr_reviewer.specialist_runtime.policy import RecipePolicy, ReviewPolicy, RuntimeConfig
from pr_reviewer.specialist_runtime.model_gateway import ModelTurnResult
from pr_reviewer.specialist_runtime.scheduler import SessionScheduler
from pr_reviewer.specialist_runtime.session import (
    SessionResult,
    SpecialistSession,
    SpecialistRequestEvent,
)
from pr_reviewer.specialist_runtime.types import (
    BudgetLimits,
    BudgetUsage,
    CandidateFinding,
    CoverageObligation,
    PhaseShares,
    RunPhase,
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
    del tmp_path
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
        artifact_path="specialist-review-artifact.json",
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


def _planner_role(request):
    return _planner(
        request.context["obligations"],
        request.context["topology"],
        request.context["config"],
    )


def _critic_role(request):
    return {
        "decisions": [
            {"candidate_id": item.candidate_id, "action": "keep"}
            for item in request.context["candidates"]
        ]
    }


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
        planner=_planner_role,
        session_factory=factory,
        critic=_critic_role,
        finalizer=lambda request: FinalizerProposal(
            component_ids=("worker",),
            recipe_ids=("delivery",),
        ),
        clock=lambda: 0.0,
        artifact_output_root=tmp_path,
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


@pytest.mark.parametrize(
    ("excluded_recipes", "expected_status"),
    [
        ((), "not_applicable"),
        (("messaging",), "suppressed_by_policy"),
    ],
)
def test_artifact_projects_recipe_accounting_status(
    tmp_path,
    excluded_recipes,
    expected_status,
):
    recipe = RecipePolicy(
        id="messaging",
        title="Messaging",
        objective="Review messaging behavior",
        match={"file_roles_any": ("messaging",)},
        expected_evidence=("tests",),
    )
    inputs = replace(
        _inputs(tmp_path),
        policy=ReviewPolicy(
            recipes=(recipe,),
            exclude={
                "paths": (),
                "components": (),
                "lenses": (),
                "recipes": excluded_recipes,
            },
        ),
    )

    result = _controller(tmp_path).run(inputs)

    recipe_accounting = [
        item for item in result.artifact["coverage"].values()
        if item["origin"] == "recipe-accounting"
    ]
    assert [item["status"] for item in recipe_accounting] == [expected_status]
    assert result.notes
    assert result.artifact["notes"]
    assert result.publishing_ready is True


def test_session_finalization_diagnostics_are_artifact_only(tmp_path):
    diagnostic = {
        "code": "invalid_candidate_finding_references",
        "attempt": "initial",
        "candidate_finding_ids": ("candidate-forged",),
        "omitted_count": 0,
    }

    class DiagnosedSession(_SuccessfulSession):
        def finalize(self):
            return replace(
                super().finalize(),
                finalization_diagnostics=(diagnostic,),
            )

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, coverage
        return DiagnosedSession(
            assignment, evidence_store, obligations, expected_session_id,
        )

    result = _controller(tmp_path, session_factory=factory).run(_inputs(tmp_path))

    assert result.artifact["sessions"][0]["finalization_diagnostics"] == ({
        **diagnostic,
    },)
    assert "candidate-forged" not in result.handoff.markdown


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
    return FinalizerProposal(
        component_ids=("worker",), recipe_ids=("delivery",),
    )


def _controller(tmp_path, **overrides):
    values = {
        "planner": _planner_role,
        "session_factory": _factory,
        "critic": _critic_role,
        "finalizer": _finalizer,
        "clock": lambda: 0.0,
        "artifact_output_root": tmp_path,
    }
    values.update(overrides)
    return ReviewController(**values)


def test_planner_failure_uses_deterministic_assignment_plan(tmp_path):
    def broken_planner(*args):
        raise RuntimeError("planner unavailable")

    result = _controller(tmp_path, planner=broken_planner).run(_inputs(tmp_path))

    assert result.artifact["assignments"][0]["id"].startswith("fallback-")
    assert result.artifact["evaluation_status"] == "complete"
    assert result.artifact["assignment_plan"]["ignored_transformations"]
    assert not any(
        item["component"] == "planner" for item in result.artifact["degradation"]
    )
    assert "planner unavailable" not in result.handoff.markdown
    assert result.publishing_ready is True


def test_optional_planner_absence_keeps_authoritative_base_without_degradation(tmp_path):
    result = _controller(tmp_path, planner=None).run(_inputs(tmp_path))

    assert result.artifact["assignment_plan"]["source"] == "deterministic_base"
    assert not any(
        item["component"] == "planner" for item in result.artifact["degradation"]
    )
    assert result.artifact["assignments"]


def test_invalid_planner_items_are_diagnostic_and_valid_items_still_apply(tmp_path):
    def planner(request):
        base = request.context["base_plan"]
        assignment = base.assignments[0]
        return {"transformations": [
            {
                "kind": "improve",
                "assignment_id": assignment.id,
                "seed_paths": ["invented/outside.py"],
            },
            {
                "kind": "improve",
                "assignment_id": assignment.id,
                "objective": "Trace the worker's reachable delivery behavior.",
            },
        ]}

    result = _controller(tmp_path, planner=planner).run(_inputs(tmp_path))

    assert result.artifact["assignment_plan"]["source"] == (
        "deterministic_base_transformed"
    )
    assert result.artifact["assignment_plan"]["ignored_transformations"]
    assert result.artifact["assignments"][0]["objective"] == (
        "Trace the worker's reachable delivery behavior."
    )
    assert not any(
        item["component"] == "planner" for item in result.artifact["degradation"]
    )


def test_planner_has_no_whole_plan_semantic_repair_loop(tmp_path):
    calls = []

    def planner(request):
        calls.append(request.request_id)
        return {"assignments": []}

    result = _controller(tmp_path, planner=planner).run(_inputs(tmp_path))

    assert calls == ["planner:1"]
    assert result.artifact["assignment_plan"]["source"] == "deterministic_base"
    assert result.artifact["assignments"]


def test_invalid_planner_final_json_keeps_base_without_semantic_repair(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("AI_MODEL", "local-model")
    controller = cli.build_controller(cli.CliConfig.from_env(workspace=tmp_path))
    now = time.monotonic()
    controller.clock = lambda: now
    payloads = []
    responses = iter((
        {
            "choices": [{
                "finish_reason": "length",
                "message": {"role": "assistant", "reasoning_content": "first reasoning"},
            }],
            "usage": {},
        },
        {
            "choices": [{
                "finish_reason": "length",
                "message": {"role": "assistant", "reasoning_content": "second reasoning"},
            }],
            "usage": {},
        },
        {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"assignments":[]}'},
            }],
            "usage": {},
        },
        {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"assignments":[]}'},
            }],
            "usage": {},
        },
    ))

    def transport(_base_url, _api_format, payload, _api_key, _timeout, **_kwargs):
        payloads.append(payload)
        return next(responses)

    controller.planner.gateway.transport = transport
    inputs = _inputs(tmp_path)
    state = _RunState(
        inputs=inputs,
        journal=EventJournal(),
        deadline=RunDeadline(
            now, inputs.config.review_deadline_sec, inputs.config.phase_shares,
        ),
        evidence=EvidenceStore(),
        obligations=derive_obligations(
            inputs.topology, inputs.classification, inputs.policy,
        ),
    )

    plan = controller._plan(state)

    assert len(payloads) == 3
    assert payloads[2]["reasoning_effort"] == "none"
    assert state.plan_source == "deterministic_base"
    assert state.planner_diagnostics
    assert plan.assignments[0].id.startswith("fallback-")


def test_planner_uses_continuations_but_no_whole_plan_semantic_repair(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("AI_MODEL", "local-model")
    monkeypatch.setenv("AI_REASONING_EFFORT", "high")
    controller = cli.build_controller(cli.CliConfig.from_env(workspace=tmp_path))
    now = time.monotonic()
    controller.clock = lambda: now
    payloads = []
    inputs = _inputs(tmp_path)
    state = _RunState(
        inputs=inputs,
        journal=EventJournal(),
        deadline=RunDeadline(
            now, inputs.config.review_deadline_sec, inputs.config.phase_shares,
        ),
        evidence=EvidenceStore(),
        obligations=derive_obligations(
            inputs.topology, inputs.classification, inputs.policy,
        ),
    )
    valid_repair = _planner(state.obligations, inputs.topology, inputs.config)
    responses = iter((
        {
            "choices": [{
                "finish_reason": "length",
                "message": {"role": "assistant", "reasoning_content": "first reasoning"},
            }],
            "usage": {},
        },
        {
            "choices": [{
                "finish_reason": "length",
                "message": {"role": "assistant", "reasoning_content": "second reasoning"},
            }],
            "usage": {},
        },
        {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"assignments":[]}'},
            }],
            "usage": {},
        },
        {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(valid_repair)},
            }],
            "usage": {},
        },
    ))

    def transport(_base_url, _api_format, payload, _api_key, _timeout, **_kwargs):
        payloads.append(payload)
        return next(responses)

    controller.planner.gateway.transport = transport

    plan = controller._plan(state)

    assert len(payloads) == 3
    assert payloads[2]["reasoning_effort"] == "none"
    assert state.plan_source == "deterministic_base"
    assert state.planner_repaired is False
    assert plan.assignments[0].id.startswith("fallback-")


def test_planner_continuation_stops_after_first_structured_response(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("AI_MODEL", "local-model")
    monkeypatch.setenv("AI_REASONING_EFFORT", "high")
    controller = cli.build_controller(cli.CliConfig.from_env(workspace=tmp_path))
    now = time.monotonic()
    controller.clock = lambda: now
    payloads = []
    responses = iter((
        {
            "choices": [{
                "finish_reason": "length",
                "message": {"role": "assistant", "reasoning_content": "initial reasoning"},
            }],
            "usage": {},
        },
        {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"assignments":[]}'},
            }],
            "usage": {},
        },
        {
            "choices": [{
                "finish_reason": "length",
                "message": {"role": "assistant", "reasoning_content": "repair reasoning"},
            }],
            "usage": {},
        },
        {
            "choices": [{
                "finish_reason": "length",
                "message": {"role": "assistant", "reasoning_content": "more repair reasoning"},
            }],
            "usage": {},
        },
    ))

    def transport(_base_url, _api_format, payload, _api_key, _timeout, **_kwargs):
        payloads.append(payload)
        return next(responses)

    controller.planner.gateway.transport = transport
    inputs = _inputs(tmp_path)
    state = _RunState(
        inputs=inputs,
        journal=EventJournal(),
        deadline=RunDeadline(
            now, inputs.config.review_deadline_sec, inputs.config.phase_shares,
        ),
        evidence=EvidenceStore(),
        obligations=derive_obligations(
            inputs.topology, inputs.classification, inputs.policy,
        ),
    )

    plan = controller._plan(state)

    assert len(payloads) == 2
    assert state.plan_source == "deterministic_base"
    assert state.planner_diagnostics
    assert plan.assignments[0].id.startswith("fallback-")


def test_planner_does_not_spend_a_second_request_on_semantic_repair(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("AI_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("AI_MODEL", "local-model")
    controller = cli.build_controller(cli.CliConfig.from_env(workspace=tmp_path))
    now = time.monotonic()
    controller.clock = lambda: now
    payloads = []
    inputs = _inputs(tmp_path)
    state = _RunState(
        inputs=inputs,
        journal=EventJournal(),
        deadline=RunDeadline(
            now, inputs.config.review_deadline_sec, inputs.config.phase_shares,
        ),
        evidence=EvidenceStore(),
        obligations=derive_obligations(
            inputs.topology, inputs.classification, inputs.policy,
        ),
    )
    valid_repair = _planner(state.obligations, inputs.topology, inputs.config)
    responses = iter((
        {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": '{"assignments":[]}'},
            }],
            "usage": {},
        },
        {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(valid_repair)},
            }],
            "usage": {},
        },
    ))

    def transport(_base_url, _api_format, payload, _api_key, _timeout, **_kwargs):
        payloads.append(payload)
        return next(responses)

    controller.planner.gateway.transport = transport

    plan = controller._plan(state)

    assert len(payloads) == 1
    assert state.plan_source == "deterministic_base"
    assert state.planner_repaired is False
    assert plan.assignments[0].id.startswith("fallback-")


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

    result = _controller(tmp_path, session_factory=factory).run(_inputs(tmp_path))

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

    result = _controller(tmp_path,
        session_factory=factory, negotiator=broken_negotiator,
    ).run(_inputs(tmp_path))

    assert len(sessions) == 1
    assert sessions[0].feedback
    assert result.artifact["recipes"]["delivery"]["status"] == "covered"
    assert any(
        event.kind == "negotiation_action" and event.payload["kind"] == "resume"
        for event in result.events
    )
    assert "negotiator" in result.handoff.coverage_warning
    assert "negotiator invalid response" not in result.handoff.markdown


def test_degraded_session_is_promoted_once_across_initial_followup_and_finalization(
    tmp_path,
):
    class DegradedResumeSession(_ResumeSession):
        def explore(self):
            return replace(super().explore(), degraded=True)

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, coverage
        return DegradedResumeSession(
            assignment, evidence_store, obligations, expected_session_id,
        )

    def broken_negotiator(_state):
        raise RuntimeError("negotiator unavailable")

    result = _controller(
        tmp_path,
        session_factory=factory,
        negotiator=broken_negotiator,
    ).run(_inputs(tmp_path))

    specialist_degradations = tuple(
        item for item in result.artifact["degradation"]
        if item["component"].startswith("specialist:")
    )
    assert result.artifact["evaluation_status"] == "degraded"
    assert specialist_degradations == ({
        "component": "specialist:fallback-combined-1",
        "reason": "specialist completed with degraded retained state",
    },)
    assert result.handoff.status == "AI review completed with material coverage limits"
    assert result.handoff.coverage_warning.count("specialist") == 1
    assert "worker-flow" not in result.handoff.markdown


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

    result = _controller(tmp_path, critic=broken_critic).run(replace(
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


def test_empty_candidate_set_skips_critic_without_degradation(tmp_path):
    critic_calls = []

    class NoCandidateSession(_SuccessfulSession):
        def explore(self):
            result = super().explore()
            self.candidate_findings = ()
            return replace(
                result,
                checkpoint=replace(
                    result.checkpoint,
                    candidate_finding_ids=(),
                ),
            )

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, coverage
        return NoCandidateSession(
            assignment, evidence_store, obligations, expected_session_id,
        )

    def critic(request):
        critic_calls.append(request)
        return {"actions": [{"action": "request_verification"}]}

    result = _controller(
        tmp_path,
        session_factory=factory,
        critic=critic,
    ).run(_inputs(tmp_path))

    assert critic_calls == []
    assert result.artifact["accepted_candidates"] == ()
    assert not any(
        item["component"] == "critic"
        for item in result.artifact["degradation"]
    )


@pytest.mark.parametrize(
    "critic_result",
    (
        {"actions": [{"action": "keep"}]},
        {"actions": []},
        {"actions": [
            {"candidate_id": "candidate-delivery", "action": "keep"},
            {"candidate_id": "candidate-delivery", "action": "reject"},
        ]},
        {"actions": [{"candidate_id": "invented", "action": "keep"}]},
        {"actions": [{
            "candidate_id": "candidate-delivery",
            "action": "invented",
        }]},
        {"actions": [{
            "candidate_id": "candidate-delivery",
            "action": "keep",
            "target_id": "candidate-delivery",
        }]},
        {"actions": [{
            "candidate_id": "candidate-delivery",
            "action": "merge",
        }]},
    ),
)
def test_malformed_critic_result_uses_evidence_gated_conservative_fallback(
    tmp_path, critic_result,
):
    result = _controller(
        tmp_path,
        critic=lambda _request: critic_result,
    ).run(_inputs(tmp_path))

    assert [
        item["candidate_id"]
        for item in result.artifact["accepted_candidates"]
    ] == ["candidate-delivery"]
    critic_degradations = [
        item for item in result.artifact["degradation"]
        if item["component"] == "critic"
    ]
    assert len(critic_degradations) == 1
    assert "private" not in critic_degradations[0]["reason"]


def test_duplicate_candidate_ids_reject_every_occurrence_without_first_wins(tmp_path):
    candidates = (
        CandidateFinding(
            candidate_id="collision",
            root_cause_fingerprint="first",
            claim="First claim",
            collector_session_id="input:first",
            model_identity="model-a",
        ),
        CandidateFinding(
            candidate_id="collision",
            root_cause_fingerprint="second",
            claim="Second claim",
            collector_session_id="input:second",
            model_identity="model-b",
        ),
    )
    critic_inputs = []

    def critic(request):
        critic_inputs.extend(request.context["candidates"])
        return {"decisions": []}

    result = _controller(tmp_path, critic=critic).run(replace(
        _inputs(tmp_path), candidate_findings=candidates,
    ))

    collisions = [
        item for item in result.artifact["candidate_dispositions"]
        if item["candidate_id"] == "collision"
    ]
    assert all(item.candidate_id != "collision" for item in critic_inputs)
    assert len(collisions) == 2
    assert {item["occurrence_ref"] for item in collisions} == {
        "input:0", "input:1",
    }
    assert all(item["reason"] == "duplicate-candidate-id" for item in collisions)


def test_finalizer_failure_builds_useful_sparse_handoff_from_controller_state(tmp_path):
    def broken_finalizer(*args):
        raise RuntimeError("finalizer timed out")

    result = _controller(tmp_path, finalizer=broken_finalizer).run(_inputs(tmp_path))

    assert result.handoff.markdown.startswith("## AI Review Handoff")
    assert "Runtime implementation behavior" in result.handoff.change_map
    assert "Component: worker" in result.handoff.change_map
    assert result.handoff.specialist_focuses == ()
    assert result.handoff.recipe_focuses == ("Repository recipe: delivery",)
    assert result.handoff.coverage_boundaries == (
        "Runtime implementation behavior",
    )
    assert result.handoff.thread_status == (
        "1 detail review note prepared for publication; "
        "highest proposed finding severity: minor."
    )
    assert result.handoff.review_emphasis == ("Failure recovery",)
    assert len(result.handoff.review_emphasis) <= 3
    assert "A retry can process one delivery twice" not in result.handoff.markdown
    assert "read_file" not in result.handoff.markdown
    assert "review the complete change" in result.handoff.markdown
    assert any(
        item["component"] == "finalizer" for item in result.artifact["degradation"]
    )


def test_re_review_handoff_does_not_claim_prepared_notes_are_open_threads(tmp_path):
    def broken_finalizer(*_args):
        raise RuntimeError("finalizer timed out")

    inputs = replace(
        _inputs(tmp_path),
        pr_metadata={
            "review_cycle": "re-review",
            "previously_resolved_note_fingerprints": ("candidate-delivery",),
        },
    )
    result = _controller(
        tmp_path,
        finalizer=broken_finalizer,
    ).run(inputs)

    assert result.handoff.thread_status == (
        "1 detail review note prepared for publication; "
        "highest proposed finding severity: minor."
    )
    assert "**Prepared detail notes:**" in result.handoff.markdown
    assert "unresolved" not in result.handoff.markdown.casefold()
    assert "open thread" not in result.handoff.markdown.casefold()
    assert "resolved thread" not in result.handoff.markdown.casefold()


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_deadline_stops_exploration_and_preserves_finalization_reserve(tmp_path):
    clock = _Clock()
    sessions_started = []
    finalizer_calls = []

    def planner(request):
        raw = _planner_role(request)
        clock.now = 90.0
        return raw

    def factory(*args):
        sessions_started.append(True)
        return _factory(*args)

    def finalizer(state):
        finalizer_calls.append(state)
        return _finalizer(state)

    result = _controller(tmp_path,
        planner=planner, session_factory=factory, finalizer=finalizer, clock=clock,
    ).run(_inputs(tmp_path))

    assert sessions_started == []
    assert len(finalizer_calls) == 1
    assert result.artifact["timing"]["finalization_reserve_seconds"] == 10
    assert result.artifact["unknowns"]
    assert "Runtime implementation behavior" in result.handoff.change_map
    assert result.handoff.specialist_focuses == ()
    assert result.handoff.recipe_focuses == ()
    assert result.handoff.thread_status is None
    assert any(
        item["component"] == "deadline" for item in result.artifact["degradation"]
    )


def test_degraded_handoff_rejects_focus_from_failed_planned_assignment(tmp_path):
    def planner(request):
        raw = _planner_role(request)
        raw["assignments"][0]["lenses"] = ["security"]
        return raw

    def broken_factory(*_args):
        raise RuntimeError("specialist unavailable")

    def finalizer(_request):
        return FinalizerProposal(
            component_ids=("worker",),
            specialist_topics=(ReviewOrientationTopic.SECURITY,),
        )

    result = _controller(
        tmp_path,
        planner=planner,
        session_factory=broken_factory,
        finalizer=finalizer,
    ).run(_inputs(tmp_path))

    assert result.artifact["evaluation_status"] == "degraded"
    assert result.handoff.specialist_focuses == ()
    assert "Security-sensitive behavior" not in result.handoff.markdown


def test_failed_specialist_requests_retain_attempt_budget_without_event_replay(tmp_path):
    class FailedRequestSession:
        def __init__(self, assignment, expected_session_id):
            self.assignment = assignment
            self.session_id = expected_session_id
            self.candidate_findings = ()
            self.budget = BudgetLedger(BudgetLimits(
                model_turns=8, tool_calls=8, recoveries=1,
            ))
            self._events = ()

        @property
        def request_events(self):
            return self._events

        def explore(self):
            self.budget.reserve_model_turn()
            request_id = f"{self.session_id}:model:1"
            self._events = (
                SpecialistRequestEvent(request_id, "started", True, None),
                SpecialistRequestEvent(
                    request_id, "failed", True, None, "provider failed",
                ),
            )
            raise RuntimeError("provider failed")

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, evidence_store, coverage, obligations
        return FailedRequestSession(assignment, expected_session_id)

    result = _controller(tmp_path, session_factory=factory).run(_inputs(tmp_path))
    totals = result.artifact["budgets"]["totals"]

    assert totals["specialist_model_requests"] == 2
    assert totals["specialist_model_failed"] == 2
    assert totals["model_turns"] == 2
    assert len(result.artifact["budgets"]["sessions"]) == 2
    assert len({
        event["payload"]["request_id"]
        for event in result.artifact["events"]
        if event["kind"] == "specialist_request_started"
    }) == 2


def test_recoverable_provider_history_failure_reuses_same_session_and_budget(
    tmp_path,
):
    factory_calls = []
    recoveries = []

    class RecoverableSession(_SuccessfulSession):
        def __post_init__(self):
            super().__post_init__()
            self.failed_once = False
            self.budget = BudgetLedger(BudgetLimits(8, 8, 1))

        def explore(self):
            if not self.failed_once:
                self.failed_once = True
                self.budget.reserve_model_turn()
                raise RuntimeError("invalid provider history")
            return super().explore()

        def recover(self, reason):
            recoveries.append((self.session_id, reason, id(self)))
            self.budget.record_recovery(reason)
            return SessionResult(
                session_id=self.session_id,
                state=SessionState.EXPLORING,
                checkpoint=SessionCheckpoint(
                    session_id=self.session_id,
                    state=SessionState.EXPLORING,
                ),
                budget=self.budget.snapshot(),
            )

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, coverage
        session = RecoverableSession(
            assignment, evidence_store, tuple(obligations), expected_session_id,
        )
        factory_calls.append((expected_session_id, id(session)))
        return session

    result = _controller(tmp_path, session_factory=factory).run(_inputs(tmp_path))

    assert len(factory_calls) == 1
    assert recoveries == [(
        factory_calls[0][0], "invalid-provider-history", factory_calls[0][1],
    )]
    assert len(result.artifact["sessions"]) == 1
    assert result.artifact["sessions"][0]["session_id"] == factory_calls[0][0]


def test_hanging_specialist_gateway_is_bounded_and_accounted_in_artifact(tmp_path):
    import threading

    release = threading.Event()

    class HangingGateway:
        def complete(self, request):
            del request
            release.wait(2)
            raise AssertionError("late gateway result must not be admitted")

    inputs = replace(
        _inputs(tmp_path),
        config=replace(_inputs(tmp_path).config, model_request_timeout_sec=0.01),
    )

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del snapshot, obligations
        return SpecialistSession(
            session_id=expected_session_id,
            assignment=assignment,
            conversation=Conversation(system="review"),
            gateway=HangingGateway(),
            execute_tool=lambda name, arguments: {},
            evidence_store=evidence_store,
            coverage=coverage,
            budget=BudgetLedger(inputs.config.session_limits),
            lease=lease,
            request_timeout_sec=inputs.config.model_request_timeout_sec,
            max_tokens=128,
        )

    try:
        result = _controller(tmp_path,
            session_factory=factory, clock=time.monotonic,
        ).run(inputs)
        totals = result.artifact["budgets"]["totals"]

        assert totals["specialist_model_requests"] == 2
        assert totals["specialist_model_timed_out"] == 2
        assert totals["model_turns"] == 2
        assert result.publishing_ready is False or result.verdict == "request_changes"
    finally:
        release.set()
        deadline = time.monotonic() + 1
        while CALLBACK_POOL.in_flight and time.monotonic() < deadline:
            time.sleep(0.005)


def test_phase_cutoff_freezes_in_flight_request_once_before_late_completion(tmp_path):
    import threading

    gateway_entered = threading.Event()
    release = threading.Event()
    clock = _Clock()
    clock.now = time.monotonic()

    class BarrierGateway:
        def complete(self, request):
            del request
            gateway_entered.set()
            release.wait(2)
            text = json.dumps({
                "inspected": [],
                "unresolved": ["OB-code"],
                "hypotheses": [],
                "candidate_finding_ids": [],
                "invariants_evaluated": [],
                "unknowns": ["OB-code"],
                "proposed_next_actions": [],
            })
            return ModelTurnResult(
                response={}, tool_calls=(), text=text, text_source="content",
                finish_reason="stop",
                usage={"prompt_tokens": 3, "completion_tokens": 2},
                request_diagnostics={},
            )

    class BarrierCutoffScheduler(SessionScheduler):
        def __init__(self, **kwargs):
            original_sink = kwargs.get("event_sink")
            self._test_deadline = kwargs["deadline"]
            self._test_phase = RunPhase.INITIAL

            def sink(kind, payload):
                if kind == "session_queued":
                    assert gateway_entered.wait(1)
                    clock.now = self._test_deadline.cutoff_for(self._test_phase)
                if original_sink is not None:
                    original_sink(kind, payload)

            kwargs["event_sink"] = sink
            super().__init__(**kwargs)

        def run_wave(self, assignments, phase):
            self._test_phase = RunPhase(phase)
            if self._test_phase is RunPhase.FOLLOWUP:
                clock.now = self._test_deadline.cutoff_for(RunPhase.FOLLOWUP)
            return super().run_wave(assignments, phase)

    inputs = replace(
        _inputs(tmp_path),
        config=replace(
            _inputs(tmp_path).config,
            review_deadline_sec=100,
            model_request_timeout_sec=30,
        ),
    )

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del snapshot, obligations
        return SpecialistSession(
            session_id=expected_session_id,
            assignment=assignment,
            conversation=Conversation(system="review"),
            gateway=BarrierGateway(),
            execute_tool=lambda name, arguments: {},
            evidence_store=evidence_store,
            coverage=coverage,
            budget=BudgetLedger(inputs.config.session_limits),
            lease=lease,
            request_timeout_sec=inputs.config.model_request_timeout_sec,
            max_tokens=128,
        )

    try:
        result = _controller(
            tmp_path,
            session_factory=factory,
            scheduler_type=BarrierCutoffScheduler,
            clock=clock,
        ).run(inputs)
        attempts = result.artifact["budgets"]["request_attempts"]
        assert attempts, (
            json.dumps(result.artifact["budgets"], sort_keys=True),
            json.dumps(result.artifact["degradation"], sort_keys=True),
            tuple(event["kind"] for event in result.artifact["events"]),
        )
        request_id = attempts[0]["request_id"]
        request_events = tuple(
            event for event in result.artifact["events"]
            if event["payload"].get("request_id") == request_id
        )
        frozen_artifact = json.dumps(result.artifact, sort_keys=True)

        assert len(attempts) == 1
        assert attempts[0]["status"] == "timed_out_at_phase_cutoff"
        assert attempts[0]["in_flight"] is True
        assert tuple(event["kind"] for event in request_events) == (
            "specialist_request_started",
            "specialist_request_timed_out_at_phase_cutoff",
        )
        assert result.artifact["budgets"]["totals"]["model_turns"] == 1
        assert result.artifact["budgets"]["totals"]["specialist_model_cutoff"] == 1
        assert any(
            event["kind"] == "session_in_flight"
            for event in result.artifact["events"]
        )

        release.set()
        deadline = time.monotonic() + 1
        while CALLBACK_POOL.in_flight and time.monotonic() < deadline:
            time.sleep(0.005)
        assert json.dumps(result.artifact, sort_keys=True) == frozen_artifact
    finally:
        release.set()


def test_exhausted_schema_repair_cannot_inflate_artifact_model_turns(tmp_path):
    class FinalizationGateway:
        def __init__(self):
            self.requests = []

        def complete(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                text = json.dumps({
                    "inspected": [],
                    "unresolved": ["OB-code"],
                    "hypotheses": [],
                    "candidate_finding_ids": [],
                    "invariants_evaluated": [],
                    "unknowns": ["OB-code"],
                    "proposed_next_actions": [],
                })
            else:
                text = "invalid-final-json"
            return ModelTurnResult(
                response={}, tool_calls=(), text=text, text_source="content",
                finish_reason="stop",
                usage={"prompt_tokens": 3, "completion_tokens": 2},
                request_diagnostics={},
            )

    gateway = FinalizationGateway()
    inputs = replace(
        _inputs(tmp_path),
        config=replace(
            _inputs(tmp_path).config,
            max_followup_sessions=0,
            session_limits=BudgetLimits(
                model_turns=2, tool_calls=8, recoveries=1,
            ),
        ),
    )

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del snapshot, obligations
        return SpecialistSession(
            session_id=expected_session_id,
            assignment=assignment,
            conversation=Conversation(system="review"),
            gateway=gateway,
            execute_tool=lambda name, arguments: {},
            evidence_store=evidence_store,
            coverage=coverage,
            budget=BudgetLedger(inputs.config.session_limits),
            lease=lease,
            request_timeout_sec=inputs.config.model_request_timeout_sec,
            max_tokens=128,
        )

    def record_unknowns(request):
        negotiation_state = request.context["negotiation_state"]
        obligations = tuple(
            item for item in negotiation_state.obligations if item.mandatory
        )
        return {"actions": [{
            "kind": "record_unknown",
            "obligation_ids": [item.id for item in obligations],
            "expected_evidence": sorted({
                category
                for item in obligations
                for category in item.required_evidence_categories
            }),
            "estimated_turns": 0,
            "reason": "retain unresolved coverage for finalization",
        }]}

    result = _controller(
        tmp_path,
        session_factory=factory,
        negotiator=record_unknowns,
        clock=time.monotonic,
    ).run(inputs)
    session_budgets = result.artifact["budgets"]["sessions"]
    attempts = result.artifact["budgets"]["request_attempts"]

    assert len(gateway.requests) == 2, result.artifact["degradation"]
    assert len(attempts) == 2
    assert all(item["status"] == "completed" for item in attempts)
    assert max(item["model_turns"] for item in session_budgets.values()) == 2
    assert all(
        item["model_turns"] <= inputs.config.session_limits.model_turns
        for item in session_budgets.values()
    )
    assert result.artifact["budgets"]["totals"]["model_turns"] == 2
    assert result.artifact["budgets"]["totals"]["model_turns"] <= (
        len(session_budgets) * inputs.config.session_limits.model_turns
    )


def test_repeated_dangling_final_references_promote_top_level_specialist_degradation(
    tmp_path,
):
    class DanglingFinalGateway:
        def __init__(self):
            self.requests = []

        def complete(self, request):
            self.requests.append(request)
            request_number = len(self.requests)
            if request_number == 1:
                call = {
                    "id": "read-worker",
                    "name": "read_file",
                    "arguments": json.dumps({"path": "src/worker.py"}),
                }
                return ModelTurnResult(
                    response={}, tool_calls=(call,), text="", text_source="none",
                    finish_reason="tool_calls",
                    usage={"prompt_tokens": 3, "completion_tokens": 2},
                    request_diagnostics={},
                )
            if request_number == 2:
                text = json.dumps({
                    "inspected": ["src/worker.py"],
                    "unresolved": [],
                    "hypotheses": [],
                    "candidate_finding_ids": [],
                    "invariants_evaluated": [],
                    "unknowns": [],
                    "proposed_next_actions": [],
                })
            else:
                text = json.dumps({
                    "summary": "candidate-forged-private-detail",
                    "recommendation": "approve",
                    "candidate_finding_ids": ["candidate-forged-private-detail"],
                    "evidence_ids": [],
                    "unknowns": [],
                })
            return ModelTurnResult(
                response={}, tool_calls=(), text=text, text_source="content",
                finish_reason="stop",
                usage={"prompt_tokens": 3, "completion_tokens": 2},
                request_diagnostics={},
            )

    gateway = DanglingFinalGateway()
    inputs = replace(
        _inputs(tmp_path),
        config=replace(
            _inputs(tmp_path).config,
            session_limits=BudgetLimits(
                model_turns=4, tool_calls=8, recoveries=1,
            ),
        ),
    )

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del snapshot, obligations
        return SpecialistSession(
            session_id=expected_session_id,
            assignment=assignment,
            conversation=Conversation(system="review"),
            gateway=gateway,
            execute_tool=lambda name, arguments: {
                "tool": name,
                "status": "ok",
                "result": {"content": "def process(): pass"},
            },
            evidence_store=evidence_store,
            coverage=coverage,
            budget=BudgetLedger(inputs.config.session_limits),
            lease=lease,
            request_timeout_sec=inputs.config.model_request_timeout_sec,
            max_tokens=128,
            clock=lambda: 0.0,
        )

    result = _controller(tmp_path, session_factory=factory).run(inputs)

    assert len(gateway.requests) == 4
    assert result.artifact["evaluation_status"] == "degraded"
    assert tuple(result.artifact["degradation"]) == ({
        "component": "specialist:fallback-combined-1",
        "reason": "specialist completed with degraded retained state",
    },)
    assert result.artifact["sessions"][0]["degraded"] is True
    assert result.handoff.status == "AI review completed with material coverage limits"
    assert "Affected stages: specialist." in result.handoff.markdown
    assert "candidate-forged-private-detail" not in result.handoff.markdown


def test_artifact_write_failure_preserves_prior_file_and_valid_result(tmp_path):
    artifact_path = tmp_path / "specialist-review-artifact.json"
    artifact_path.write_text('{"prior":true}\n', encoding="utf-8")

    def broken_writer(path, artifact):
        del path, artifact
        raise OSError("disk full token=very-secret-value")

    result = _controller(tmp_path, artifact_writer=broken_writer).run(_inputs(tmp_path))

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

    def planner(request):
        items = request.context["obligations"]
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
        directory.mkdir(parents=True, exist_ok=True)
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
            critic=lambda request: {"decisions": []},
            finalizer=_finalizer,
            clock=lambda: clock_origin,
            obligation_deriver=deriver,
            artifact_output_root=tmp_path,
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

    result = _controller(tmp_path, ).run(replace(
        inputs, source_access_requests=(request,),
    ))

    assert result.artifact["source_access_requests"][0]["host"] == "docs.example.com"
    assert any(event.kind == "source_access_request" for event in result.events)


def test_unexpected_controller_failure_returns_notice_without_fabricated_blocker(tmp_path):
    def broken_deriver(*args):
        del args
        raise RuntimeError("topology unavailable")

    result = ReviewController(
        planner=_planner_role,
        session_factory=_factory,
        critic=lambda request: {"decisions": []},
        finalizer=_finalizer,
        clock=lambda: 0.0,
        obligation_deriver=broken_deriver,
        artifact_output_root=tmp_path,
    ).run(_inputs(tmp_path))

    assert result.verdict == "notice"
    assert result.publishing_ready is False
    assert "Approve" not in result.handoff.markdown
    assert result.artifact["accepted_candidates"] == ()
    assert result.artifact["verdict"]["blocking_finding_ids"] == ()


def test_planner_receives_typed_phase_lease_request(tmp_path):
    requests = []

    def planner(request):
        requests.append(request)
        return _planner(
            request.context["obligations"],
            request.context["topology"],
            request.context["config"],
        )

    result = _controller(tmp_path, planner=planner).run(replace(
        _inputs(tmp_path),
        pr_metadata={"title": "Preserve retry intent", "body": "No duplicate work"},
    ))

    assert result.artifact["assignment_plan"]["source"] == "deterministic_base"
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
            review_deadline_sec=0.5,
            model_request_timeout_sec=0.01,
        ),
    )
    started = time.monotonic()
    result = _controller(tmp_path, planner=hanging_planner, clock=time.monotonic).run(inputs)
    elapsed = time.monotonic() - started
    before = json.dumps(result.artifact, sort_keys=True)
    release.set()
    assert returned.wait(1)
    time.sleep(0.02)

    assert elapsed < 1
    assert result.artifact["assignment_plan"]["source"] == "deterministic_base"
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
    result = _controller(tmp_path,
        session_factory=factory, clock=time.monotonic,
    ).run(inputs)
    assert started.is_set(), result.artifact["degradation"]
    before = json.dumps(result.artifact, sort_keys=True)
    release.set()
    time.sleep(0.05)

    assert finalized.is_set() is False
    assert json.dumps(result.artifact, sort_keys=True) == before
    assert result.artifact["evidence"] == ()
    assert result.artifact["sessions"] == ()


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

    result = _controller(tmp_path, session_factory=factory).run(_inputs(tmp_path))

    assert result.artifact["sessions"] == ()
    assert result.artifact["evidence"] == ()
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

    result = _controller(tmp_path,
        session_factory=factory,
    ).run(replace(
        _inputs(tmp_path),
        config=replace(_inputs(tmp_path).config, max_followup_sessions=0),
    ))

    assert result.artifact["sessions"] == ()
    assert result.artifact["evidence"] == ()
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

    result = _controller(tmp_path,
        session_factory=factory,
        negotiator=lambda state: (_ for _ in ()).throw(RuntimeError("fallback")),
    ).run(_inputs(tmp_path))

    assert result.artifact["recipes"]["delivery"]["status"] == "covered"
    assert [lease.phase.value for lease in leases[:2]] == ["initial", "followup"]
    assert leases[1].deadline_at > leases[0].deadline_at


def test_artifact_is_schema_v2_strict_json_with_matching_reference(tmp_path):
    result = _controller(tmp_path, ).run(_inputs(tmp_path))

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

    result = _controller(tmp_path, session_factory=factory).run(_inputs(tmp_path))
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

    result = _controller(tmp_path,
        finalizer=malicious_finalizer,
        planner=lambda *args: (_ for _ in ()).throw(RuntimeError("degraded")),
    ).run(_inputs(tmp_path))

    assert "invented" not in result.handoff.markdown
    assert result.artifact["evaluation_status"] == "degraded"
    assert result.handoff.coverage_warning


def test_finalizer_proposal_selects_only_authorized_orientation(tmp_path):
    def finalizer(request):
        assert request.context["pr_metadata"] == {}
        assert request.context["policy"].version == 2
        return FinalizerProposal(
            component_ids=("worker", "invented"),
            recipe_ids=("delivery", "invented"),
            review_emphasis_topics=(
                ReviewOrientationTopic.FAILURE_RECOVERY,
                ReviewOrientationTopic.SECURITY,
            ),
            recommendation="approve",
        )

    result = _controller(tmp_path, finalizer=finalizer).run(_inputs(tmp_path))

    assert "Component: worker" in result.handoff.markdown
    assert "invented" not in result.handoff.markdown
    assert "Failure recovery" in result.handoff.markdown
    assert "Security" not in result.handoff.markdown
    assert result.handoff.recommendation in {"Approve", "Request changes"}
    assert result.handoff.review_emphasis == ("Failure recovery",)
    assert result.artifact["handoff"]["status"] == "AI review complete"
    assert result.artifact["handoff"]["recipe_focuses"] == (
        "Repository recipe: delivery",
    )
    event = next(
        item for item in result.artifact["events"]
        if item["kind"] == "finalizer_proposal_applied"
    )
    assert event["payload"]["coverage_boundary_topics"] == ()
    assert event["payload"]["review_emphasis_topics"] == ("failure_recovery",)


def test_finalizer_filters_invalid_topics_without_discarding_valid_selection(
    tmp_path,
):
    def finalizer(_request):
        return {
            "component_ids": ["worker"],
            "recipe_ids": ["delivery"],
            "change_topics": "wrong-container",
            "review_emphasis_topics": [
                "failure_recovery",
                "invented-private-topic",
                "FAILURE_RECOVERY",
            ],
        }

    result = _controller(tmp_path, finalizer=finalizer).run(_inputs(tmp_path))

    assert result.handoff.review_emphasis == ("Failure recovery",)
    assert "Component: worker" in result.handoff.markdown
    assert "invented-private-topic" not in result.handoff.markdown
    assert not any(
        item["component"] == "finalizer"
        for item in result.artifact["degradation"]
    )


def test_repository_publishing_policy_prevents_caller_broadening(tmp_path):
    policy = replace(_policy(), publishing={
        "allowed_modes": ("comment",),
        "allow_approve": False,
    })
    result = _controller(tmp_path, ).run(replace(
        _inputs(tmp_path),
        policy=policy,
        publishing_mode="review_verdict",
        allow_approve=True,
    ))

    assert result.artifact["publishing"]["mode"] == "comment"
    assert result.artifact["publishing"]["allow_approve"] is False
    assert result.verdict == "request_changes"
    assert result.notes == ()


def test_missing_repository_publishing_policy_never_grants_approval(tmp_path):
    result = _controller(tmp_path, ).run(replace(
        _inputs(tmp_path),
        policy=replace(_policy(), publishing={}),
        publishing_mode="review_verdict",
        allow_approve=True,
    ))

    assert result.artifact["publishing"]["allow_approve"] is False
    assert result.verdict == "request_changes"


def test_repository_publishing_mode_without_allow_flag_never_grants_approval(tmp_path):
    result = _controller(tmp_path, ).run(replace(
        _inputs(tmp_path),
        policy=replace(
            _policy(), publishing={"allowed_modes": ("review_verdict",)},
        ),
        publishing_mode="review_verdict",
        allow_approve=True,
    ))

    assert result.artifact["publishing"]["allow_approve"] is False
    assert result.verdict == "request_changes"


def test_caller_can_narrow_repository_publishing_policy(tmp_path):
    policy = replace(_policy(), publishing={
        "allowed_modes": ("review_verdict",),
        "allow_approve": True,
    })
    result = _controller(tmp_path, ).run(replace(
        _inputs(tmp_path),
        policy=policy,
        publishing_mode="review_comment",
        allow_approve=True,
    ))

    assert result.artifact["publishing"]["mode"] == "review_comment"
    assert result.artifact["publishing"]["allow_approve"] is True
    assert result.verdict == "approve"


def test_detailed_publishing_is_not_ready_when_a_required_note_is_unauthorized(tmp_path):
    result = _controller(tmp_path, ).run(replace(
        _inputs(tmp_path),
        verification_requests=({"kind": "invalid"},),
    ))

    assert result.artifact["publishing"]["required_note_count"] > len(result.notes)
    assert result.publishing_ready is False
    publish_phase = next(
        item for item in result.artifact["phases"]
        if item["phase"] == "publish_ready"
    )
    assert publish_phase["status"] == "degraded"


def test_comment_mode_is_ready_without_detailed_notes(tmp_path):
    policy = replace(_policy(), publishing={
        "allowed_modes": ("comment",), "allow_approve": False,
    })
    result = _controller(tmp_path, ).run(replace(
        _inputs(tmp_path),
        policy=policy,
        publishing_mode="comment",
        verification_requests=({"kind": "invalid"},),
    ))

    assert result.notes == ()
    assert result.publishing_ready is True


def test_documented_allowed_modes_intersects_with_caller_mode(tmp_path):
    policy = replace(_policy(), publishing={
        "allowed_modes": ("review_comment",),
        "allow_approve": False,
    })

    broadened = _controller(tmp_path).run(replace(
        _inputs(tmp_path),
        policy=policy,
        publishing_mode="review_verdict",
        allow_approve=True,
    ))
    narrowed = _controller(tmp_path).run(replace(
        _inputs(tmp_path),
        policy=policy,
        publishing_mode="comment",
        allow_approve=True,
    ))

    assert broadened.artifact["publishing"]["mode"] == "review_comment"
    assert broadened.artifact["publishing"]["allow_approve"] is False
    assert narrowed.artifact["publishing"]["mode"] == "comment"


def test_output_path_must_be_beneath_controller_owned_root(tmp_path):
    outside = tmp_path.parent / "must-not-overwrite.json"
    outside.write_text('{"owner":"user"}\n', encoding="utf-8")
    inputs = replace(_inputs(tmp_path), artifact_path=outside)

    result = _controller(tmp_path, ).run(inputs)

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
        "clientSecret": "value-d",
        "githubToken": "value-e",
        "nan": math.nan,
        "positive_infinity": math.inf,
        "hostile": Hostile(),
    })
    payload = dict(event.payload)

    assert payload["access-token"] == "[REDACTED]"
    assert payload["client_secret_value"] == "[REDACTED]"
    assert payload["AUTHORIZATION_HEADER"] == "[REDACTED]"
    assert payload["clientSecret"] == "[REDACTED]"
    assert payload["githubToken"] == "[REDACTED]"
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


def test_event_observer_orphan_cap_is_process_global_across_journals():
    import threading

    release = threading.Event()
    entered = []

    def observer(event):
        entered.append(event.sequence)
        release.wait(1)

    journals = [EventJournal(observer, observer_timeout_sec=0.01) for _ in range(5)]
    try:
        for journal in journals:
            journal.emit("event", {})
        assert len(entered) == 4
        assert any("capacity" in item for item in journals[-1].external_errors())
    finally:
        release.set()


def test_role_and_session_callback_orphans_share_one_process_global_cap():
    import threading

    release = threading.Event()
    entered = []

    def hanging_callback():
        entered.append(len(entered))
        release.wait(1)

    try:
        for index in range(CALLBACK_POOL.capacity):
            with pytest.raises(CallbackTimedOut):
                CALLBACK_POOL.run(
                    hanging_callback,
                    timeout_sec=0.005,
                    name=f"mixed-role-session-{index}",
                )
        with pytest.raises(CallbackCapacityExceeded):
            CALLBACK_POOL.run(
                lambda: None,
                timeout_sec=0.01,
                name="later-controller",
            )
        assert len(entered) == CALLBACK_POOL.capacity
    finally:
        release.set()
        deadline = time.monotonic() + 1
        while CALLBACK_POOL.in_flight and time.monotonic() < deadline:
            time.sleep(0.005)


def test_reusing_controller_does_not_reuse_prior_run_evidence(tmp_path):
    shared = EvidenceStore()
    controller = _controller(tmp_path, evidence_store=shared)

    first = controller.run(_inputs(tmp_path))
    second = controller.run(replace(
        _inputs(tmp_path),
        head_sha="c" * 40,
        artifact_path="second.json",
    ))

    assert len(first.artifact["evidence"]) == len(second.artifact["evidence"])
    assert shared.snapshot().records == ()


def test_emergency_projection_is_terminal_schema_valid_and_not_publishable(tmp_path):
    class BrokenProjectionController(ReviewController):
        def _artifact(self, state, path):
            del state, path
            raise KeyboardInterrupt("projection interrupted token=private")

    controller = BrokenProjectionController(
        planner=_planner_role,
        session_factory=_factory,
        critic=_critic_role,
        finalizer=_finalizer,
        clock=lambda: 0.0,
        artifact_output_root=tmp_path,
    )

    result = controller.run(_inputs(tmp_path))

    assert result.artifact["schema_version"] == 2
    assert result.artifact["evaluation_status"] == "degraded"
    assert result.artifact["publishing"]["ready"] is False
    assert result.publishing_ready is False
    assert result.artifact["assignments"]
    assert result.artifact["sessions"]
    assert result.artifact["evidence"]
    assert set(
        item["status"] for item in result.artifact["coverage"].values()
    ) <= {
        "covered", "partially_covered", "unresolved", "not_applicable",
        "suppressed_by_policy",
    }
    assert "private" not in json.dumps(result.artifact)
    json.dumps(result.artifact, allow_nan=False)


def test_artifact_root_identity_swap_cannot_redirect_atomic_write(tmp_path):
    root = tmp_path / "owned-root"
    moved = tmp_path / "moved-owned-root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    class SwapRootController(ReviewController):
        swapped = False

        def _artifact(self, state, path):
            artifact = super()._artifact(state, path)
            if not self.swapped:
                os.rename(root, moved)
                try:
                    os.symlink(outside, root, target_is_directory=True)
                except OSError as exc:
                    os.rename(moved, root)
                    pytest.skip(f"directory links are unavailable: {exc}")
                self.swapped = True
            return artifact

    controller = SwapRootController(
        planner=_planner_role,
        session_factory=_factory,
        critic=_critic_role,
        finalizer=_finalizer,
        clock=lambda: 0.0,
        artifact_output_root=root,
    )
    result = controller.run(_inputs(tmp_path))

    assert not (outside / "specialist-review-artifact.json").exists()
    assert result.artifact_write_error
    assert result.publishing_ready is False


def test_artifact_target_link_is_rejected_before_write(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"owner":"user"}\n', encoding="utf-8")
    target = tmp_path / "specialist-review-artifact.json"
    try:
        os.symlink(outside, target)
    except OSError as exc:
        pytest.skip(f"file links are unavailable: {exc}")

    result = _controller(tmp_path, ).run(_inputs(tmp_path))

    assert outside.read_text(encoding="utf-8") == '{"owner":"user"}\n'
    assert result.artifact_write_error
    assert result.publishing_ready is False


def test_post_replace_directory_fsync_failure_is_durability_warning(
    tmp_path, monkeypatch,
):
    if os.name == "nt":
        monkeypatch.setattr(
            os, "fsync", lambda descriptor: (_ for _ in ()).throw(
                OSError(f"directory fsync unavailable for {descriptor}")
            ),
        )
        assert _directory_fsync_status(123) == "written_durability_warning"
        return

    directory_fd = os.open(
        tmp_path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    original_fsync = os.fsync

    def selective_fsync(descriptor):
        if descriptor == directory_fd:
            raise OSError("directory fsync unavailable")
        return original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", selective_fsync)
    try:
        status = _atomic_write_json(
            tmp_path / "artifact.json",
            {"safe": True},
            directory_fd=directory_fd,
            root_identity=(
                int(os.fstat(directory_fd).st_dev),
                int(os.fstat(directory_fd).st_ino),
            ),
        )
    finally:
        os.close(directory_fd)

    assert status == "written_durability_warning"
    assert json.loads((tmp_path / "artifact.json").read_text()) == {"safe": True}


def test_terminal_artifact_contains_complete_sanitized_event_journal_digest(tmp_path):
    result = _controller(tmp_path, ).run(_inputs(tmp_path))
    events = result.artifact["events"]
    encoded = json.dumps(
        events, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")

    assert result.artifact["event_journal"] == {
        "count": len(events),
        "digest": hashlib.sha256(encoded).hexdigest(),
    }
    assert tuple(item["sequence"] for item in events) == tuple(
        range(1, len(events) + 1)
    )
    assert tuple(
        {"sequence": item["sequence"], "kind": item["kind"]}
        for item in events
    ) == result.artifact["event_references"]


def test_result_artifact_is_recursively_immutable(tmp_path):
    result = _controller(tmp_path, ).run(_inputs(tmp_path))

    with pytest.raises(TypeError, match="immutable"):
        result.artifact["repository"] = "other/repository"
    with pytest.raises(TypeError, match="immutable"):
        result.artifact["publishing"]["ready"] = False
    assert isinstance(result.artifact["events"], tuple)


def test_mismatched_evidence_seed_degrades_to_terminal_result(tmp_path):
    seed = EvidenceSeed(
        repository="other/repository",
        head_sha="c" * 40,
        snapshot=EvidenceStore().snapshot(),
    )
    result = _controller(tmp_path, evidence_seed=seed).run(_inputs(tmp_path))

    assert result.publishing_ready is False
    assert result.artifact["phases"][-1]["phase"] == "complete"
    assert any(
        item["component"] == "controller"
        for item in result.artifact["degradation"]
    )


def test_clock_failure_is_projected_as_terminal_degradation(tmp_path):
    def broken_clock():
        raise KeyboardInterrupt("clock secret=private")

    result = _controller(tmp_path, clock=broken_clock).run(_inputs(tmp_path))

    assert result.publishing_ready is False
    assert result.artifact["phases"][-1]["status"] == "degraded"
    assert "private" not in json.dumps(result.artifact)


def test_obligation_derivation_failure_marks_current_phase_and_skips_later(tmp_path):
    result = _controller(tmp_path,
        obligation_deriver=lambda *args: (_ for _ in ()).throw(
            RuntimeError("derivation failed")
        ),
    ).run(_inputs(tmp_path))
    phases = {item["phase"]: item["status"] for item in result.artifact["phases"]}

    assert phases["precheck"] == "complete"
    assert phases["planning"] == "degraded"
    assert phases["initial"] == "skipped"
    assert phases["publish_ready"] == "skipped"
    assert phases["complete"] == "degraded"


def test_scheduler_failure_marks_initial_degraded_and_skips_publish(tmp_path):
    class BrokenScheduler:
        def __init__(self, **kwargs):
            del kwargs

        def run_wave(self, assignments, phase):
            del assignments, phase
            raise RuntimeError("scheduler failed")

    result = _controller(tmp_path, scheduler_type=BrokenScheduler).run(_inputs(tmp_path))
    phases = {item["phase"]: item["status"] for item in result.artifact["phases"]}

    assert phases["planning"] == "complete"
    assert phases["initial"] == "degraded"
    assert phases["followup"] == "skipped"
    assert phases["publish_ready"] == "skipped"


def test_unexpected_finalization_failure_never_completes_publish_ready(tmp_path):
    class BrokenFinalizationController(ReviewController):
        def _finalize_products(self, state):
            del state
            raise KeyboardInterrupt("finalization failed")

    result = BrokenFinalizationController(
        planner=_planner_role,
        session_factory=_factory,
        critic=_critic_role,
        finalizer=_finalizer,
        clock=lambda: 0.0,
        artifact_output_root=tmp_path,
    ).run(_inputs(tmp_path))
    phases = {item["phase"]: item["status"] for item in result.artifact["phases"]}

    assert phases["followup"] == "complete"
    assert phases["finalization"] == "degraded"
    assert phases["publish_ready"] == "skipped"
    assert result.publishing_ready is False


def test_hostile_evidence_snapshot_on_primary_and_emergency_uses_last_resort(tmp_path):
    class HostileEvidenceStore(EvidenceStore):
        def snapshot(self):
            raise KeyboardInterrupt("evidence secret=private")

    result = _controller(tmp_path,
        evidence_store_factory=HostileEvidenceStore,
    ).run(_inputs(tmp_path))

    assert result.artifact["schema_version"] == 2
    assert result.artifact["publishing"]["ready"] is False
    assert result.artifact["coverage"]
    assert "private" not in json.dumps(result.artifact)


def test_hostile_validator_cannot_escape_last_resort_terminal_shell(tmp_path):
    class HostileValidatorController(ReviewController):
        @staticmethod
        def _validate_artifact(artifact):
            del artifact
            raise KeyboardInterrupt("validator secret=private")

    result = HostileValidatorController(
        planner=_planner_role,
        session_factory=_factory,
        critic=_critic_role,
        finalizer=_finalizer,
        clock=lambda: 0.0,
        artifact_output_root=tmp_path,
    ).run(_inputs(tmp_path))

    assert result.artifact["schema_version"] == 2
    assert result.verdict_source == "controller-terminal-fallback"
    assert result.publishing_ready is False
    assert "private" not in json.dumps(result.artifact)


def test_metaclass_hostile_validator_cannot_escape_last_resort_terminal_shell(tmp_path):
    class HostileExceptionMeta(type):
        def __getattribute__(cls, name):
            if name == "__name__":
                raise KeyboardInterrupt("hostile type name secret=private")
            return super().__getattribute__(name)

    class HostileTerminalError(BaseException, metaclass=HostileExceptionMeta):
        def __str__(self):
            raise KeyboardInterrupt("hostile str secret=private")

        def __repr__(self):
            raise KeyboardInterrupt("hostile repr secret=private")

    class HostileValidatorController(ReviewController):
        @staticmethod
        def _validate_artifact(artifact):
            del artifact
            raise HostileTerminalError()

    result = HostileValidatorController(
        planner=_planner_role,
        session_factory=_factory,
        critic=_critic_role,
        finalizer=_finalizer,
        clock=lambda: 0.0,
        artifact_output_root=tmp_path,
    ).run(_inputs(tmp_path))

    assert isinstance(result, ReviewResult)
    assert result.artifact["schema_version"] == 2
    assert result.verdict_source == "controller-terminal-fallback"
    assert result.publishing_ready is False
    assert result.artifact_write_error == "BaseException: [unserializable]"
    assert "private" not in json.dumps(result.artifact)


def test_last_resort_formats_metaclass_hostile_exception_without_escaping(tmp_path):
    class HostileExceptionMeta(type):
        def __getattribute__(cls, name):
            if name == "__name__":
                raise KeyboardInterrupt("hostile terminal type name")
            return super().__getattribute__(name)

    class HostileTerminalError(BaseException, metaclass=HostileExceptionMeta):
        def __str__(self):
            raise KeyboardInterrupt("hostile terminal str")

        def __repr__(self):
            raise KeyboardInterrupt("hostile terminal repr")

    class BrokenController(ReviewController):
        def _run_impl(self, inputs, terminal_capture):
            del inputs, terminal_capture
            raise HostileTerminalError()

    result = BrokenController(artifact_output_root=tmp_path).run(_inputs(tmp_path))

    assert isinstance(result, ReviewResult)
    assert result.verdict_source == "controller-terminal-fallback"
    assert result.publishing_ready is False
    assert result.artifact_write_error == "BaseException: [unserializable]"


def test_hostile_writer_and_observer_never_escape_terminal_result(tmp_path):
    def writer(path, artifact):
        del path, artifact
        raise KeyboardInterrupt("writer secret=private")

    def observer(event):
        del event
        raise KeyboardInterrupt("observer secret=private")

    result = _controller(tmp_path,
        artifact_writer=writer,
        event_sink=observer,
    ).run(_inputs(tmp_path))

    assert result.artifact["schema_version"] == 2
    assert result.artifact["publishing"]["ready"] is False
    assert result.artifact_write_error
    assert "private" not in json.dumps(result.artifact)


def test_role_callback_receives_only_detached_bounded_role_request(tmp_path):
    inputs = _inputs(tmp_path)
    mutable_topology = {
        **inputs.topology,
        "changed_files": list(inputs.topology["changed_files"]),
    }
    inputs = replace(inputs, topology=mutable_topology)
    observed = []

    def planner(*args):
        observed.append(args)
        mutable_topology["changed_files"].append("src/late.py")
        request = args[0]
        assert tuple(request.context["topology"]["changed_files"]) == (
            "src/worker.py",
        )
        return _planner(
            request.context["obligations"],
            request.context["topology"],
            request.context["config"],
        )

    result = _controller(tmp_path, planner=planner).run(inputs)

    assert result.artifact["assignment_plan"]["source"] == "deterministic_base"
    assert len(observed) == 1
    assert len(observed[0]) == 1
    assert isinstance(observed[0][0], RoleRequest)


@dataclass
class _HangingHookSession(_ResumeSession):
    hook: str
    entered: object
    release: object
    finalized: object

    def update_lease(self, lease):
        if self.hook == "update_lease" and lease.phase.value == "followup":
            self.entered.set()
            self.release.wait(2)
        super().update_lease(lease)

    def apply_coverage_feedback(self, gaps):
        if self.hook == "feedback":
            self.entered.set()
            self.release.wait(2)
        super().apply_coverage_feedback(gaps)

    def finalize(self):
        if self.hook == "finalize":
            self.entered.set()
            self.release.wait(2)
        self.finalized.set()
        return super().finalize()


def _run_hanging_hook_case(tmp_path, hook):
    import threading

    entered = threading.Event()
    release = threading.Event()
    finalized = threading.Event()

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del snapshot, coverage
        session = _HangingHookSession(
            assignment, evidence_store, obligations, expected_session_id,
            hook, entered, release, finalized,
        )
        session.lease = lease
        return session

    inputs = replace(
        _inputs(tmp_path),
        config=replace(
            _inputs(tmp_path).config,
            model_request_timeout_sec=0.02,
            max_followup_sessions=0 if hook == "update_lease" else 1,
        ),
    )
    started = time.monotonic()
    result = _controller(tmp_path,
        session_factory=factory,
        negotiator=lambda request: (_ for _ in ()).throw(RuntimeError("fallback")),
        clock=time.monotonic,
    ).run(inputs)
    elapsed = time.monotonic() - started
    artifact_before_release = json.dumps(result.artifact, sort_keys=True)
    release.set()
    time.sleep(0.04)
    return result, elapsed, entered, finalized, artifact_before_release


def test_hanging_feedback_hook_is_bounded_and_session_is_quarantined(tmp_path):
    result, elapsed, entered, finalized, before = _run_hanging_hook_case(
        tmp_path, "feedback",
    )

    assert entered.is_set()
    assert elapsed < 0.5
    assert finalized.is_set() is False
    assert json.dumps(result.artifact, sort_keys=True) == before
    assert any(
        item["component"].startswith("specialist_hook:")
        for item in result.artifact["degradation"]
    )


def test_hanging_lease_update_hook_is_bounded_and_never_finalized(tmp_path):
    result, elapsed, entered, finalized, before = _run_hanging_hook_case(
        tmp_path, "update_lease",
    )

    assert entered.is_set()
    assert elapsed < 0.5
    assert finalized.is_set() is False
    assert json.dumps(result.artifact, sort_keys=True) == before


def test_hanging_finalize_hook_is_bounded_and_retains_checkpoint(tmp_path):
    result, elapsed, entered, finalized, before = _run_hanging_hook_case(
        tmp_path, "finalize",
    )

    assert entered.is_set()
    assert elapsed < 0.5
    assert json.dumps(result.artifact, sort_keys=True) == before
    assert result.artifact["sessions"][0]["state"] == "checkpoint"
