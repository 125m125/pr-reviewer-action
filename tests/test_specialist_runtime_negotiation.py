from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from pr_reviewer.specialist_runtime.assignments import Assignment, validate_assignment_plan
from pr_reviewer.specialist_runtime.coverage import (
    CoverageLedger,
    SessionOwnership,
    evidence_satisfies_obligation,
    reconcile_wave,
)
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.negotiation import (
    NegotiationError,
    NegotiationState,
    SessionResources,
    fallback_next_action,
    validate_negotiation,
)
from pr_reviewer.specialist_runtime.policy import RuntimeConfig
from pr_reviewer.specialist_runtime.types import (
    CoverageObligation,
    ObligationStatus,
    SessionCheckpoint,
    SessionState,
    SpecialistAssignment,
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
    new_session_turn_cap: int = 2,
    new_session_lease_remaining_sec: float = 100.0,
    remaining_deadline_sec: float = 100.0,
    assignments: tuple[Assignment | SpecialistAssignment, ...] | None = None,
    session_ownership: tuple[SessionOwnership, ...] | None = None,
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
        assignments=assignments or (
            assignment("A1", ("OB1",)), assignment("A2", ("OB2",)),
        ),
        checkpoints=(),
        session_ownership=session_ownership or (
            SessionOwnership("S1", "A1", primary_obligation_ids=("OB1",)),
            SessionOwnership("S2", "A2", primary_obligation_ids=("OB2",)),
        ),
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
        new_session_turn_cap=new_session_turn_cap,
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
        wave_start_coverage=ledger.snapshot(),
        checkpoints=(checkpoint,),
        evidence=store.snapshot(),
        assignments=(assignment("S1", ("OB1", "OB2")),),
        session_ownership=(SessionOwnership(
            session_id="S1",
            assignment_id="S1",
            primary_obligation_ids=("OB1", "OB2"),
        ),),
    )

    assert result.snapshot.obligation_statuses == (
        ("OB1", ObligationStatus.COVERED),
        ("OB2", ObligationStatus.PENDING),
    )
    assert result.newly_covered_obligation_ids == ("OB1",)
    assert result.uncovered_obligation_ids == ("OB2",)


def test_shared_path_with_wrong_evidence_category_does_not_satisfy_obligation():
    required = obligation("OB1", category="tests", path="shared.py")
    store = EvidenceStore()
    implementation = store.add_tool_result(
        session_id="session-1",
        tool="read_file",
        arguments={"path": "shared.py"},
        result={"status": "ok", "content": "implementation"},
        category="implementation",
    )

    assert evidence_satisfies_obligation(implementation, required) is False


def test_reconciliation_maps_durable_session_to_specialist_assignment_ownership():
    required = obligation("OB1")
    ledger = CoverageLedger((required,))
    specialist_assignment = SpecialistAssignment(
        assignment_id="assignment-A",
        objective="Inspect tests",
        primary_obligation_ids=("OB1",),
    )
    ownership = SessionOwnership(
        session_id="durable-session-9",
        assignment_id=specialist_assignment.assignment_id,
        primary_obligation_ids=specialist_assignment.primary_obligation_ids,
        independent_obligation_ids=specialist_assignment.independent_obligation_ids,
    )
    store = EvidenceStore()
    evidence = store.add_tool_result(
        session_id="durable-session-9",
        tool="read_file",
        arguments={"path": "tests/test_a.py"},
        result={"status": "ok", "content": "assert behavior"},
        category="tests",
    )

    result = reconcile_wave(
        ledger,
        wave_start_coverage=ledger.snapshot(),
        checkpoints=(SessionCheckpoint(
            session_id="durable-session-9",
            state=SessionState.CHECKPOINT,
            evidence_ids=(evidence.id,),
        ),),
        evidence=store.snapshot(),
        assignments=(specialist_assignment,),
        session_ownership=(ownership,),
    )

    assert result.snapshot.obligation_statuses == (("OB1", ObligationStatus.COVERED),)


