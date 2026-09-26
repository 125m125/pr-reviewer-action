import pytest

from pr_reviewer.specialist_runtime.assignments import Assignment, ObligationBrief
from pr_reviewer.specialist_runtime.delegation import (
    DelegationDecision,
    DelegationLimits,
    DelegationProposal,
    DelegationTransfer,
    prepare_delegation_transfer,
    validate_delegation,
)
from pr_reviewer.specialist_runtime.types import InvestigationLead


def _assignment(*, depth=0):
    return Assignment(
        id="backend",
        title="Backend",
        objective="Review backend behavior.",
        obligation_ids=("OB-sync", "OB-cast"),
        primary_obligation_ids=("OB-sync", "OB-cast"),
        recipe_ids=("backend",),
        lenses=("correctness",),
        seed_paths=("contracts/api.yaml",),
        boundary_paths=("contracts/**",),
        expected_evidence=("implementation",),
        estimated_turns=12,
        priority="high",
        obligation_briefs=(
            ObligationBrief(
                obligation_id="OB-sync",
                subject="sync",
                explanation="Review synchronization.",
                risk_tier="high",
                required_evidence=("implementation",),
                satisfaction_predicates=(),
                scope=("backend/sync.py",),
            ),
            ObligationBrief(
                obligation_id="OB-cast",
                subject="cast",
                explanation="Review cast mapping.",
                risk_tier="normal",
                required_evidence=("implementation",),
                satisfaction_predicates=(),
                scope=("backend/cast.py",),
            ),
        ),
        owner_component_id="backend",
        owned_changed_paths=("backend/sync.py", "backend/cast.py"),
        delegation_depth=depth,
    )


def _limits(**changes):
    values = {
        "remaining_session_capacity": 1,
        "accepted_evidence_ids": frozenset({"evidence:cast"}),
        "authorized_reference_paths": frozenset({"contracts/api.yaml"}),
    }
    values.update(changes)
    return DelegationLimits(**values)


def _raw(**changes):
    values = {
        "question": "Check cast/person mapping identity and cardinality.",
        "changed_paths": ["backend/cast.py"],
        "targets": ["O2"],
        "reference_paths": ["contracts/api.yaml"],
        "reason": "The mapping is separable from synchronization.",
        "evidence_ids": ["evidence:cast"],
        "observations": ["The mapper changed independently."],
        "expected_result": "Report mapping defects or confirm the scoped behavior.",
    }
    values.update(changes)
    return values


def test_validate_delegation_accepts_owned_subset_without_mutating_assignment():
    assignment = _assignment()

    proposal = validate_delegation(
        _raw(),
        assignment,
        assignment.owned_changed_paths,
        assignment.owned_changed_paths,
        _limits(),
    )

    assert proposal.status == "accepted"
    assert proposal.changed_paths == ("backend/cast.py",)
    assert proposal.obligation_ids == ("OB-cast",)
    assert proposal.reference_paths == ("contracts/api.yaml",)
    assert proposal.request_fingerprint.startswith("delegation:")
    assert assignment.owned_changed_paths == ("backend/sync.py", "backend/cast.py")


def test_validate_delegation_accepts_target_only_separable_requirement():
    assignment = _assignment()

    proposal = validate_delegation(
        _raw(changed_paths=[], targets=["OB-cast"]),
        assignment,
        assignment.owned_changed_paths,
        assignment.owned_changed_paths,
        _limits(),
    )

    assert proposal.status == "accepted"
    assert proposal.changed_paths == ()
    assert proposal.obligation_ids == ("OB-cast",)


def test_validate_delegation_accepts_minimal_wire_shape_from_plan():
    assignment = _assignment()
    raw = {
        "question": "Check cast/person mapping identity and cardinality.",
        "changed_paths": ["backend/cast.py"],
        "targets": ["O2"],
        "reason": "Independent from synchronization.",
        "evidence_ids": [],
    }

    proposal = validate_delegation(
        raw,
        assignment,
        assignment.owned_changed_paths,
        assignment.owned_changed_paths,
        _limits(),
    )

    assert proposal.status == "accepted"
    assert proposal.expected_result == (
        "Report scoped findings or confirm the delegated question."
    )


