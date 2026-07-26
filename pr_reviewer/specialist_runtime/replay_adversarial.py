"""Deterministic adversarial probes used by offline specialist replay."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from pr_reviewer.conversation import Conversation, TOOL_SCHEMAS

from .adjudication import adjudicate_candidates, build_review_notes
from .budget import BudgetLedger, SessionLease
from .controller import GatewayRoleAdapter, ReviewController, ReviewInputs
from .coverage import CoverageLedger
from .evidence import EvidenceStore
from .model_gateway import ModelTurnRequest, ModelTurnResult, OpenAIModelGateway
from .policy import ReviewPolicy, RuntimeConfig
from .session import SpecialistSession
from .types import (
    BudgetLimits,
    CandidateFinding,
    CoverageObligation,
    PhaseShares,
    ReviewNote,
    RunPhase,
    SpecialistAssignment,
)


_READ_FILE_SCHEMA = next(
    item for item in TOOL_SCHEMAS if item.get("name") == "read_file"
)


class _ReplayClock:
    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self._now = self.started_at
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self._now

    def advance_to(self, value: float) -> None:
        with self._lock:
            self._now = max(self._now, float(value))

    @property
    def elapsed(self) -> float:
        return self() - self.started_at


class _RecordedGateway:
    """Parse explicit OpenAI bodies while validating the public request contract."""

    def __init__(
        self,
        turns: Sequence[Mapping[str, Any]],
        *,
        before: Callable[[int, ModelTurnRequest], None] | None = None,
        after: Callable[[int, ModelTurnResult], None] | None = None,
    ) -> None:
        self.turns = tuple(turns)
        self.before = before
        self.after = after
        self.index = 0
        self._active: Mapping[str, Any] | None = None
        self.gateway = OpenAIModelGateway(
            base_url="https://recorded.invalid/v1",
            api_key="recorded-offline",
            default_model="recorded-adversarial",
            api_format="openai",
            response_format="json_schema",
            stream_watchdog=False,
            transport=self._transport,
        )

    def complete(self, request: ModelTurnRequest) -> ModelTurnResult:
        if self.index >= len(self.turns):
            raise AssertionError("adversarial recorded responses were exhausted")
        turn_index = self.index
        turn = self.turns[turn_index]
        expected = turn.get("expect")
        if not isinstance(expected, Mapping) or set(expected) != {
            "role", "tools_enabled", "response_schema_name",
        }:
            raise AssertionError("adversarial request expectation has wrong schema")
        actual = {
            "role": request.role,
            "tools_enabled": request.tools_enabled,
            "response_schema_name": request.response_schema_name,
        }
        if dict(expected) != actual:
            raise AssertionError(
                f"adversarial recorded request mismatch: "
                f"expected {dict(expected)!r}, got {actual!r}"
            )
        if self.before is not None:
            self.before(turn_index, request)
        self.index += 1
        self._active = turn
        try:
            result = self.gateway.complete(request)
        finally:
            self._active = None
        if self.after is not None:
            self.after(turn_index, result)
        return result

    def _transport(
        self,
        base_url: str,
        api_format: str,
        payload: dict[str, Any],
        api_key: str,
        timeout: float,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del api_key, timeout, kwargs
        if (
            self._active is None
            or base_url != "https://recorded.invalid/v1"
            or api_format != "openai"
        ):
            raise AssertionError("adversarial transport boundary changed")
        expected = self._active["expect"]
        if not isinstance(payload.get("messages"), list) or not payload["messages"]:
            raise AssertionError("adversarial OpenAI request lacks messages")
        if bool(payload.get("tools")) != bool(expected["tools_enabled"]):
            raise AssertionError("adversarial OpenAI tools shape changed")
        if ("response_format" in payload) == bool(expected["tools_enabled"]):
            raise AssertionError("adversarial structured-output shape changed")
        response = self._active.get("response")
        if not isinstance(response, Mapping):
            raise AssertionError("adversarial response is not an OpenAI object")
        return deepcopy(dict(response))

    def assert_complete(self) -> None:
        if self.index != len(self.turns):
            raise AssertionError(
                f"adversarial responses not consumed: "
                f"{len(self.turns) - self.index}"
            )


def _session(
    turns: Sequence[Mapping[str, Any]],
    obligations: Sequence[CoverageObligation],
    assignment: SpecialistAssignment,
) -> SpecialistSession:
    evidence = EvidenceStore()
    gateway = _RecordedGateway(turns)

    def execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "tool": name,
            "status": "ok",
            "result": {"content": f"contents:{arguments.get('path', '')}"},
        }

    session = SpecialistSession(
        session_id="failure-session",
        assignment=assignment,
        conversation=Conversation(
            system="Recorded failure replay.",
            tool_schemas=[_READ_FILE_SCHEMA],
        ),
        gateway=gateway,
        execute_tool=execute,
        evidence_store=evidence,
        coverage=CoverageLedger(obligations),
        budget=BudgetLedger(BudgetLimits(12, 8, 1)),
        lease=SessionLease(RunPhase.FOLLOWUP, time.monotonic() + 60),
        request_timeout_sec=5,
        max_tokens=512,
        max_no_progress_streak=2,
    )
    session._recorded_gateway = gateway  # type: ignore[attr-defined]
    return session


class _CompletionOrder:
    """Release the second assignment only after the first checkpoint completes."""

    def __init__(self, order: Sequence[str]) -> None:
        if len(order) != 2 or len(set(order)) != 2:
            raise ValueError("completion order must contain two unique assignments")
        self.order = tuple(order)
        self.first_checkpoint = threading.Event()
        self.actual: list[str] = []
        self._lock = threading.Lock()

    def before(
        self, assignment_id: str, index: int, request: ModelTurnRequest,
    ) -> None:
        del request
        if index == 0 and assignment_id != self.order[0]:
            if not self.first_checkpoint.wait(timeout=5):
                raise AssertionError("first recorded completion never checkpointed")

    def after(
        self, assignment_id: str, index: int, result: ModelTurnResult,
    ) -> None:
        del result
        if index != 1:
            return
        with self._lock:
            self.actual.append(assignment_id)
        if assignment_id == self.order[0]:
            self.first_checkpoint.set()


def _controller_topology() -> dict[str, Any]:
    return {
        "changed_files": ["src/a.py", "src/b.py"],
        "file_roles": ["implementation"],
        "components": [
            {
                "id": "a",
                "changed_files": ["src/a.py"],
                "file_roles": ["implementation"],
            },
            {
                "id": "b",
                "changed_files": ["src/b.py"],
                "file_roles": ["implementation"],
            },
        ],
        "relationships": [],
        "available_role_paths": {},
    }


def _completion_controller_run(
    raw: Mapping[str, Any],
    order: Sequence[str],
) -> dict[str, Any]:
    coordinator = _CompletionOrder(order)
    planner_gateway = _RecordedGateway((raw["planner_response"],))
    session_gateways: dict[str, _RecordedGateway] = {}

    def session_factory(
        assignment: object,
        lease: SessionLease,
        snapshot: object,
        evidence: EvidenceStore,
        coverage: CoverageLedger,
        obligations: Sequence[CoverageObligation],
        session_id: str,
    ) -> SpecialistSession:
        del snapshot, obligations
        assignment_id = str(getattr(assignment, "id"))
        gateway = _RecordedGateway(
            raw["session_responses"][assignment_id],
            before=lambda index, request: coordinator.before(
                assignment_id, index, request,
            ),
            after=lambda index, result: coordinator.after(
                assignment_id, index, result,
            ),
        )
        session_gateways[assignment_id] = gateway

        def execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result = {
                "tool": name,
                "status": "ok",
                "result": {
                    "content": f"{assignment_id}:{arguments.get('path', '')}",
                },
            }
            evidence.add_tool_result(
                session_id=session_id,
                tool=name,
                arguments=arguments,
                result=result,
                category=str(
                    arguments.get("evidence_category") or "tool-result"
                ),
                model_identity="recorded-adversarial",
            )
            return result

        return SpecialistSession(
            session_id=session_id,
            assignment=assignment,
            conversation=Conversation(
                system="Recorded completion-order replay.",
                tool_schemas=[_READ_FILE_SCHEMA],
            ),
            gateway=gateway,
            execute_tool=execute,
            evidence_store=evidence,
            coverage=coverage,
            budget=BudgetLedger(BudgetLimits(8, 8, 1)),
            lease=lease,
            request_timeout_sec=5,
            max_tokens=512,
        )

    config = RuntimeConfig(
        review_deadline_sec=60,
        model_request_timeout_sec=5,
        phase_shares=PhaseShares(),
        concurrency=2,
        max_sessions=2,
        max_followup_sessions=1,
        session_limits=BudgetLimits(8, 8, 1),
    )
    topology = _controller_topology()
    with tempfile.TemporaryDirectory(prefix="completion-inversion-") as temp_dir:
        controller = ReviewController(
            planner=GatewayRoleAdapter(planner_gateway),
            session_factory=session_factory,
            artifact_output_root=Path(temp_dir),
        )
        result = controller.run(ReviewInputs(
            repository="example/completion-inversion",
            pr_number=1,
            base_sha="1" * 40,
            head_sha="2" * 40,
            topology=topology,
            classification={},
            policy=ReviewPolicy.minimal(),
            config=config,
            changed_files=tuple(topology["changed_files"]),
            artifact_path="artifact.json",
            publishing_mode="comment",
            pr_metadata={"title": "Completion inversion"},
        ))
    planner_gateway.assert_complete()
    gateway_consumption = {
        assignment_id: (gateway.index, len(gateway.turns))
        for assignment_id, gateway in session_gateways.items()
    }
    for gateway in session_gateways.values():
        gateway.assert_complete()
    return {
        "target_order": list(order),
        "actual_order": list(coordinator.actual),
        "coverage": {
            key: {
                "status": value["status"],
                "evidence_ids": list(value["evidence_ids"]),
            }
            for key, value in sorted(result.artifact["coverage"].items())
        },
        "evidence": [
            {
                key: item.get(key)
                for key in (
                    "evidence_id", "content_hash", "source_path",
                    "category", "status",
                )
            }
            for item in result.artifact["evidence"]
        ],
        "terminal": result.artifact["evaluation_status"] in {
            "complete", "degraded",
        },
        "gateway_consumption": gateway_consumption,
    }


def _deadline_controller_run(raw: Mapping[str, Any]) -> dict[str, Any]:
    clock = _ReplayClock()
    shares = PhaseShares(**raw["phase_shares"])
    deadline_sec = int(raw["deadline_sec"])
    initial_cutoff = (
        clock.started_at
        + deadline_sec * (shares.planning + shares.initial) / 100
    )
    gateway = _RecordedGateway(
        raw["responses"],
        before=lambda index, request: (
            clock.advance_to(initial_cutoff) if index == 0 else None
        ),
    )
    topology = {
        "changed_files": ["src/a.py"],
        "file_roles": ["implementation"],
        "components": [{
            "id": "a",
            "changed_files": ["src/a.py"],
            "file_roles": ["implementation"],
        }],
        "relationships": [],
        "available_role_paths": {},
    }

    def session_factory(
        assignment: object,
        lease: SessionLease,
        snapshot: object,
        evidence: EvidenceStore,
        coverage: CoverageLedger,
        obligations: Sequence[CoverageObligation],
        session_id: str,
    ) -> SpecialistSession:
        del snapshot, obligations
        return SpecialistSession(
            session_id=session_id,
            assignment=assignment,
            conversation=Conversation(
                system="Recorded deadline replay.",
                tool_schemas=[_READ_FILE_SCHEMA],
            ),
            gateway=gateway,
            execute_tool=lambda name, arguments: {
                "tool": name, "status": "ok", "result": {"content": ""},
            },
            evidence_store=evidence,
            coverage=coverage,
            budget=BudgetLedger(BudgetLimits(4, 2, 1)),
            lease=lease,
            request_timeout_sec=5,
            max_tokens=512,
        )

    config = RuntimeConfig(
        review_deadline_sec=deadline_sec,
        model_request_timeout_sec=5,
        phase_shares=shares,
        concurrency=1,
        max_sessions=1,
        max_followup_sessions=1,
        session_limits=BudgetLimits(4, 2, 1),
    )
    with tempfile.TemporaryDirectory(prefix="deadline-cutoff-") as temp_dir:
        controller = ReviewController(
            session_factory=session_factory,
            clock=clock,
            artifact_output_root=Path(temp_dir),
        )
        result = controller.run(ReviewInputs(
            repository="example/deadline-cutoff",
            pr_number=2,
            base_sha="1" * 40,
            head_sha="2" * 40,
            topology=topology,
            classification={},
            policy=ReviewPolicy.minimal(),
            config=config,
            changed_files=("src/a.py",),
            artifact_path="artifact.json",
            publishing_mode="comment",
            pr_metadata={"title": "Deadline cutoff"},
        ))
    timing = result.artifact["timing"]
    expected_reserve = deadline_sec * shares.finalization / 100
    return {
        "deadline_violation": clock.elapsed > deadline_sec,
        "finalization_reserved": (
            timing["finalization_reserve_seconds"] == expected_reserve
        ),
        "cutoff_enforced": clock.elapsed == initial_cutoff - clock.started_at,
        "terminal": result.artifact["evaluation_status"] in {
            "complete", "degraded",
        },
        "elapsed_simulated_sec": clock.elapsed,
        "deadline_sec": deadline_sec,
        "provider_turns_consumed": gateway.index,
    }


def _anchor_notes(
    orders: object,
) -> tuple[tuple[ReviewNote, ...], tuple[ReviewNote, ...]]:
    if not isinstance(orders, list) or len(orders) != 2:
        raise ValueError("note_anchor_race must record two orders")
    obligation = CoverageObligation(
        obligation_id="OB-anchor",
        origin="replay",
        subject="src/anchor.py",
        required_evidence_categories=("implementation",),
        scope=("src/anchor.py",),
    )
    evidence = EvidenceStore()
    record = evidence.add_tool_result(
        session_id="anchor-session",
        tool="read_file",
        arguments={"path": "src/anchor.py"},
        result={"status": "ok", "content": "value = 1"},
        category="implementation",
    )

    def candidate(candidate_id: str, location: str) -> CandidateFinding:
        return CandidateFinding(
            candidate_id=candidate_id,
            root_cause_fingerprint="root:" + candidate_id,
            claim=f"Recorded {candidate_id} claim.",
            affected_location=location,
            causal_chain="The changed value reaches a user-visible response.",
            severity="minor",
            category="correctness",
            supporting_evidence_ids=(record.id,),
            related_obligation_ids=(obligation.id,),
            collector_session_id="anchor-session",
            model_identity="recorded-specialist",
            user_visible_consequence="The response can be incorrect.",
            manual_validation="Exercise the changed response.",
        )

    by_id = {
        item.candidate_id: item for item in (
            candidate("file-candidate", "src/anchor.py"),
            candidate("line-candidate", "src/anchor.py:1"),
        )
    }

    def notes_for(order: Sequence[str]) -> tuple[ReviewNote, ...]:
        values = tuple(by_id[item] for item in order)
        review = adjudicate_candidates(
            values,
            {"decisions": [
                {"candidate_id": item.candidate_id, "action": "keep"}
                for item in values
            ]},
            evidence,
            obligations={obligation.id: obligation},
            changed_files=("src/anchor.py",),
        )
        return build_review_notes(
            review,
            evidence,
            obligations={obligation.id: obligation},
            changed_files=("src/anchor.py",),
        )

    return notes_for(orders[0]), notes_for(orders[1])


def run_failure_injections(
    artifact: Mapping[str, Any],
    planner_calls: Sequence[str],
    scenarios: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Run recorded failures through public sessions and controllers."""
    no_progress_turns = scenarios["no_progress_resume"].get("responses")
    reconstruction_turns = scenarios["reconstruction"].get("responses")
    if not isinstance(no_progress_turns, list) or len(no_progress_turns) != 6:
        raise ValueError("no_progress_resume must record six OpenAI responses")
    if not isinstance(reconstruction_turns, list) or len(reconstruction_turns) != 2:
        raise ValueError("reconstruction must record two OpenAI responses")

    code = CoverageObligation(
        obligation_id="OB-code",
        origin="replay",
        subject="src/a.py",
        required_evidence_categories=("implementation",),
        scope=("src/a.py",),
    )
    tests = CoverageObligation(
        obligation_id="OB-tests",
        origin="replay",
        subject="tests/test_a.py",
        required_evidence_categories=("tests",),
        scope=("tests/test_a.py",),
    )
    assignment = SpecialistAssignment(
        assignment_id="failure-assignment",
        objective="Exercise durable failure handling.",
        primary_obligation_ids=("OB-code", "OB-tests"),
        seed_paths=("src/a.py", "tests/test_a.py"),
    )
    no_progress = _session(no_progress_turns, (code, tests), assignment)
    first = no_progress.explore()
    conversation_identity = id(no_progress.conversation)
    no_progress.apply_coverage_feedback(["OB-tests"])
    second = no_progress.explore()
    no_progress._recorded_gateway.assert_complete()  # type: ignore[attr-defined]

    reconstruction = _session(
        reconstruction_turns, (code, tests), assignment,
    )
    reconstruction.explore()
    checkpoint = reconstruction.latest_checkpoint
    reconstruction.recover(
        str(scenarios["reconstruction"].get("recovery_reason")),
    )
    reconstruction._recorded_gateway.assert_complete()  # type: ignore[attr-defined]
    recovery_usage = reconstruction.budget.snapshot()

    completion_raw = scenarios["completion_inversion"]
    orders = completion_raw.get("orders")
    if not isinstance(orders, list) or len(orders) != 2:
        raise ValueError("completion_inversion must record two completion orders")
    completion_runs = [
        _completion_controller_run(completion_raw, order)
        for order in orders
    ]
    coverage_stable = (
        completion_runs[0]["coverage"] == completion_runs[1]["coverage"]
    )
    evidence_stable = (
        completion_runs[0]["evidence"] == completion_runs[1]["evidence"]
    )
    orders_enforced = all(
        item["target_order"] == item["actual_order"]
        for item in completion_runs
    )
    deadline = _deadline_controller_run(scenarios["deadline_cutoff"])
    notes_one, notes_two = _anchor_notes(
        scenarios["note_anchor_race"].get("orders"),
    )
    note_projection = lambda values: tuple(  # noqa: E731
        (item.fingerprint, item.file, item.line) for item in values
    )
    critic_degraded = any(
        isinstance(item, Mapping) and item.get("component") == "critic"
        for item in artifact.get("degradation", [])
    )
    return {
        "no_progress_resume": {
            "same_session": id(no_progress.conversation) == conversation_identity,
            "budget_reset": second.budget.model_turns < first.budget.model_turns,
            "first_model_turns": first.budget.model_turns,
            "second_model_turns": second.budget.model_turns,
        },
        "reconstruction": {
            "reason": recovery_usage.recovery_reasons[0],
            "recoveries": recovery_usage.recoveries,
            "checkpoint_retained": reconstruction.latest_checkpoint == checkpoint,
        },
        "planner_repair": {
            "repair_requests": tuple(planner_calls).count("repair"),
            "source": artifact["assignment_plan"]["source"],
        },
        "failed_critic": {
            "terminal": artifact.get("evaluation_status") in {
                "complete", "degraded",
            },
            "fallback": "conservative" if critic_degraded else "not_observed",
        },
        "deadline_cutoff": deadline,
        "completion_inversion": {
            "stable_projection": coverage_stable and evidence_stable,
            "coverage_stable": coverage_stable,
            "evidence_stable": evidence_stable,
            "orders_enforced": orders_enforced,
            "controller_runs": len(completion_runs),
            "terminal": all(item["terminal"] for item in completion_runs),
        },
        "note_anchor_race": {
            "stable": note_projection(notes_one) == note_projection(notes_two),
            "anchor_types": sorted({
                "line" if item.line is not None else "file"
                for item in notes_one if item.file is not None
            }),
        },
    }