def test_independent_owner_cannot_satisfy_obligation_with_imported_evidence():
    required = replace(obligation("OB1"), requires_independent_verification=True)
    ledger = CoverageLedger((required,))
    specialist_assignment = SpecialistAssignment(
        assignment_id="independent-assignment",
        objective="Independently inspect tests",
        independent_obligation_ids=("OB1",),
    )
    ownership = SessionOwnership(
        session_id="independent-session",
        assignment_id=specialist_assignment.assignment_id,
        independent_obligation_ids=("OB1",),
    )
    store = EvidenceStore()
    imported = store.add_tool_result(
        session_id="primary-session",
        tool="read_file",
        arguments={"path": "tests/test_a.py"},
        result={"status": "ok", "content": "primary collection"},
        category="tests",
    )
    store.import_into_session("independent-session", imported.id)
    wave_start_coverage = ledger.snapshot()
    # SpecialistSession may optimistically attach checkpoint evidence before the
    # controller's post-wave reconciliation. The controller must remove it.
    ledger.attach_evidence("OB1", imported.id)

    result = reconcile_wave(
        ledger,
        wave_start_coverage=wave_start_coverage,
        checkpoints=(SessionCheckpoint(
            session_id="independent-session",
            state=SessionState.CHECKPOINT,
            evidence_ids=(imported.id,),
            imported_evidence_ids=(imported.id,),
        ),),
        evidence=store.snapshot(),
        assignments=(specialist_assignment,),
        session_ownership=(ownership,),
    )

    assert result.snapshot.obligation_statuses == (("OB1", ObligationStatus.PENDING),)


def test_independent_owner_fresh_collection_satisfies_independent_obligation():
    required = replace(obligation("OB1"), requires_independent_verification=True)
    ledger = CoverageLedger((required,))
    specialist_assignment = SpecialistAssignment(
        assignment_id="independent-assignment",
        objective="Independently inspect tests",
        independent_obligation_ids=("OB1",),
    )
    ownership = SessionOwnership(
        session_id="independent-session",
        assignment_id=specialist_assignment.assignment_id,
        independent_obligation_ids=("OB1",),
    )
    store = EvidenceStore()
    fresh = store.add_tool_result(
        session_id="independent-session",
        tool="read_file",
        arguments={"path": "tests/test_a.py"},
        result={"status": "ok", "content": "independent collection"},
        category="tests",
    )

    result = reconcile_wave(
        ledger,
        wave_start_coverage=ledger.snapshot(),
        checkpoints=(SessionCheckpoint(
            session_id="independent-session",
            state=SessionState.CHECKPOINT,
            evidence_ids=(fresh.id,),
        ),),
        evidence=store.snapshot(),
        assignments=(specialist_assignment,),
        session_ownership=(ownership,),
    )

    assert result.snapshot.obligation_statuses == (("OB1", ObligationStatus.COVERED),)


def test_independent_owner_identical_fresh_read_is_not_collapsed_to_import():
    required = replace(obligation("OB1"), requires_independent_verification=True)
    ledger = CoverageLedger((required,))
    assignment_value = SpecialistAssignment(
        assignment_id="independent-assignment",
        objective="Independently inspect tests",
        independent_obligation_ids=("OB1",),
    )
    ownership = SessionOwnership(
        session_id="independent-session",
        assignment_id=assignment_value.assignment_id,
        independent_obligation_ids=("OB1",),
    )
    store = EvidenceStore()
    store.add_tool_result(
        session_id="primary-session", tool="read_file",
        arguments={"path": "tests/test_a.py"},
        result={"status": "ok", "content": "identical"}, category="tests",
    )
    record, collection = store.add_tool_result_with_collection(
        session_id="independent-session", tool="read_file",
        arguments={"path": "tests/test_a.py"},
        result={"status": "ok", "content": "identical"}, category="tests",
    )
    store.associate_collection(
        collection.id, obligation_id="OB1", categories=("tests",),
    )

    result = reconcile_wave(
        ledger,
        wave_start_coverage=ledger.snapshot(),
        checkpoints=(SessionCheckpoint(
            session_id="independent-session",
            state=SessionState.CHECKPOINT,
            evidence_ids=(record.id,),
        ),),
        evidence=store.snapshot(),
        assignments=(assignment_value,),
        session_ownership=(ownership,),
    )

    assert result.snapshot.obligation_statuses == (
        ("OB1", ObligationStatus.COVERED),
    )


