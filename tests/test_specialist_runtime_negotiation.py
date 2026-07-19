from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from pr_reviewer.specialist_runtime.assignments import Assignment
from pr_reviewer.specialist_runtime.coverage import CoverageLedger, reconcile_wave
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.negotiation import (
    NegotiationError,
    NegotiationState,
    SessionResources,
    fallback_next_action,
    validate_negotiation,
)
from pr_reviewer.specialist_runtime.types import (
    CoverageObligation,
    ObligationStatus,
    SessionCheckpoint,
    SessionState,
)


def obligation(
    obligation_id: str,
    *,
    risk: str = "normal",
    category: str = "tests",
    path: str = "tests/test_a.py",
    unresolved_policy: str = "record_unknown",
) -> CoverageObligation:
    return CoverageObligation(
        obligation_id=obligation_id,
        origin="test",
        subject=path,
        required_evidence_categories=(category,),
        satisfaction_predicates=("recorded_evidence",),
        risk_tier=risk,
        unresolved_policy=unresolved_policy,
        scope=(path,),
    )


def assignment(
    session_id: str,
    obligation_ids: tuple[str, ...],
    *,
    primary: tuple[str, ...] | None = None,
) -> Assignment:
    return Assignment(
        id=session_id,
        title=session_id,
        objective=f"Review {', '.join(obligation_ids)}",
        obligation_ids=obligation_ids,
        recipe_ids=(),
        lenses=("correctness",),
        seed_paths=(),
        boundary_paths=(),
        expected_evidence=("tests", "implementation"),
        estimated_turns=2,
        priority="high",
        primary_obligation_ids=obligation_ids if primary is None else primary,
    )


def state_for(
    *,
    covered: tuple[str, ...] = (),
    resources: tuple[SessionResources, ...] | None = None,
    current_session_count: int = 2,
    max_sessions: int = 3,
    followup_sessions_started: int = 0,
    max_followup_sessions: int = 1,
    new_session_turns_remaining: int = 4,
    new_session_lease_remaining_sec: float = 100.0,
    remaining_deadline_sec: float = 100.0,
) -> NegotiationState:
    obligations = (
        obligation("OB1", risk="high"),
        obligation("OB2", risk="critical", category="implementation", path="src/a.py"),
    )
    ledger = CoverageLedger(obligations)
    for obligation_id in covered:
        ledger.attach_evidence(obligation_id, f"E-{obligation_id}")
    return NegotiationState(
        obligations=obligations,
        coverage=ledger.snapshot(),
        assignments=(assignment("S1", ("OB1",)), assignment("S2", ("OB2",))),
        checkpoints=(),
        session_resources=resources or (
            SessionResources("S1", remaining_model_turns=3, lease_remaining_sec=100.0),
            SessionResources("S2", remaining_model_turns=3, lease_remaining_sec=100.0),
        ),
        remaining_deadline_sec=remaining_deadline_sec,
        seconds_per_turn=10.0,
        current_session_count=current_session_count,
        max_sessions=max_sessions,
        followup_sessions_started=followup_sessions_started,
        max_followup_sessions=max_followup_sessions,
        new_session_turns_remaining=new_session_turns_remaining,
        new_session_lease_remaining_sec=new_session_lease_remaining_sec,
    )


def resume_raw(**updates):
    raw = {
        "kind": "resume",
        "session_id": "S1",
        "obligation_ids": ["OB1"],
        "expected_evidence": ["tests"],
        "estimated_turns": 2,
        "reason": "The owner inspected implementation but not tests.",
    }
    raw.update(updates)
    return {"actions": [raw]}


def test_reconcile_wave_uses_evidence_predicates_not_declared_coverage():
    obligations = (
        obligation("OB1"),
        obligation("OB2", category="implementation", path="src/a.py"),
    )
    ledger = CoverageLedger(obligations)
    store = EvidenceStore()
    evidence = store.add_tool_result(
        session_id="S1",
        tool="read_file",
        arguments={"path": "tests/test_a.py"},
        result={"status": "ok", "content": "assert behavior"},
        category="tests",
    )
    checkpoint = SessionCheckpoint(
        session_id="S1",
        state=SessionState.CHECKPOINT,
        evidence_ids=(evidence.id,),
        obligation_statuses=(("OB2", ObligationStatus.COVERED),),
    )

    result = reconcile_wave(
        ledger,
        checkpoints=(checkpoint,),
        evidence=store.snapshot(),
        assignments=(assignment("S1", ("OB1", "OB2")),),
    )

    assert result.snapshot.obligation_statuses == (
        ("OB1", ObligationStatus.COVERED),
        ("OB2", ObligationStatus.PENDING),
    )
    assert result.newly_covered_obligation_ids == ("OB1",)
    assert result.uncovered_obligation_ids == ("OB2",)


@pytest.mark.parametrize(
    "action",
    ["delete_obligation", "mark_covered", "grant_budget", "reset_budget", "extend_deadline"],
)
def test_negotiator_cannot_change_controller_authority(action):
    raw = {"actions": [{"kind": action, "obligation_ids": ["OB1"]}]}

    with pytest.raises(NegotiationError):
        validate_negotiation(raw, state_for())