@pytest.mark.parametrize(
    "raw, limits, depth, message",
    [
        (
            _raw(changed_paths=["backend/sync.py", "backend/cast.py"]),
            _limits(),
            0,
            "whole remaining scope",
        ),
        (
            _raw(changed_paths=[], targets=["O1", "O2"]),
            _limits(),
            0,
            "whole remaining scope",
        ),
        (_raw(changed_paths=["backend/missing.py"]), _limits(), 0, "not owned"),
        (_raw(changed_paths=["../outside.py"]), _limits(), 0, "repository-relative"),
        (_raw(reference_paths=["../outside.py"]), _limits(), 0, "repository-relative"),
        (
            _raw(reference_paths=["other/private.py"]),
            _limits(),
            0,
            "not authorized",
        ),
        (
            _raw(),
            _limits(transferred_paths=frozenset({"backend/cast.py"})),
            0,
            "already transferred",
        ),
        (
            _raw(changed_paths=[], targets=["O2"]),
            _limits(transferred_obligation_ids=frozenset({"OB-cast"})),
            0,
            "already transferred",
        ),
        (
            _raw(evidence_ids=["evidence:unaccepted"]),
            _limits(),
            0,
            "accepted retained evidence",
        ),
        (_raw(), _limits(), 1, "children cannot delegate"),
        (
            _raw(),
            _limits(remaining_session_capacity=0),
            0,
            "capacity is exhausted",
        ),
    ],
)
def test_validate_delegation_rejects_infeasible_requests(raw, limits, depth, message):
    assignment = _assignment(depth=depth)

    proposal = validate_delegation(
        raw,
        assignment,
        assignment.owned_changed_paths,
        assignment.owned_changed_paths,
        limits,
    )

    assert proposal.status == "rejected"
    assert message in proposal.reason


def test_validate_delegation_rejects_duplicate_request_fingerprint():
    assignment = _assignment()
    accepted = validate_delegation(
        _raw(), assignment, assignment.owned_changed_paths,
        assignment.owned_changed_paths, _limits(),
    )

    duplicate = validate_delegation(
        _raw(reason="Rephrased rationale."),
        assignment,
        assignment.owned_changed_paths,
        assignment.owned_changed_paths,
        _limits(existing_request_fingerprints=frozenset({accepted.request_fingerprint})),
    )

    assert duplicate.status == "rejected"
    assert duplicate.reason == "duplicate delegation request"


def test_prepare_transfer_atomically_partitions_path_ownership_and_reuses_lead_lifecycle():
    assignment = _assignment()
    proposal = validate_delegation(
        _raw(), assignment, assignment.owned_changed_paths,
        assignment.owned_changed_paths, _limits(),
    )

    transfer = prepare_delegation_transfer(
        proposal,
        assignment,
        request_id="delegation-request-1",
        child_assignment_id="backend-child-1",
        origin_session_id="session-backend",
    )

    assert transfer.decision == DelegationDecision(
        status="queued",
        request_id="delegation-request-1",
        child_assignment_id="backend-child-1",
        reason="Delegation admitted and queued for scheduling.",
    )
    assert transfer.parent_assignment.owned_changed_paths == ("backend/sync.py",)
    assert transfer.child_assignment.owned_changed_paths == ("backend/cast.py",)
    assert (
        set(transfer.parent_assignment.owned_changed_paths)
        | set(transfer.child_assignment.owned_changed_paths)
        == set(assignment.owned_changed_paths)
    )
    assert not (
        set(transfer.parent_assignment.owned_changed_paths)
        & set(transfer.child_assignment.owned_changed_paths)
    )
    assert transfer.parent_assignment.obligation_ids == assignment.obligation_ids
    assert transfer.child_assignment.obligation_ids == ("OB-cast",)
    assert transfer.child_assignment.parent_assignment_id == assignment.id
    assert transfer.child_assignment.delegation_depth == 1
    assert transfer.child_assignment.seed_paths == ("contracts/api.yaml",)
    assert "Untrusted parent orientation (not evidence)" in transfer.child_assignment.objective
    assert "The mapper changed independently." in transfer.child_assignment.objective
    assert transfer.lead.kind == "delegation"
    assert transfer.lead.parent_assignment_id == assignment.id
    assert transfer.lead.child_assignment_id == "backend-child-1"
    assert transfer.lead.delegated_paths == ("backend/cast.py",)
    assert transfer.lead.delegated_obligation_ids == ("OB-cast",)
    assert transfer.lead.evidence_ids == ("evidence:cast",)