def test_wave_start_baseline_retains_prior_coverage_and_counts_current_gain():
    obligations = (
        obligation("OB1"),
        obligation("OB2", category="implementation", path="src/a.py"),
    )
    store = EvidenceStore()
    prior = store.add_tool_result(
        session_id="prior-session",
        tool="read_file",
        arguments={"path": "tests/test_a.py"},
        result={"status": "ok", "content": "prior test evidence"},
        category="tests",
    )
    current = store.add_tool_result(
        session_id="current-session",
        tool="read_file",
        arguments={"path": "src/a.py"},
        result={"status": "ok", "content": "current implementation evidence"},
        category="implementation",
    )
    baseline_ledger = CoverageLedger(obligations)
    baseline_ledger.attach_evidence("OB1", prior.id)
    live_ledger = CoverageLedger(obligations)
    live_ledger.attach_evidence("OB1", prior.id)
    live_ledger.attach_evidence("OB2", current.id)  # optimistic current-wave attachment
    assignments = (
        SpecialistAssignment(
            assignment_id="prior-assignment", objective="Prior tests",
            primary_obligation_ids=("OB1",),
        ),
        SpecialistAssignment(
            assignment_id="current-assignment", objective="Current implementation",
            primary_obligation_ids=("OB2",),
        ),
    )
    ownership = (
        SessionOwnership(
            "prior-session", "prior-assignment", primary_obligation_ids=("OB1",),
        ),
        SessionOwnership(
            "current-session", "current-assignment", primary_obligation_ids=("OB2",),
        ),
    )

    result = reconcile_wave(
        live_ledger,
        wave_start_coverage=baseline_ledger.snapshot(),
        checkpoints=(SessionCheckpoint(
            session_id="current-session",
            state=SessionState.CHECKPOINT,
            evidence_ids=(current.id,),
        ),),
        evidence=store.snapshot(),
        assignments=assignments,
        session_ownership=ownership,
    )

    assert result.snapshot.obligation_statuses == (
        ("OB1", ObligationStatus.COVERED),
        ("OB2", ObligationStatus.COVERED),
    )
    assert result.newly_covered_obligation_ids == ("OB2",)


def test_unknown_checkpoint_session_fails_closed():
    required = obligation("OB1")
    ledger = CoverageLedger((required,))
    specialist_assignment = SpecialistAssignment(
        assignment_id="A1", objective="Tests", primary_obligation_ids=("OB1",),
    )

    with pytest.raises(ValueError, match="unknown durable session"):
        reconcile_wave(
            ledger,
            wave_start_coverage=ledger.snapshot(),
            checkpoints=(SessionCheckpoint(
                session_id="rogue-session", state=SessionState.CHECKPOINT,
            ),),
            evidence=EvidenceStore().snapshot(),
            assignments=(specialist_assignment,),
            session_ownership=(SessionOwnership(
                "known-session", "A1", primary_obligation_ids=("OB1",),
            ),),
        )


def test_planner_secondary_owner_can_be_selected_for_consultation():
    assignments = (
        assignment("A1", ("OB1",), primary=("OB1",)),
        assignment("A2", ("OB1",), primary=()),
    )
    ownership = (
        SessionOwnership("S1", "A1", primary_obligation_ids=("OB1",)),
        SessionOwnership("S2", "A2", secondary_obligation_ids=("OB1",)),
    )
    state = state_for(
        assignments=assignments,
        session_ownership=ownership,
        resources=(
            SessionResources("S1", remaining_model_turns=3, lease_remaining_sec=100.0),
            SessionResources("S2", remaining_model_turns=3, lease_remaining_sec=100.0),
        ),
    )
    raw = resume_raw(kind="consult", session_id="S2", estimated_turns=1)

    proposal = validate_negotiation(raw, state)

    assert proposal.actions[0].session_id == "S2"


