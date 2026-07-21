"""Authoritative orchestration for one specialist review run.

The controller deliberately composes the runtime components implemented by Tasks
1--12.  It owns ordering, controller-authority projections, degradation, and the
terminal artifact; it does not reproduce their policy decisions.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import Enum
import hashlib
import inspect
import json
import os
from pathlib import Path
import tempfile
from threading import Lock
import time
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from scripts.redact import mask_secrets

from .adjudication import (
    AdjudicatedReview,
    ReviewHandoffContext,
    adjudicate_candidates,
    apply_runtime_verdict_policy,
    build_review_handoff,
    build_review_notes,
)
from .assignments import (
    Assignment,
    AssignmentPlan,
    AssignmentPlanError,
    fallback_assignment_plan,
    repair_prompt,
    validate_assignment_plan,
)
from .budget import BudgetLedger, RunDeadline
from .coverage import (
    CoverageLedger,
    CoverageReconciliation,
    SessionOwnership,
    _assignment_ownership,
    derive_obligations,
    reconcile_wave,
)
from .evidence import EvidenceStore
from .events import EventJournal, RunEvent
from .negotiation import (
    NegotiationAction,
    NegotiationError,
    NegotiationState,
    SessionResources,
    fallback_next_action,
    validate_negotiation,
)
from .policy import ReviewPolicy, RuntimeConfig
from .scheduler import SessionScheduler, WaveResult, WaveSnapshot
from .types import (
    BudgetUsage,
    CandidateFinding,
    CoverageObligation,
    ObligationStatus,
    ReviewHandoff,
    ReviewNote,
    RunPhase,
)
from .web_evidence import SourceAccessRequest


_SCHEMA_VERSION = "1.0"
_PHASES = (
    "precheck", "planning", "initial", "followup", "finalization",
    "publish_ready", "complete",
)
_LEGAL_TRANSITIONS = {
    None: "precheck",
    "precheck": "planning",
    "planning": "initial",
    "initial": "followup",
    "followup": "finalization",
    "finalization": "publish_ready",
    "publish_ready": "complete",
}


class ControllerPhase(str, Enum):
    PRECHECK = "precheck"
    PLANNING = "planning"
    INITIAL = "initial"
    FOLLOWUP = "followup"
    FINALIZATION = "finalization"
    PUBLISH_READY = "publish_ready"
    COMPLETE = "complete"


@dataclass(frozen=True)
class ReviewInputs:
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    topology: Mapping[str, Any]
    classification: Mapping[str, Any]
    policy: ReviewPolicy
    config: RuntimeConfig
    changed_files: tuple[str, ...]
    artifact_path: Path | str = Path("specialist-review-artifact.json")
    allow_approve: bool = False
    publishing_mode: str = "review_comment"
    model_verdict: str = "approve"
    candidate_findings: tuple[CandidateFinding, ...] = ()
    source_access_requests: tuple[SourceAccessRequest, ...] = ()
    verification_requests: tuple[Mapping[str, Any], ...] = ()
    pr_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReviewResult:
    artifact: Mapping[str, Any]
    handoff: ReviewHandoff = field(default_factory=ReviewHandoff)
    notes: tuple[ReviewNote, ...] = ()
    verdict: str = "notice"
    verdict_source: str = "controller-degraded"
    events: tuple[RunEvent, ...] = ()
    artifact_path: Path | None = None
    artifact_write_error: str | None = None
    publishing_ready: bool = False


@dataclass
class _RunState:
    inputs: ReviewInputs
    journal: EventJournal
    deadline: RunDeadline
    evidence: EvidenceStore
    phase: str | None = None
    obligations: tuple[CoverageObligation, ...] = ()
    coverage: CoverageLedger | None = None
    plan: AssignmentPlan = field(default_factory=lambda: AssignmentPlan(()))
    plan_source: str = "deterministic_fallback"
    planner_repaired: bool = False
    assignments: dict[str, Assignment] = field(default_factory=dict)
    ownership: dict[str, SessionOwnership] = field(default_factory=dict)
    sessions: dict[str, object] = field(default_factory=dict)
    session_results: dict[str, object] = field(default_factory=dict)
    candidates: dict[str, CandidateFinding] = field(default_factory=dict)
    source_requests: list[SourceAccessRequest] = field(default_factory=list)
    unknowns: list[dict[str, object]] = field(default_factory=list)
    degradations: list[dict[str, str]] = field(default_factory=list)
    review: AdjudicatedReview = field(default_factory=AdjudicatedReview)
    handoff: ReviewHandoff = field(default_factory=ReviewHandoff)
    notes: tuple[ReviewNote, ...] = ()
    verdict: str = "notice"
    verdict_source: str = "controller-degraded"
    blocking_finding_ids: tuple[str, ...] = ()
    blocking_obligation_ids: tuple[str, ...] = ()
    publishing_ready: bool = False
    registry_lock: Lock = field(default_factory=Lock, repr=False)


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, (set, frozenset)):
        value = tuple(sorted(value, key=str))
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if is_dataclass(value):
        return {
            item.name: _json_value(getattr(value, item.name))
            for item in fields(value)
        }
    return str(value)


def _digest(value: object) -> str:
    encoded = json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_error(exc: BaseException) -> str:
    return mask_secrets(f"{type(exc).__name__}: {exc}")[:1000]


def _assignment_id(assignment: object) -> str:
    return str(getattr(assignment, "id", getattr(assignment, "assignment_id", ""))).strip()


def _budget_projection(value: BudgetUsage) -> dict[str, object]:
    return _json_value(value)  # type: ignore[return-value]


def _atomic_write_json(path: Path, artifact: Mapping[str, object]) -> None:
    """Write a private canonical JSON file without exposing a partial target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8") + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if hasattr(os, "O_DIRECTORY"):
            try:
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            except OSError:
                directory = None
            if directory is not None:
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