def test_prepare_target_only_transfer_removes_requirement_but_keeps_parent_paths():
    assignment = _assignment()
    proposal = validate_delegation(
        _raw(changed_paths=[], targets=["O2"]),
        assignment,
        assignment.owned_changed_paths,
        assignment.owned_changed_paths,
        _limits(),
    )

    transfer = prepare_delegation_transfer(
        proposal,
        assignment,
        request_id="delegation-request-2",
        child_assignment_id="backend-child-2",
        origin_session_id="session-backend",
    )

    assert transfer.parent_assignment.owned_changed_paths == assignment.owned_changed_paths
    assert transfer.parent_assignment.obligation_ids == ("OB-sync",)
    assert transfer.child_assignment.owned_changed_paths == ()
    assert transfer.child_assignment.obligation_ids == ("OB-cast",)


def test_ordinary_investigation_lead_has_no_transfer_authority():
    lead = InvestigationLead(
        lead_id="lead:ordinary",
        summary="Trace a possible issue.",
        affected_paths=("backend/cast.py",),
        evidence_ids=(),
        next_action="Inspect the mapper.",
        required_capability="investigation",
        origin_session_id="session-backend",
    )

    assert lead.kind == "investigation"
    assert lead.parent_assignment_id is None
    assert lead.child_assignment_id is None
    assert lead.delegated_paths == ()
    assert lead.delegated_obligation_ids == ()


def test_prepare_rejected_proposal_fails_before_changing_parent_ownership():
    assignment = _assignment()
    rejected = validate_delegation(
        _raw(changed_paths=list(assignment.owned_changed_paths)),
        assignment,
        assignment.owned_changed_paths,
        assignment.owned_changed_paths,
        _limits(),
    )

    with pytest.raises(ValueError, match="accepted"):
        prepare_delegation_transfer(
            rejected,
            assignment,
            request_id="delegation-request-rejected",
            child_assignment_id="backend-child-rejected",
            origin_session_id="session-backend",
        )

    assert assignment.owned_changed_paths == ("backend/sync.py", "backend/cast.py")
    assert assignment.obligation_ids == ("OB-sync", "OB-cast")


def test_prepare_rejects_forged_accepted_scope_before_creating_transfer():
    assignment = _assignment()
    forged = DelegationProposal(
        status="accepted",
        question="Review an unowned path.",
        changed_paths=("other/component.py",),
        reason="Separate work.",
        expected_result="Report results.",
        request_fingerprint="delegation:forged",
    )

    with pytest.raises(ValueError, match="not owned"):
        prepare_delegation_transfer(
            forged,
            assignment,
            request_id="delegation-request-forged",
            child_assignment_id="backend-child-forged",
            origin_session_id="session-backend",
        )


def test_serialized_second_proposal_cannot_reuse_capacity_or_primary_work():
    assignment = _assignment()
    first = validate_delegation(
        _raw(), assignment, assignment.owned_changed_paths,
        assignment.owned_changed_paths, _limits(),
    )
    assert first.status == "accepted"

    second = validate_delegation(
        _raw(question="A concurrent rephrasing."),
        assignment,
        assignment.owned_changed_paths,
        ("backend/sync.py",),
        _limits(
            remaining_session_capacity=0,
            transferred_paths=frozenset(first.changed_paths),
            existing_request_fingerprints=frozenset({first.request_fingerprint}),
        ),
    )

    assert second.status == "rejected"
    assert "already transferred" in second.reason

    different_scope = validate_delegation(
        _raw(
            question="Review synchronization.",
            changed_paths=["backend/sync.py"],
            targets=["O1"],
        ),
        assignment,
        assignment.owned_changed_paths,
        ("backend/sync.py",),
        _limits(
            remaining_session_capacity=0,
            transferred_paths=frozenset(first.changed_paths),
            existing_request_fingerprints=frozenset({first.request_fingerprint}),
        ),
    )
    assert different_scope.status == "rejected"
    assert "capacity is exhausted" in different_scope.reason