def test_planner_independent_owner_fresh_evidence_satisfies_obligation():
    required = replace(obligation("OB1"), requires_independent_verification=True)
    ledger = CoverageLedger((required,))
    planner_assignment = assignment("A-independent", ("OB1",), primary=())
    ownership = SessionOwnership(
        "independent-session", "A-independent",
        secondary_obligation_ids=("OB1",),
        independent_obligation_ids=("OB1",),
    )
    store = EvidenceStore()
    fresh = store.add_tool_result(
        session_id="independent-session",
        tool="read_file",
        arguments={"path": "tests/test_a.py"},
        result={"status": "ok", "content": "fresh independent evidence"},
        category="tests",
    )

    result = reconcile_wave(
        ledger,
        wave_start_coverage=ledger.snapshot(),
        checkpoints=(SessionCheckpoint(
            session_id="independent-session",
            state=SessionState.CHECKPOINT,
            evidence_ids=(fresh.id,),
        ),),
        evidence=store.snapshot(),
        assignments=(planner_assignment,),
        session_ownership=(ownership,),
    )

    assert result.snapshot.obligation_statuses == (("OB1", ObligationStatus.COVERED),)


def test_validated_sole_independent_owner_is_primary_and_independent_collector():
    required = replace(
        obligation("OB1"),
        requires_independent_verification=True,
        recipe_id="independent-check",
        recipe_execution="independent",
    )
    plan = validate_assignment_plan({"assignments": [{
        "id": "A-independent",
        "title": "Independent verification",
        "objective": "Independently verify the relevant test behavior.",
        "obligation_ids": ["OB1"],
        "lenses": ["independent-verification"],
        "seed_paths": ["tests/test_a.py"],
        "boundary_paths": [],
        "expected_evidence": ["tests"],
        "estimated_turns": 1,
        "priority": "normal",
        "overlap_justification": "",
    }]}, (required,), {}, RuntimeConfig())
    planner_assignment = plan.assignments[0]
    assert planner_assignment.primary_obligation_ids == ("OB1",)
    ownership = SessionOwnership(
        "independent-session",
        planner_assignment.id,
        primary_obligation_ids=("OB1",),
        independent_obligation_ids=("OB1",),
    )
    assert ownership.obligation_ids == ("OB1",)

    fresh_store = EvidenceStore()
    fresh = fresh_store.add_tool_result(
        session_id="independent-session",
        tool="read_file",
        arguments={"path": "tests/test_a.py"},
        result={"status": "ok", "content": "fresh independent collection"},
        category="tests",
    )
    fresh_ledger = CoverageLedger((required,))
    fresh_result = reconcile_wave(
        fresh_ledger,
        wave_start_coverage=fresh_ledger.snapshot(),
        checkpoints=(SessionCheckpoint(
            session_id="independent-session",
            state=SessionState.CHECKPOINT,
            evidence_ids=(fresh.id,),
        ),),
        evidence=fresh_store.snapshot(),
        assignments=plan.assignments,
        session_ownership=(ownership,),
    )

    imported_store = EvidenceStore()
    imported = imported_store.add_tool_result(
        session_id="other-collector",
        tool="read_file",
        arguments={"path": "tests/test_a.py"},
        result={"status": "ok", "content": "other collector evidence"},
        category="tests",
    )
    imported_store.import_into_session("independent-session", imported.id)
    imported_ledger = CoverageLedger((required,))
    imported_result = reconcile_wave(
        imported_ledger,
        wave_start_coverage=imported_ledger.snapshot(),
        checkpoints=(SessionCheckpoint(
            session_id="independent-session",
            state=SessionState.CHECKPOINT,
            evidence_ids=(imported.id,),
            imported_evidence_ids=(imported.id,),
        ),),
        evidence=imported_store.snapshot(),
        assignments=plan.assignments,
        session_ownership=(ownership,),
    )

    assert fresh_result.snapshot.obligation_statuses == (
        ("OB1", ObligationStatus.COVERED),
    )
    assert imported_result.snapshot.obligation_statuses == (
        ("OB1", ObligationStatus.PENDING),
    )


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


