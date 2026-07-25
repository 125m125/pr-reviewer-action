"""Deterministic adversarial probes used by offline specialist replay."""

from __future__ import annotations

import json
import time
from typing import Any, Mapping, Sequence

from pr_reviewer.conversation import Conversation

from .adjudication import adjudicate_candidates, build_review_notes
from .budget import BudgetLedger, RunDeadline, SessionLease
from .coverage import CoverageLedger
from .evidence import EvidenceStore
from .model_gateway import ModelTurnResult
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


def _turn(
    *,
    text: str = "",
    tool_calls: Sequence[Mapping[str, Any]] = (),
    finish_reason: str = "stop",
) -> ModelTurnResult:
    return ModelTurnResult(
        response={},
        tool_calls=tuple(dict(item) for item in tool_calls),
        text=text,
        text_source="content" if text else "none",
        finish_reason=finish_reason,
        usage={"prompt_tokens": 3, "completion_tokens": 2},
        request_diagnostics={},
    )


def _checkpoint_text(inspected: list[str], unresolved: list[str]) -> str:
    return json.dumps({
        "inspected": inspected,
        "unresolved": unresolved,
        "hypotheses": [],
        "candidate_finding_ids": [],
        "invariants_evaluated": [],
        "unknowns": unresolved,
        "proposed_next_actions": [],
    }, sort_keys=True)


def _tool_turn(call_id: str, path: str) -> ModelTurnResult:
    return _turn(
        tool_calls=({
            "id": call_id,
            "name": "read_file",
            "arguments": json.dumps({"path": path}, sort_keys=True),
        },),
        finish_reason="tool_calls",
    )


def _session(
    responses: Sequence[ModelTurnResult],
    obligations: Sequence[CoverageObligation],
    assignment: SpecialistAssignment,
) -> SpecialistSession:
    evidence = EvidenceStore()

    class Gateway:
        def __init__(self) -> None:
            self.responses = list(responses)

        def complete(self, request: object) -> ModelTurnResult:
            del request
            if not self.responses:
                raise AssertionError("failure-injection provider turns exhausted")
            return self.responses.pop(0)

    def execute(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = {
            "tool": name,
            "status": "ok",
            "result": {"content": f"contents:{arguments.get('path', '')}"},
        }
        evidence.add_tool_result(
            session_id="failure-session",
            tool=name,
            arguments=arguments,
            result=result,
            category=(
                "tests" if "test" in str(arguments.get("path", ""))
                else "implementation"
            ),
        )
        return result

    return SpecialistSession(
        session_id="failure-session",
        assignment=assignment,
        conversation=Conversation(system="Recorded failure replay."),
        gateway=Gateway(),
        execute_tool=execute,
        evidence_store=evidence,
        coverage=CoverageLedger(obligations),
        budget=BudgetLedger(BudgetLimits(12, 8, 1)),
        lease=SessionLease(RunPhase.FOLLOWUP, time.monotonic() + 60),
        request_timeout_sec=5,
        max_tokens=512,
        max_no_progress_streak=2,
    )


def _completion_projection(
    orders: object,
) -> tuple[tuple[tuple[str, str], ...], ...]:
    expected = [
        ["assignment-b", "assignment-a"],
        ["assignment-a", "assignment-b"],
    ]
    if orders != expected:
        raise ValueError("completion_inversion orders are invalid")
    stores: dict[str, EvidenceStore] = {}
    for session_id, path, content in (
        ("assignment-a", "src/a.py", "a"),
        ("assignment-b", "src/b.py", "b"),
    ):
        store = EvidenceStore()
        store.add_tool_result(
            session_id=session_id,
            tool="read_file",
            arguments={"path": path},
            result={"status": "ok", "content": content},
            category="implementation",
        )
        stores[session_id] = store
    projections = []
    for order in expected:
        merged = EvidenceStore()
        for assignment_id in order:
            merged.merge_completed_snapshot(stores[assignment_id].snapshot())
        projections.append(tuple(
            (item.id, item.content_hash) for item in merged.snapshot().records
        ))
    return tuple(projections)


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
    """Run recorded failure sequences against public runtime components."""
    no_progress_turns = scenarios["no_progress_resume"].get("sequence")
    reconstruction_turns = scenarios["reconstruction"].get("sequence")
    if not isinstance(no_progress_turns, list) or len(no_progress_turns) != 5:
        raise ValueError("no_progress_resume must record exactly five transitions")
    if not isinstance(reconstruction_turns, list) or len(reconstruction_turns) != 3:
        raise ValueError("reconstruction must record exactly three transitions")

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
    no_progress = _session([
        _tool_turn("call-a", no_progress_turns[0]["path"]),
        _tool_turn("call-a-duplicate-1", no_progress_turns[1]["path"]),
        _tool_turn("call-a-duplicate-2", no_progress_turns[2]["path"]),
        _turn(text=_checkpoint_text(["src/a.py"], ["OB-tests"])),
        _tool_turn("call-tests", no_progress_turns[4]["path"]),
        _turn(text=_checkpoint_text(["src/a.py", "tests/test_a.py"], [])),
    ], (code, tests), assignment)
    first = no_progress.explore()
    conversation_identity = id(no_progress.conversation)
    no_progress.apply_coverage_feedback(["OB-tests"])
    second = no_progress.explore()

    reconstruction = _session([
        _tool_turn("call-reconstruct", reconstruction_turns[0]["path"]),
        _turn(text=_checkpoint_text(["src/a.py"], ["OB-tests"])),
    ], (code, tests), assignment)
    reconstruction.explore()
    checkpoint = reconstruction.latest_checkpoint
    reconstruction.recover(str(reconstruction_turns[2].get("reason")))
    recovery_usage = reconstruction.budget.snapshot()

    deadline_raw = scenarios["deadline_cutoff"]
    deadline = RunDeadline(
        0.0,
        float(deadline_raw["deadline_sec"]),
        PhaseShares(**deadline_raw["phase_shares"]),
    )
    exploration_cutoff = deadline.cutoff_for(RunPhase.FOLLOWUP)
    reserve = deadline.cutoff_for(RunPhase.FINALIZATION) - exploration_cutoff
    completion = _completion_projection(
        scenarios["completion_inversion"].get("orders"),
    )
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
            "terminal": artifact.get("evaluation_status") in {"complete", "degraded"},
            "fallback": "conservative" if critic_degraded else "not_observed",
        },
        "deadline_cutoff": {
            "deadline_violation": exploration_cutoff > deadline.deadline_sec,
            "finalization_reserved": reserve == (
                deadline.deadline_sec * deadline.phase_shares.finalization / 100
            ),
        },
        "completion_inversion": {
            "stable_projection": completion[0] == completion[1],
        },
        "note_anchor_race": {
            "stable": note_projection(notes_one) == note_projection(notes_two),
            "anchor_types": sorted({
                "line" if item.line is not None else "file"
                for item in notes_one if item.file is not None
            }),
        },
    }