@pytest.mark.parametrize(
    "field",
    ["delete_obligation", "mark_covered", "grant_budget", "reset_budget", "deadline_sec"],
)
def test_negotiator_cannot_smuggle_controller_mutations_into_valid_action(field):
    with pytest.raises(NegotiationError, match="unsupported fields"):
        validate_negotiation(resume_raw(**{field: 99}), state_for())


def test_valid_resume_preserves_owner_and_returns_immutable_typed_proposal():
    proposal = validate_negotiation(resume_raw(), state_for())

    assert proposal.actions[0].kind == "resume"
    assert proposal.actions[0].session_id == "S1"
    assert proposal.actions[0].obligation_ids == ("OB1",)
    assert proposal.actions[0].expected_coverage_gain == 1
    with pytest.raises(FrozenInstanceError):
        proposal.actions[0].reason = "mutated"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (resume_raw(obligation_ids=["OB2"]), "primary owner"),
        (resume_raw(expected_evidence=["implementation"]), "expected new evidence"),
        (resume_raw(estimated_turns=4), "remaining model-turn budget"),
    ],
)
def test_resume_requires_ownership_evidence_gain_and_budget(raw, message):
    with pytest.raises(NegotiationError, match=message):
        validate_negotiation(raw, state_for())


def test_proposals_cannot_repeat_covered_work_or_exceed_lease_or_deadline():
    with pytest.raises(NegotiationError, match="already covered"):
        validate_negotiation(resume_raw(), state_for(covered=("OB1",)))

    resources = (
        SessionResources("S1", remaining_model_turns=3, lease_remaining_sec=15.0),
        SessionResources("S2", remaining_model_turns=3, lease_remaining_sec=100.0),
    )
    with pytest.raises(NegotiationError, match="lease"):
        validate_negotiation(resume_raw(), state_for(resources=resources))

    with pytest.raises(NegotiationError, match="deadline"):
        validate_negotiation(resume_raw(), state_for(remaining_deadline_sec=15.0))


def test_new_session_requires_hard_and_followup_capacity():
    raw = {"actions": [{
        "kind": "new_session",
        "obligation_ids": ["OB1"],
        "expected_evidence": ["tests"],
        "estimated_turns": 1,
        "reason": "Existing owners have no useful budget.",
    }]}

    proposal = validate_negotiation(raw, state_for())
    assert proposal.actions[0].session_id is None

    with pytest.raises(NegotiationError, match="session capacity"):
        validate_negotiation(raw, state_for(current_session_count=3))
    with pytest.raises(NegotiationError, match="follow-up session capacity"):
        validate_negotiation(raw, state_for(followup_sessions_started=1))


def test_consult_and_policy_governed_unknown_are_the_other_exact_actions():
    consultation = validate_negotiation({"actions": [{
        "kind": "consult",
        "session_id": "S1",
        "obligation_ids": ["OB1"],
        "expected_evidence": ["tests"],
        "estimated_turns": 1,
        "reason": "Ask the assigned specialist for one bounded verification.",
    }]}, state_for()).actions[0]
    unknown = validate_negotiation({"actions": [{
        "kind": "record_unknown",
        "obligation_ids": ["OB1"],
        "expected_evidence": ["tests"],
        "estimated_turns": 0,
        "reason": "The required source is externally unavailable.",
    }]}, state_for()).actions[0]

    assert consultation.kind == "consult"
    assert unknown.kind == "record_unknown"
    assert unknown.expected_coverage_gain == 1
    assert unknown.resolution_policy == "record_unknown"


def test_fallback_resumes_useful_primary_owner_for_highest_risk_gap():
    action = fallback_next_action(state_for())

    assert action.kind == "resume"
    assert action.session_id == "S2"
    assert action.obligation_ids == ("OB2",)
    assert action.expected_evidence == ("implementation",)


def test_fallback_creates_one_narrow_session_when_primary_owner_is_infeasible():
    resources = (
        SessionResources("S1", remaining_model_turns=3, lease_remaining_sec=100.0),
        SessionResources("S2", remaining_model_turns=0, lease_remaining_sec=100.0),
    )

    action = fallback_next_action(state_for(resources=resources))

    assert action.kind == "new_session"
    assert action.session_id is None
    assert action.obligation_ids == ("OB2",)


def test_fallback_records_policy_governed_unknown_when_no_work_is_feasible():
    resources = (
        SessionResources("S1", remaining_model_turns=0, lease_remaining_sec=0.0),
        SessionResources("S2", remaining_model_turns=0, lease_remaining_sec=0.0),
    )

    action = fallback_next_action(
        state_for(resources=resources, current_session_count=3, max_sessions=3)
    )

    assert action.kind == "record_unknown"
    assert action.obligation_ids == ("OB2",)
    assert action.resolution_policy == "record_unknown"
    assert action.estimated_turns == 0


def test_fallback_order_is_stable_for_equal_risk_obligations():
    original = state_for()
    equal_risk = tuple(replace(item, risk_tier="high") for item in reversed(original.obligations))
    state = replace(original, obligations=equal_risk)

    assert fallback_next_action(state).obligation_ids == ("OB1",)