def test_negotiation_uses_explicit_session_to_specialist_assignment_ownership():
    specialist_assignment = SpecialistAssignment(
        assignment_id="assignment-A",
        objective="Inspect tests",
        primary_obligation_ids=("OB1",),
    )
    state = state_for(
        current_session_count=1,
        assignments=(specialist_assignment,),
        session_ownership=(SessionOwnership(
            session_id="durable-session-9",
            assignment_id="assignment-A",
            primary_obligation_ids=("OB1",),
        ),),
        resources=(SessionResources(
            "durable-session-9", remaining_model_turns=3, lease_remaining_sec=100.0,
        ),),
    )

    proposal = validate_negotiation(resume_raw(session_id="durable-session-9"), state)

    assert proposal.actions[0].session_id == "durable-session-9"


def test_multiple_actions_cannot_target_same_durable_session_even_when_disjoint():
    shared_assignment = assignment("A1", ("OB1", "OB2"))
    state = state_for(
        current_session_count=1,
        assignments=(shared_assignment,),
        session_ownership=(SessionOwnership(
            session_id="S1",
            assignment_id="A1",
            primary_obligation_ids=("OB1", "OB2"),
        ),),
        resources=(SessionResources(
            "S1", remaining_model_turns=4, lease_remaining_sec=100.0,
        ),),
    )
    raw = {"actions": [
        resume_raw()["actions"][0],
        resume_raw(
            obligation_ids=["OB2"], expected_evidence=["implementation"], estimated_turns=1,
        )["actions"][0],
    ]}

    with pytest.raises(NegotiationError, match="same durable session"):
        validate_negotiation(raw, state)


def test_new_session_is_bounded_by_controller_owned_per_session_turn_cap():
    raw = {"actions": [{
        "kind": "new_session",
        "obligation_ids": ["OB1"],
        "expected_evidence": ["tests"],
        "estimated_turns": 3,
        "reason": "Collect missing tests evidence.",
    }]}

    with pytest.raises(NegotiationError, match="per-session turn cap"):
        validate_negotiation(raw, state_for(new_session_turn_cap=2))
    raw["actions"][0]["new_session_turn_cap"] = 99
    with pytest.raises(NegotiationError, match="unsupported fields"):
        validate_negotiation(raw, state_for(new_session_turn_cap=2))


def test_controller_sorting_makes_equivalent_action_orders_identical():
    resume = resume_raw()["actions"][0]
    unknown = {
        "kind": "record_unknown",
        "obligation_ids": ["OB2"],
        "expected_evidence": ["implementation"],
        "estimated_turns": 0,
        "reason": "The external source is unavailable.",
    }

    forward = validate_negotiation({"actions": [resume, unknown]}, state_for())
    reverse = validate_negotiation({"actions": [unknown, resume]}, state_for())

    assert forward == reverse
    assert tuple(item.obligation_ids for item in forward.actions) == (("OB2",), ("OB1",))


@pytest.mark.parametrize("kind", ["new_session", "record_unknown"])
def test_non_session_actions_reject_session_id_even_when_null(kind):
    raw = {
        "kind": kind,
        "session_id": None,
        "obligation_ids": ["OB1"],
        "expected_evidence": ["tests"],
        "estimated_turns": 0 if kind == "record_unknown" else 1,
        "reason": "Bounded action.",
    }

    with pytest.raises(NegotiationError, match="must omit session_id"):
        validate_negotiation({"actions": [raw]}, state_for())


@pytest.mark.parametrize("kind", ["resume", "consult"])
def test_session_actions_require_session_id_field(kind):
    raw = {
        "kind": kind,
        "obligation_ids": ["OB1"],
        "expected_evidence": ["tests"],
        "estimated_turns": 1,
        "reason": "Bounded action.",
    }

    with pytest.raises(NegotiationError, match="requires session_id field"):
        validate_negotiation({"actions": [raw]}, state_for())


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