class ReviewController:
    """Own one review run from precheck through terminal projection."""

    def __init__(
        self,
        *,
        planner: object | None = None,
        planner_gateway: object | None = None,
        session_factory: Callable[..., object] | None = None,
        negotiator: object | None = None,
        critic: object | None = None,
        finalizer: object | None = None,
        evidence_store: EvidenceStore | None = None,
        event_sink: Callable[[RunEvent], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        artifact_writer: Callable[[Path, Mapping[str, object]], None] = _atomic_write_json,
        obligation_deriver: Callable[..., tuple[CoverageObligation, ...]] = derive_obligations,
        assignment_validator: Callable[..., AssignmentPlan] = validate_assignment_plan,
        scheduler_type: type[SessionScheduler] = SessionScheduler,
    ) -> None:
        if planner is not None and planner_gateway is not None:
            raise ValueError("provide planner or planner_gateway, not both")
        self.planner = planner if planner is not None else planner_gateway
        self.session_factory = session_factory
        self.negotiator = negotiator
        self.critic = critic
        self.finalizer = finalizer
        self._provided_evidence_store = evidence_store
        self.event_sink = event_sink
        self.clock = clock
        self.artifact_writer = artifact_writer
        self.obligation_deriver = obligation_deriver
        self.assignment_validator = assignment_validator
        self.scheduler_type = scheduler_type

    def _transition(self, state: _RunState, phase: str) -> None:
        expected = _LEGAL_TRANSITIONS.get(state.phase)
        if phase != expected:
            raise RuntimeError(f"illegal controller transition {state.phase!r} -> {phase!r}")
        previous = state.phase
        state.phase = phase
        state.journal.emit("phase_changed", {"phase": phase, "previous_phase": previous})

    def _degrade(self, state: _RunState, component: str, reason: str) -> None:
        item = {"component": component, "reason": mask_secrets(reason)[:1000]}
        state.degradations.append(item)
        state.journal.emit("degradation", item)

    def _model_request(
        self,
        state: _RunState,
        *,
        role: str,
        request_id: str,
        callback: Callable[[], object],
    ) -> object:
        state.journal.emit("model_request_started", {
            "request_id": request_id,
            "role": role,
            "remaining_deadline_sec": round(state.deadline.remaining(now=self.clock()), 6),
        })
        try:
            value = callback()
        except Exception as exc:
            state.journal.emit("model_request_failed", {
                "request_id": request_id, "role": role, "error": _bounded_error(exc),
            })
            raise
        state.journal.emit("model_request_completed", {
            "request_id": request_id, "role": role,
        })
        return value

    @staticmethod
    def _call_with_supported_args(component: object, method: str, *args: object) -> object:
        target = getattr(component, method, None)
        if target is None:
            target = component
        if not callable(target):
            raise TypeError(f"{method} component is not callable")
        signature = inspect.signature(target)
        if any(item.kind is inspect.Parameter.VAR_POSITIONAL for item in signature.parameters.values()):
            return target(*args)
        positional = tuple(
            item for item in signature.parameters.values()
            if item.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        )
        return target(*args[:len(positional)])

    def _plan(self, state: _RunState) -> AssignmentPlan:
        inputs = state.inputs
        if self.planner is None or self.clock() >= state.deadline.cutoff_for(RunPhase.PLANNING):
            self._degrade(state, "planner", "deterministic assignment fallback")
            return fallback_assignment_plan(state.obligations, inputs.topology, inputs.config)
        raw: object
        try:
            raw = self._model_request(
                state, role="planner", request_id="planner:1",
                callback=lambda: self._call_with_supported_args(
                    self.planner, "plan", state.obligations, inputs.topology, inputs.config,
                ),
            )
            if not isinstance(raw, Mapping):
                raise AssignmentPlanError("planner result must be an object")
            plan = self.assignment_validator(
                raw, state.obligations, inputs.topology, inputs.config,
            )
            state.plan_source = "model_validated"
            return plan
        except AssignmentPlanError as first_error:
            try:
                repair = repair_prompt(first_error.errors, raw if isinstance(raw, Mapping) else {})
                repaired = self._model_request(
                    state, role="planner", request_id="planner:repair:1",
                    callback=lambda: self._call_with_supported_args(
                        self.planner, "repair", state.obligations, inputs.topology,
                        inputs.config, repair,
                    ),
                )
                if not isinstance(repaired, Mapping):
                    raise AssignmentPlanError("planner repair must be an object")
                plan = self.assignment_validator(
                    repaired, state.obligations, inputs.topology, inputs.config,
                )
                state.plan_source = "model_repaired_validated"
                state.planner_repaired = True
                return plan
            except Exception as exc:
                self._degrade(state, "planner", _bounded_error(exc))
        except Exception as exc:
            self._degrade(state, "planner", _bounded_error(exc))
        state.journal.emit("recovery", {
            "component": "planner", "action": "deterministic_assignment_plan",
        })
        state.plan_source = "deterministic_fallback"
        return fallback_assignment_plan(state.obligations, inputs.topology, inputs.config)

    def _ownership(self, assignment: Assignment, session_id: str, state: _RunState) -> SessionOwnership:
        primary, secondary, independent = _assignment_ownership(
            assignment, {item.id: item for item in state.obligations},
        )
        return SessionOwnership(
            session_id=session_id,
            assignment_id=assignment.id,
            primary_obligation_ids=primary,
            secondary_obligation_ids=secondary,
            independent_obligation_ids=independent,
        )

    def _create_session(
        self,
        state: _RunState,
        assignment: Assignment,
        lease: object,
        snapshot: WaveSnapshot,
    ) -> object:
        with state.registry_lock:
            existing = state.sessions.get(assignment.id)
        if existing is not None:
            return existing
        if self.session_factory is None:
            raise RuntimeError("no specialist session factory configured")
        factory = self.session_factory
        signature = inspect.signature(factory)
        positional = tuple(
            item for item in signature.parameters.values()
            if item.kind in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        )
        args = (
            assignment, lease, snapshot, state.evidence, state.coverage,
            state.obligations,
        )
        session = factory(*args if any(
            item.kind is inspect.Parameter.VAR_POSITIONAL
            for item in signature.parameters.values()
        ) else args[:len(positional)])
        session_id = str(getattr(session, "session_id", assignment.id)).strip()
        if not session_id:
            raise ValueError("specialist session requires a stable session_id")
        with state.registry_lock:
            state.sessions[assignment.id] = session
            state.sessions[session_id] = session
            state.ownership[session_id] = self._ownership(assignment, session_id, state)
        return session

    def _run_wave(
        self,
        state: _RunState,
        assignments: Iterable[Assignment],
        phase: RunPhase,
    ) -> tuple[WaveResult, WaveSnapshot]:
        assert state.coverage is not None
        wave_snapshot = WaveSnapshot(state.evidence.snapshot(), state.coverage.snapshot())
        scheduler = self.scheduler_type(
            deadline=state.deadline,
            session_factory=lambda assignment, lease, snapshot: self._create_session(
                state, assignment, lease, snapshot,
            ),
            wave_snapshot=wave_snapshot,
            concurrency=state.inputs.config.concurrency,
            event_sink=lambda kind, payload: state.journal.emit(kind, payload),
            clock=self.clock,
        )
        result = scheduler.run_wave(tuple(assignments), phase)
        assignment_by_id = state.assignments
        for item in result.results:
            state.session_results[item.session_result.session_id] = item.session_result
            assignment = assignment_by_id[item.assignment_id]
            state.ownership[item.session_result.session_id] = self._ownership(
                assignment, item.session_result.session_id, state,
            )
            state.journal.emit("session_transition", {
                "assignment_id": item.assignment_id,
                "session_id": item.session_result.session_id,
                "state": "created",
            })
            state.journal.emit("session_transition", {
                "assignment_id": item.assignment_id,
                "session_id": item.session_result.session_id,
                "state": item.session_result.state.value,
            })
            usage = item.session_result.budget
            for turn in range(1, usage.model_turns + 1):
                state.journal.emit("model_request_observed", {
                    "request_id": f"session:{item.session_result.session_id}:{turn}",
                    "role": "specialist",
                })
            state.journal.emit("budget_changed", {
                "session_id": item.session_result.session_id,
                "usage": _budget_projection(usage),
            })
        for failure in result.failures:
            self._degrade(state, f"specialist:{failure.assignment_id}", failure.error)
            state.journal.emit("recovery", {
                "component": "specialist", "assignment_id": failure.assignment_id,
                "action": "bounded_followup_or_unknown",
            })
        before_ids = set(wave_snapshot.evidence.evidence_ids)
        for record in state.evidence.snapshot().records:
            if record.id not in before_ids:
                state.journal.emit("evidence_added", {
                    "evidence_id": record.id,
                    "category": record.category,
                    "collector_session_id": record.collector_session_id,
                    "content_hash": record.content_hash,
                    "status": record.status,
                })
        return result, wave_snapshot

    def _reconcile(
        self,
        state: _RunState,
        result: WaveResult,
        wave_snapshot: WaveSnapshot,
    ) -> CoverageReconciliation:
        assert state.coverage is not None
        checkpoints = tuple(item.session_result.checkpoint for item in result.results)
        try:
            reconciled = reconcile_wave(
                state.coverage,
                wave_start_coverage=wave_snapshot.coverage,
                checkpoints=checkpoints,
                evidence=state.evidence.snapshot(),
                assignments=tuple(state.assignments[key] for key in sorted(state.assignments)),
                session_ownership=tuple(
                    state.ownership[key] for key in sorted(state.ownership)
                    if state.ownership[key].assignment_id in state.assignments
                ),
            )
        except Exception as exc:
            self._degrade(state, "coverage_reconciliation", _bounded_error(exc))
            baseline_evidence = dict(wave_snapshot.coverage.evidence_by_obligation)
            unresolved = tuple(
                obligation_id for obligation_id, status in wave_snapshot.coverage.obligation_statuses
                if status is ObligationStatus.UNRESOLVED
            )
            state.coverage.replace_reconciled_state(baseline_evidence, unresolved)
            snapshot = state.coverage.snapshot()
            statuses = dict(snapshot.obligation_statuses)
            uncovered = tuple(sorted(
                item.id for item in state.obligations
                if item.mandatory and statuses.get(item.id) is not ObligationStatus.COVERED
            ))
            reconciled = CoverageReconciliation(snapshot, (), uncovered, (), uncovered)
        for obligation_id, status in reconciled.snapshot.obligation_statuses:
            state.journal.emit("coverage_decision", {
                "obligation_id": obligation_id,
                "status": status.value,
                "evidence_ids": dict(reconciled.snapshot.evidence_by_obligation).get(
                    obligation_id, (),
                ),
            })
        for recipe_id, status in reconciled.snapshot.recipe_statuses:
            state.journal.emit("recipe_status", {
                "recipe_id": recipe_id, "status": status,
            })
        return reconciled

    def _negotiation_state(
        self,
        state: _RunState,
        reconciliation: CoverageReconciliation,
        followup_started: int,
    ) -> NegotiationState:
        resources: list[SessionResources] = []
        for session_id, ownership in sorted(state.ownership.items()):
            session = state.sessions.get(session_id) or state.sessions.get(ownership.assignment_id)
            result = state.session_results.get(session_id)
            if session is None or result is None:
                continue
            ledger = getattr(session, "budget", None)
            if isinstance(ledger, BudgetLedger):
                remaining_turns = ledger.remaining_model_turns()
            else:
                remaining_turns = max(
                    0, state.inputs.config.session_limits.model_turns - result.budget.model_turns,
                )
            resources.append(SessionResources(
                session_id=session_id,
                remaining_model_turns=remaining_turns,
                lease_remaining_sec=state.deadline.remaining_for_exploration(now=self.clock()),
            ))
        checkpoints = tuple(
            state.session_results[key].checkpoint for key in sorted(state.session_results)
        )
        return NegotiationState(
            obligations=state.obligations,
            coverage=reconciliation.snapshot,
            assignments=tuple(state.assignments[key] for key in sorted(state.assignments)),
            checkpoints=checkpoints,
            session_ownership=tuple(state.ownership[key] for key in sorted(state.ownership)),
            session_resources=tuple(resources),
            remaining_deadline_sec=state.deadline.remaining_for_exploration(now=self.clock()),
            seconds_per_turn=float(state.inputs.config.model_request_timeout_sec),
            current_session_count=len(state.assignments),
            max_sessions=state.inputs.config.max_sessions,
            followup_sessions_started=followup_started,
            max_followup_sessions=state.inputs.config.max_followup_sessions,
            new_session_turns_remaining=max(
                0,
                (state.inputs.config.max_followup_sessions - followup_started)
                * state.inputs.config.session_limits.model_turns,
            ),
            new_session_turn_cap=state.inputs.config.session_limits.model_turns,
            new_session_lease_remaining_sec=state.deadline.remaining_for_exploration(
                now=self.clock(),
            ),
        )

    def _negotiate(
        self, state: _RunState, reconciliation: CoverageReconciliation,
    ) -> tuple[NegotiationAction, ...]:
        if not reconciliation.uncovered_obligation_ids:
            return ()
        negotiation_state = self._negotiation_state(state, reconciliation, 0)
        if not state.deadline.exploration_allowed(now=self.clock()):
            self._degrade(state, "deadline", "exploration cutoff reached before follow-up")
            try:
                return (fallback_next_action(negotiation_state),)
            except NegotiationError:
                return ()
        if self.negotiator is not None:
            try:
                raw = self._model_request(
                    state, role="negotiator", request_id="negotiator:1",
                    callback=lambda: self._call_with_supported_args(
                        self.negotiator, "propose", negotiation_state,
                    ),
                )
                if not isinstance(raw, Mapping):
                    raise NegotiationError("negotiator result must be an object")
                return validate_negotiation(raw, negotiation_state).actions
            except Exception as exc:
                self._degrade(state, "negotiator", _bounded_error(exc))
        try:
            action = fallback_next_action(negotiation_state)
            state.journal.emit("recovery", {
                "component": "negotiator", "action": action.kind,
                "obligation_ids": action.obligation_ids,
            })
            return (action,)
        except NegotiationError as exc:
            self._degrade(state, "negotiator", _bounded_error(exc))
            return ()

    def _followup_assignments(
        self, state: _RunState, actions: Iterable[NegotiationAction],
    ) -> tuple[Assignment, ...]:
        result: list[Assignment] = []
        obligation_by_id = {item.id: item for item in state.obligations}
        for index, action in enumerate(actions, start=1):
            state.journal.emit("negotiation_action", {
                "kind": action.kind,
                "session_id": action.session_id,
                "obligation_ids": action.obligation_ids,
                "estimated_turns": action.estimated_turns,
            })
            if action.kind == "record_unknown":
                for obligation_id in action.obligation_ids:
                    assert state.coverage is not None
                    state.coverage.mark_unresolved(obligation_id)
                    state.unknowns.append({
                        "obligation_id": obligation_id,
                        "reason": "bounded investigation recorded no further feasible evidence",
                        "resolution_policy": dict(action.resolution_policies).get(obligation_id),
                    })
                continue
            if action.kind in {"resume", "consult"} and action.session_id:
                ownership = state.ownership.get(action.session_id)
                if ownership is None:
                    continue
                assignment = state.assignments[ownership.assignment_id]
                session = state.sessions.get(action.session_id) or state.sessions.get(assignment.id)
                feedback = getattr(session, "apply_coverage_feedback", None)
                if callable(feedback):
                    feedback(action.obligation_ids)
                result.append(assignment)
                continue
            selected = tuple(
                obligation_by_id[item] for item in action.obligation_ids
                if item in obligation_by_id
            )
            plan = fallback_assignment_plan(selected, state.inputs.topology, state.inputs.config)
            if not plan.assignments:
                for obligation in selected:
                    state.unknowns.append({
                        "obligation_id": obligation.id,
                        "reason": "bounded follow-up assignment was infeasible",
                        "resolution_policy": obligation.unresolved_policy,
                    })
                    assert state.coverage is not None
                    state.coverage.mark_unresolved(obligation.id)
                continue
            assignment = replace(
                plan.assignments[0],
                id=f"{plan.assignments[0].id}-followup-{index}",
                title=f"{plan.assignments[0].title} follow-up {index}",
                estimated_turns=min(action.estimated_turns, plan.assignments[0].estimated_turns),
            )
            state.assignments[assignment.id] = assignment
            state.ownership[assignment.id] = self._ownership(assignment, assignment.id, state)
            result.append(assignment)
        return tuple(result)

    def _collect_candidates(self, state: _RunState) -> tuple[CandidateFinding, ...]:
        for candidate in state.inputs.candidate_findings:
            state.candidates.setdefault(candidate.candidate_id, candidate)
        seen: set[int] = set()
        for session in state.sessions.values():
            if id(session) in seen:
                continue
            seen.add(id(session))
            for candidate in getattr(session, "candidate_findings", ()):
                if isinstance(candidate, CandidateFinding):
                    state.candidates.setdefault(candidate.candidate_id, candidate)
        return tuple(state.candidates[key] for key in sorted(state.candidates))

    @staticmethod
    def _conservative_critic(candidates: Iterable[CandidateFinding]) -> dict[str, object]:
        decisions = []
        for item in sorted(candidates, key=lambda value: value.candidate_id):
            unambiguous = all((
                item.claim.strip(), item.affected_location.strip(), item.causal_chain.strip(),
                item.user_visible_consequence.strip(), item.manual_validation.strip(),
                item.supporting_evidence_ids, item.related_obligation_ids,
            ))
            decisions.append({
                "candidate_id": item.candidate_id,
                "action": "keep" if unambiguous else "reject",
            })
        return {"decisions": decisions}

    def _adjudicate(self, state: _RunState, candidates: tuple[CandidateFinding, ...]) -> None:
        obligation_map = {item.id: item for item in state.obligations}
        critic_result: object
        if self.critic is None:
            self._degrade(state, "critic", "deterministic conservative critic fallback")
            critic_result = self._conservative_critic(candidates)
        else:
            try:
                critic_result = self._model_request(
                    state, role="critic", request_id="critic:1",
                    callback=lambda: self._call_with_supported_args(
                        self.critic, "adjudicate", candidates,
                        MappingProxyType({
                            "obligations": obligation_map,
                            "changed_files": state.inputs.changed_files,
                        }),
                    ),
                )
            except Exception as exc:
                self._degrade(state, "critic", _bounded_error(exc))
                critic_result = self._conservative_critic(candidates)
        try:
            state.review = adjudicate_candidates(
                candidates, critic_result, state.evidence,
                obligations=obligation_map, changed_files=state.inputs.changed_files,
            )
        except Exception as exc:
            self._degrade(state, "adjudication", _bounded_error(exc))
            state.review = AdjudicatedReview()
        for disposition in state.review.dispositions:
            state.journal.emit("candidate_disposition", _json_value(disposition))

    def _handoff_context(self, state: _RunState, status: str) -> ReviewHandoffContext:
        recipe_ids = tuple(sorted(
            item.id for item in state.inputs.policy.recipes
            if state.coverage is not None
            and state.coverage.recipe_statuses().get(item.id) != "not_applicable"
        ))
        return ReviewHandoffContext(
            recommendation=state.verdict,
            status=status,
            component_ids=tuple(sorted(
                str(item.get("id", "")) for item in state.inputs.topology.get("components", ())
                if isinstance(item, Mapping) and str(item.get("id", "")).strip()
            )),
            recipe_ids=recipe_ids,
            unresolved_thread_count=len(state.review.accepted),
            highest_thread_severity=max(
                (item.severity for item in state.review.accepted), default=None,
                key=lambda value: {"info": 0, "minor": 1, "major": 2, "blocker": 3}.get(value, 0),
            ),
            material_coverage_limited=bool(state.degradations),
            source_access_requests=tuple(state.source_requests),
        )

    @staticmethod
    def _minimal_handoff(verdict: str, degraded: bool) -> ReviewHandoff:
        recommendation = {
            "request_changes": "Request changes",
            "approve": "Approve",
        }.get(verdict, "Human review required")
        status = (
            "AI review completed with material coverage limits"
            if degraded else "AI review complete"
        )
        markdown = (
            "## AI Review Handoff\n\n"
            f"**Recommendation:** {recommendation}\n\n"
            f"**Status:** {status}\n\n"
            "These focus suggestions do not reduce responsibility to review the complete change.\n"
        )
        return ReviewHandoff(markdown=markdown, recommendation=recommendation)

    def _finalize_products(self, state: _RunState) -> None:
        assert state.coverage is not None
        obligation_map = {item.id: item for item in state.obligations}
        statuses = state.coverage.obligation_statuses()
        unresolved = tuple(
            item for item in state.obligations
            if item.mandatory and statuses.get(item.id) is not ObligationStatus.COVERED
        )
        policy_result = apply_runtime_verdict_policy(
            model_verdict=state.inputs.model_verdict,
            unresolved=unresolved,
            allow_approve=state.inputs.allow_approve,
            evidence=state.evidence,
            obligations=obligation_map,
            changed_files=state.inputs.changed_files,
            review=state.review,
            policy=state.inputs.policy.verdict_policy,
        )
        state.verdict = policy_result.verdict
        state.verdict_source = policy_result.source
        state.blocking_finding_ids = policy_result.blocking_finding_ids
        state.blocking_obligation_ids = policy_result.blocking_obligation_ids
        state.journal.emit("verdict_selected", {
            "verdict": state.verdict,
            "source": state.verdict_source,
            "blocking_finding_ids": state.blocking_finding_ids,
            "blocking_obligation_ids": state.blocking_obligation_ids,
        })
        status = "degraded" if state.degradations else "complete"
        context = self._handoff_context(state, status)
        if self.finalizer is not None and state.deadline.remaining(now=self.clock()) > 0:
            try:
                proposed = self._model_request(
                    state, role="finalizer", request_id="finalizer:1",
                    callback=lambda: self._call_with_supported_args(
                        self.finalizer, "finalize", MappingProxyType({
                            "review": state.review,
                            "coverage": state.coverage.snapshot(),
                            "verdict": state.verdict,
                            "verdict_source": state.verdict_source,
                        }),
                    ),
                )
                if not isinstance(proposed, ReviewHandoffContext):
                    raise TypeError("finalizer must return ReviewHandoffContext")
                context = replace(
                    proposed,
                    recommendation=state.verdict,
                    status=status,
                    source_access_requests=tuple(state.source_requests),
                )
            except Exception as exc:
                self._degrade(state, "finalizer", _bounded_error(exc))
                context = self._handoff_context(state, "degraded")
        elif self.finalizer is None:
            self._degrade(state, "finalizer", "deterministic sparse handoff fallback")
            context = self._handoff_context(state, "degraded")
        else:
            self._degrade(state, "deadline", "finalizer model skipped after absolute deadline")
            context = self._handoff_context(state, "degraded")
        try:
            state.handoff = build_review_handoff(
                context, review=state.review, evidence=state.evidence,
                obligations=obligation_map, changed_files=state.inputs.changed_files,
            )
        except Exception as exc:
            self._degrade(state, "finalizer", _bounded_error(exc))
            state.handoff = self._minimal_handoff(state.verdict, True)
        try:
            state.notes = build_review_notes(
                state.review, state.evidence, state.inputs.publishing_mode,
                obligations=obligation_map, changed_files=state.inputs.changed_files,
                verification_requests=state.inputs.verification_requests,
                source_access_requests=state.source_requests,
            )
        except Exception as exc:
            self._degrade(state, "review_notes", _bounded_error(exc))
            state.notes = ()

    def _phase_allocations(self, state: _RunState) -> list[dict[str, object]]:
        shares = state.inputs.config.phase_shares
        percentages = {
            "precheck": 0, "planning": shares.planning, "initial": shares.initial,
            "followup": shares.followup, "finalization": shares.finalization,
            "publish_ready": 0, "complete": 0,
        }
        return [
            {
                "phase": phase,
                "status": "complete" if _PHASES.index(phase) <= _PHASES.index(state.phase or "precheck") else "not_started",
                "allocated_percent": percentages[phase],
                "allocated_seconds": round(
                    state.inputs.config.review_deadline_sec * percentages[phase] / 100, 6,
                ),
            }
            for phase in _PHASES
        ]

    def _artifact(self, state: _RunState, path: Path) -> dict[str, object]:
        assert state.coverage is not None
        coverage = state.coverage.snapshot()
        statuses = dict(coverage.obligation_statuses)
        evidence_by_obligation = dict(coverage.evidence_by_obligation)
        recipe_states = state.coverage.recipe_statuses()
        for recipe in state.inputs.policy.recipes:
            recipe_states.setdefault(recipe.id, "not_applicable")
        unique_sessions: dict[str, object] = {}
        for result in state.session_results.values():
            unique_sessions[result.session_id] = result
        sessions = []
        for session_id in sorted(set(state.ownership).union(unique_sessions)):
            ownership = state.ownership.get(session_id)
            result = unique_sessions.get(session_id)
            sessions.append({
                "session_id": session_id,
                "assignment_id": ownership.assignment_id if ownership else None,
                "ownership": _json_value(ownership) if ownership else None,
                "state": result.state.value if result else "failed_or_not_started",
                "checkpoint": _json_value(result.checkpoint) if result else None,
                "budget": _budget_projection(result.budget) if result else _budget_projection(BudgetUsage()),
                "degraded": bool(result.degraded) if result else True,
            })
        evidence = []
        for record in state.evidence.snapshot().records:
            evidence.append({
                "evidence_id": record.id,
                "category": record.category,
                "collector_session_id": record.collector_session_id,
                "model_identity": record.model_identity,
                "tool": record.tool,
                "source_identity": record.source_identity,
                "source_path": record.source_path,
                "provenance": {
                    "head_sha": record.provenance.head_sha,
                    "policy_hash": record.provenance.policy_hash,
                    "policy_rule_id": record.provenance.policy_rule_id,
                    "source_classification": record.provenance.source_classification,
                    "original_url": record.provenance.original_url,
                    "final_url": record.provenance.final_url,
                    "max_age_hours": record.provenance.max_age_hours,
                },
                "status": record.status,
                "content_hash": record.content_hash,
                "mime_type": record.mime_type,
                "truncated": record.truncated,
                "redacted": record.redacted,
                "imported_by": list(record.imported_by),
                "supersedes": list(record.supersedes),
                "contradicts": list(record.contradicts),
            })
        artifacts_events = [
            {"sequence": event.sequence, "kind": event.kind}
            for event in state.journal.snapshot()
        ]
        policy_digest = _digest(state.inputs.policy)
        config_digest = _digest(state.inputs.config)
        run_id = _digest({
            "repository": state.inputs.repository,
            "pr_number": state.inputs.pr_number,
            "base_sha": state.inputs.base_sha,
            "head_sha": state.inputs.head_sha,
            "policy_digest": policy_digest,
            "config_digest": config_digest,
        })[:32]
        artifact: dict[str, object] = {
            "accepted_candidates": [_json_value(item) for item in state.review.accepted],
            "candidate_dispositions": [
                _json_value(item) for item in state.review.dispositions
            ],
            "artifact_id": run_id,
            "artifact_write": {"status": "ready", "path": path.name},
            "assignment_plan": {
                "source": state.plan_source,
                "planner_repaired": state.planner_repaired,
                "unassigned_obligation_ids": list(state.plan.unassigned_obligation_ids),
            },
            "assignments": [_json_value(state.assignments[key]) for key in sorted(state.assignments)],
            "base_sha": state.inputs.base_sha,
            "budgets": {
                "sessions": {
                    item["session_id"]: item["budget"] for item in sessions
                },
                "totals": {
                    "model_turns": sum(item["budget"]["model_turns"] for item in sessions),
                    "tool_calls": sum(item["budget"]["tool_calls"] for item in sessions),
                    "recoveries": sum(item["budget"]["recoveries"] for item in sessions),
                    "controller_model_requests": sum(
                        1 for event in state.journal.snapshot()
                        if event.kind == "model_request_completed"
                    ),
                },
            },
            "coverage": {
                item.id: {
                    "status": statuses[item.id].value,
                    "mandatory": item.mandatory,
                    "risk_tier": item.risk_tier,
                    "unresolved_policy": item.unresolved_policy,
                    "recipe_id": item.recipe_id,
                    "origin": item.origin,
                    "subject": item.subject,
                    "explanation": item.explanation,
                    "scope": list(item.scope),
                    "satisfaction_predicates": list(item.satisfaction_predicates),
                    "requires_independent_verification": item.requires_independent_verification,
                    "required_evidence_categories": list(item.required_evidence_categories),
                    "evidence_ids": list(evidence_by_obligation.get(item.id, ())),
                }
                for item in state.obligations
            },
            "degradation": list(state.degradations),
            "evaluation_status": "degraded" if state.degradations else "complete",
            "event_references": artifacts_events,
            "evidence": evidence,
            "handoff": _json_value(state.handoff),
            "head_sha": state.inputs.head_sha,
            "notes": [
                {
                    "kind": item.kind.value,
                    "fingerprint": item.fingerprint,
                    "related_obligation_ids": list(item.related_obligation_ids),
                    "evidence_ids": list(item.evidence_ids),
                }
                for item in state.notes
            ],
            "phases": self._phase_allocations(state),
            "policy": {
                "version": state.inputs.policy.version,
                "digest": policy_digest,
                "config_digest": config_digest,
            },
            "pr_number": state.inputs.pr_number,
            "publishing": {"ready": state.publishing_ready, "status": "not_published"},
            "recipes": {
                key: {"status": value} for key, value in sorted(recipe_states.items())
            },
            "rejected_candidates": [_json_value(item) for item in state.review.rejected],
            "candidate_unknowns": [
                {
                    "candidate_id": item.candidate_id,
                    "related_obligation_ids": list(item.related_obligation_ids),
                    "supporting_evidence_ids": list(item.supporting_evidence_ids),
                    "contradicting_evidence_ids": list(item.contradicting_evidence_ids),
                }
                for item in state.review.unknowns
            ],
            "repository": state.inputs.repository,
            "schema_version": _SCHEMA_VERSION,
            "sessions": sessions,
            "source_access_requests": [_json_value(item) for item in state.source_requests],
            "timing": {
                "deadline_seconds": state.inputs.config.review_deadline_sec,
                "phase_shares": _json_value(state.inputs.config.phase_shares),
                "finalization_reserve_seconds": round(
                    state.inputs.config.review_deadline_sec
                    * state.inputs.config.phase_shares.finalization / 100,
                    6,
                ),
            },
            "unknowns": sorted(
                state.unknowns,
                key=lambda item: (str(item.get("obligation_id", "")), str(item.get("reason", ""))),
            ),
            "verdict": {
                "value": state.verdict,
                "source": state.verdict_source,
                "blocking_finding_ids": list(state.blocking_finding_ids),
                "blocking_obligation_ids": list(state.blocking_obligation_ids),
            },
        }
        return artifact

    @staticmethod
    def _validate_artifact(artifact: Mapping[str, object]) -> None:
        required = {
            "schema_version", "repository", "pr_number", "base_sha", "head_sha",
            "policy", "phases", "assignments", "sessions", "budgets", "evidence",
            "assignment_plan",
            "coverage", "recipes", "unknowns", "source_access_requests",
            "accepted_candidates", "rejected_candidates", "handoff", "notes",
            "candidate_dispositions", "candidate_unknowns",
            "verdict", "degradation", "publishing", "event_references",
            "evaluation_status", "artifact_write", "timing",
        }
        missing = sorted(required - set(artifact))
        if missing:
            raise ValueError("terminal artifact missing fields: " + ", ".join(missing))
        if artifact.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("terminal artifact schema version is invalid")
        coverage = artifact.get("coverage")
        recipes = artifact.get("recipes")
        if not isinstance(coverage, Mapping) or any(
            not isinstance(value, Mapping) or not value.get("status")
            for value in coverage.values()
        ):
            raise ValueError("every obligation requires a terminal status")
        if not isinstance(recipes, Mapping) or any(
            not isinstance(value, Mapping) or not value.get("status")
            for value in recipes.values()
        ):
            raise ValueError("every recipe requires a terminal status")
        json.dumps(artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _finish_after_unexpected(self, state: _RunState, exc: BaseException) -> None:
        self._degrade(state, "controller", _bounded_error(exc))
        while state.phase != "complete":
            next_phase = _LEGAL_TRANSITIONS.get(state.phase)
            if next_phase is None:
                break
            self._transition(state, next_phase)

    def run(self, inputs: ReviewInputs) -> ReviewResult:
        """Run all legal phases and always return a terminal in-memory result."""
        journal = EventJournal(self.event_sink)
        started_at = self.clock()
        deadline = RunDeadline(started_at, inputs.config.review_deadline_sec, inputs.config.phase_shares)
        evidence = self._provided_evidence_store or EvidenceStore()
        state = _RunState(inputs=inputs, journal=journal, deadline=deadline, evidence=evidence)
        path = Path(inputs.artifact_path)
        artifact: dict[str, object] = {}
        write_error: str | None = None
        try:
            journal.emit("run_started", {
                "repository": inputs.repository,
                "pr_number": inputs.pr_number,
                "base_sha": inputs.base_sha,
                "head_sha": inputs.head_sha,
            })
            self._transition(state, "precheck")
            if not inputs.repository.strip() or inputs.pr_number <= 0:
                raise ValueError("repository and positive PR number are required")
            if inputs.publishing_mode not in {"comment", "review_comment", "review_verdict"}:
                raise ValueError("invalid publishing mode")
            self._transition(state, "planning")
            state.obligations = self.obligation_deriver(
                inputs.topology, inputs.classification, inputs.policy,
            )
            state.coverage = CoverageLedger(state.obligations)
            state.plan = self._plan(state)
            state.assignments = {
                item.id: item for item in state.plan.assignments
            }
            for item in state.plan.assignments:
                state.ownership[item.id] = self._ownership(item, item.id, state)
                journal.emit("assignment_decision", {
                    "assignment_id": item.id,
                    "obligation_ids": item.obligation_ids,
                    "source": state.plan_source,
                })
            for obligation_id in state.plan.unassigned_obligation_ids:
                state.coverage.mark_unresolved(obligation_id)
                obligation = state.coverage.obligation(obligation_id)
                state.unknowns.append({
                    "obligation_id": obligation_id,
                    "reason": "deterministic assignment capacity exhausted",
                    "resolution_policy": obligation.unresolved_policy,
                })

            self._transition(state, "initial")
            initial, initial_snapshot = self._run_wave(
                state, state.plan.assignments, RunPhase.INITIAL,
            )
            reconciliation = self._reconcile(state, initial, initial_snapshot)

            self._transition(state, "followup")
            actions = self._negotiate(state, reconciliation)
            followups = self._followup_assignments(state, actions)
            followup, followup_snapshot = self._run_wave(
                state, followups, RunPhase.FOLLOWUP,
            )
            reconciliation = self._reconcile(state, followup, followup_snapshot)
            for obligation_id in reconciliation.uncovered_obligation_ids:
                if not any(item.get("obligation_id") == obligation_id for item in state.unknowns):
                    obligation = state.coverage.obligation(obligation_id)
                    state.coverage.mark_unresolved(obligation_id)
                    state.unknowns.append({
                        "obligation_id": obligation_id,
                        "reason": "mandatory coverage remained unresolved after bounded follow-up",
                        "resolution_policy": obligation.unresolved_policy,
                    })

            self._transition(state, "finalization")
            seen: set[int] = set()
            for key in sorted(state.sessions):
                session = state.sessions[key]
                if id(session) in seen:
                    continue
                seen.add(id(session))
                if state.deadline.remaining(now=self.clock()) <= 0:
                    self._degrade(
                        state, "deadline",
                        "absolute deadline reached; specialist finalization used retained checkpoint",
                    )
                    break
                if hasattr(session, "lease"):
                    try:
                        session.lease = state.deadline.lease_for(RunPhase.FINALIZATION)
                    except Exception:
                        pass
                finalizer = getattr(session, "finalize", None)
                if not callable(finalizer):
                    continue
                try:
                    result = finalizer()
                    state.session_results[result.session_id] = result
                    journal.emit("session_transition", {
                        "session_id": result.session_id,
                        "state": result.state.value,
                    })
                    journal.emit("budget_changed", {
                        "session_id": result.session_id,
                        "usage": _budget_projection(result.budget),
                    })
                    if state.deadline.remaining(now=self.clock()) <= 0:
                        self._degrade(
                            state, "deadline",
                            "specialist finalization completed at the absolute deadline",
                        )
                        break
                except Exception as exc:
                    self._degrade(state, f"specialist_finalization:{key}", _bounded_error(exc))
            candidates = self._collect_candidates(state)
            self._adjudicate(state, candidates)
            state.source_requests.extend(sorted(
                inputs.source_access_requests,
                key=lambda item: (
                    item.obligation_id, item.host, item.candidate_url, item.purpose,
                ),
            ))
            for request in state.source_requests:
                journal.emit("source_access_request", {
                    "fingerprint": _digest(request.as_dict())[:32],
                    "host": request.host,
                    "obligation_id": request.obligation_id,
                })
            self._finalize_products(state)

            self._transition(state, "publish_ready")
            state.publishing_ready = True
            journal.emit("publishing_ready", {
                "handoff": bool(state.handoff.markdown),
                "note_ids": tuple(item.fingerprint for item in state.notes),
                "verdict": state.verdict,
                "verdict_source": state.verdict_source,
            })
            self._transition(state, "complete")
        except BaseException as exc:  # terminal artifact survives every controlled failure
            self._finish_after_unexpected(state, exc)
            if state.coverage is None:
                state.coverage = CoverageLedger(())
            if not state.handoff.markdown:
                state.handoff = self._minimal_handoff(state.verdict, True)
            state.publishing_ready = (
                bool(state.handoff.markdown)
                and state.verdict in {"approve", "request_changes"}
            )

        journal.emit("artifact_reference", {
            "artifact_id": _digest({
                "repository": inputs.repository,
                "pr_number": inputs.pr_number,
                "head_sha": inputs.head_sha,
            })[:32],
            "filename": path.name,
        })
        try:
            artifact = self._artifact(state, path)
            self._validate_artifact(artifact)
        except BaseException as exc:
            self._degrade(state, "artifact_projection", _bounded_error(exc))
            # This projection is intentionally small but remains schema-valid and
            # does not invent findings, evidence, coverage, or a blocking verdict.
            artifact = {
                "schema_version": _SCHEMA_VERSION,
                "repository": inputs.repository,
                "pr_number": inputs.pr_number,
                "base_sha": inputs.base_sha,
                "head_sha": inputs.head_sha,
                "policy": {"version": inputs.policy.version, "digest": _digest(inputs.policy), "config_digest": _digest(inputs.config)},
                "phases": self._phase_allocations(state),
                "assignment_plan": {"source": "deterministic_fallback", "planner_repaired": False, "unassigned_obligation_ids": []},
                "assignments": [], "sessions": [], "budgets": {"sessions": {}, "totals": {"model_turns": 0, "tool_calls": 0, "recoveries": 0, "controller_model_requests": 0}},
                "evidence": [], "coverage": {},
                "recipes": {item.id: {"status": "unresolved"} for item in inputs.policy.recipes},
                "unknowns": list(state.unknowns), "source_access_requests": [],
                "accepted_candidates": [], "rejected_candidates": [],
                "candidate_dispositions": [], "candidate_unknowns": [],
                "handoff": _json_value(state.handoff), "notes": [],
                "verdict": {"value": state.verdict, "source": state.verdict_source, "blocking_finding_ids": [], "blocking_obligation_ids": []},
                "degradation": list(state.degradations),
                "publishing": {"ready": state.publishing_ready, "status": "not_published"},
                "event_references": [{"sequence": item.sequence, "kind": item.kind} for item in journal.snapshot()],
                "evaluation_status": "degraded",
                "artifact_write": {"status": "ready", "path": path.name},
                "timing": {"deadline_seconds": inputs.config.review_deadline_sec, "phase_shares": _json_value(inputs.config.phase_shares), "finalization_reserve_seconds": inputs.config.review_deadline_sec * inputs.config.phase_shares.finalization / 100},
            }
            self._validate_artifact(artifact)
        try:
            self.artifact_writer(path, artifact)
        except BaseException as exc:
            write_error = _bounded_error(exc)
            journal.emit("artifact_write_failed", {
                "filename": path.name, "error": write_error,
            })
            artifact = dict(artifact)
            artifact["artifact_write"] = {"status": "failed", "error": write_error}
            artifact["evaluation_status"] = "degraded"
            artifact["event_references"] = [
                {"sequence": item.sequence, "kind": item.kind}
                for item in journal.snapshot()
            ]
            self._validate_artifact(artifact)
        return ReviewResult(
            artifact=artifact,
            handoff=state.handoff,
            notes=state.notes,
            verdict=state.verdict,
            verdict_source=state.verdict_source,
            events=journal.snapshot(),
            artifact_path=path,
            artifact_write_error=write_error,
            publishing_ready=state.publishing_ready,
        )
