from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import time

import pytest

from test_specialist_runtime_controller import _controller, _inputs
from pr_reviewer.specialist_runtime.assignments import Assignment, ObligationBrief
from pr_reviewer.specialist_runtime.budget import (
    BudgetExhausted,
    RunDeadline,
    SessionLease,
)
from pr_reviewer.specialist_runtime.controller import (
    GatewayRoleAdapter,
    RoleRequest,
    ReviewController,
    _IsolatedSessionHandle,
    _RunState,
)
from pr_reviewer.specialist_runtime.coverage import (
    CoverageLedger,
    CoverageReconciliation,
)
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.events import EventJournal
from pr_reviewer.specialist_runtime.negotiation import (
    NegotiationAction,
    compact_negotiation_context,
    validate_compact_negotiation,
)
from pr_reviewer.specialist_runtime.obligation_assessment import (
    ObligationAssessment,
    ObligationDisposition,
)
from pr_reviewer.specialist_runtime.policy import parse_review_policy
from pr_reviewer.specialist_runtime.session import SessionResult
from pr_reviewer.specialist_runtime.scheduler import WaveSnapshot
from pr_reviewer.specialist_runtime.types import (
    BudgetUsage,
    CoverageObligation,
    InvestigationLead,
    InvestigationLeadStatus,
    ObligationStatus,
    RunPhase,
    SessionCheckpoint,
    SessionState,
)


def _state(tmp_path, *, assignment=None, obligation=None, inputs=None):
    inputs = inputs or _inputs(tmp_path)
    obligation = obligation or CoverageObligation(
        "component:backend", "component", "backend",
        required_evidence_categories=("implementation",),
        scope=("backend/sync.py", "backend/cast.py"),
        owner_component_id="backend",
    )
    assignment = assignment or Assignment(
        id="backend", title="Backend", objective="Review backend behavior",
        obligation_ids=(obligation.id,), primary_obligation_ids=(obligation.id,),
        recipe_ids=(), lenses=("component-owned-review",), seed_paths=(),
        boundary_paths=obligation.scope, expected_evidence=("implementation",),
        estimated_turns=2, priority="high", model_turn_limit=2,
        tool_call_limit=2, owner_component_id="backend",
        owned_changed_paths=obligation.scope,
        obligation_briefs=(ObligationBrief(
            obligation.id, obligation.subject, obligation.explanation,
            obligation.risk_tier, obligation.required_evidence_categories,
            obligation.satisfaction_predicates, obligation.scope,
        ),),
    )
    return _RunState(
        inputs=inputs,
        journal=EventJournal(),
        deadline=RunDeadline(
            time.monotonic(), inputs.config.review_deadline_sec,
            inputs.config.phase_shares,
        ),
        evidence=EvidenceStore(),
        obligations=(obligation,),
        coverage=CoverageLedger((obligation,)),
        plan=SimpleNamespace(assignments=(assignment,)),
        assignments={assignment.id: assignment},
    )


def test_role_context_guard_does_not_charge_unsent_model_turn():
    admissions = []

    class OversizedGateway:
        @staticmethod
        def rendered_request_bytes(_request):
            return 30_000

        @staticmethod
        def complete(_request):  # pragma: no cover - guarded before transport
            raise AssertionError("oversized request must not be sent")

    adapter = GatewayRoleAdapter(OversizedGateway(), max_context_tokens=100)
    request = RoleRequest(
        role="boundary_evaluator",
        request_id="boundary:test",
        phase=RunPhase.FOLLOWUP,
        lease=SessionLease(RunPhase.FOLLOWUP, 10**20),
        timeout_sec=30,
        max_tokens=128,
        context={"boundary": "backend-to-client"},
        attempt_admission=lambda: admissions.append("charged"),
    )

    with pytest.raises(ValueError, match="context exceeds token limit"):
        adapter.complete(request)
    assert admissions == []


def test_controller_atomically_admits_path_child_and_rejects_duplicate(tmp_path):
    controller = _controller(tmp_path)
    state = _state(tmp_path)

    receipt = controller._admit_delegation_request(
        state, "backend", "session:backend", {
            "question": "Check cast mapping independently.",
            "changed_paths": ["backend/cast.py"],
            "reason": "The cast mapping is separable from synchronization.",
        },
    )
    assert state.investigation_leads[receipt["request_id"]].required_capability \
        == "repository"

    assert receipt["status"] == "queued"
    assert receipt["parent_assignment"].owned_changed_paths == (
        "backend/sync.py",
    )
    child = state.assignments[receipt["child_assignment_id"]]
    assert child.obligation_ids == ("component:backend",)
    assert child.owned_changed_paths == ("backend/cast.py",)
    assert child.model_turn_limit == child.tool_call_limit == 0
    lead = state.investigation_leads[receipt["request_id"]]
    assert lead.kind == "delegation"
    assert lead.delegated_obligation_ids == ("component:backend",)

    duplicate = controller._admit_delegation_request(
        state, "backend", "session:backend", {
            "question": "Rephrased cast check.",
            "changed_paths": ["backend/cast.py"],
            "reason": "Try the same transferred scope again.",
        },
    )
    assert duplicate["status"] == "rejected"
    assert "already transferred" in duplicate["reason"]
    assert tuple(state.investigation_leads) == (receipt["request_id"],)


def test_existing_lead_path_schedules_delegation_with_live_global_lease(tmp_path):
    controller = _controller(tmp_path)
    state = _state(tmp_path)
    receipt = controller._admit_delegation_request(
        state, "backend", "session:backend", {
            "question": "Check cast mapping independently.",
            "changed_paths": ["backend/cast.py"],
            "reason": "The cast mapping is separable from synchronization.",
        },
    )
    action = NegotiationAction(
        kind="new_session", obligation_ids=(),
        expected_evidence=("backend",), estimated_turns=3,
        reason="Run the queued delegation.", expected_coverage_gain=1,
        lead_ids=(receipt["request_id"],),
    )

    scheduled = controller._followup_assignments(state, (action,))

    assert len(scheduled) == 1
    child = scheduled[0]
    assert child.id == receipt["child_assignment_id"]
    assert 0 < child.model_turn_limit <= state.inputs.config.max_total_model_turns
    assert 0 < child.tool_call_limit <= state.inputs.config.max_total_tool_calls
    assert state.investigation_leads[receipt["request_id"]].status \
        is InvestigationLeadStatus.SCHEDULED


def test_reserved_delegation_validates_and_schedules_at_last_followup_slot(
    tmp_path,
):
    inputs = _inputs(tmp_path)
    inputs = replace(inputs, config=replace(
        inputs.config,
        max_sessions=1,
        max_followup_sessions=1,
        max_total_model_turns=1,
        max_total_tool_calls=1,
        session_limits=replace(
            inputs.config.session_limits, model_turns=1, tool_calls=1,
        ),
    ))
    state = _state(tmp_path, inputs=inputs)
    controller = _controller(tmp_path)
    receipt = controller._admit_delegation_request(
        state, "backend", "session:backend", {
            "question": "Check cast mapping independently.",
            "changed_paths": ["backend/cast.py"],
            "reason": "The cast mapping is separable from synchronization.",
        },
    )
    reconciliation = CoverageReconciliation(
        state.coverage.snapshot(), (), ("component:backend",), (),
        ("component:backend",),
    )
    negotiation_state = controller._negotiation_state(
        state, reconciliation, followup_started=1,
    )
    context = compact_negotiation_context(negotiation_state)
    target = next(
        item for item in context["targets"]
        if item["summary"] == "Check cast mapping independently."
    )

    assert target["allowed_actions"] == ("new_session", "record_unknown")
    proposal = validate_compact_negotiation({
        "kind": "new_session",
        "target": target["handle"],
        "reason": "Run the already reserved child.",
    }, negotiation_state)
    fallback = controller._negotiate(state, reconciliation)
    assert fallback[0].kind == "new_session"
    assert fallback[0].lead_ids == (receipt["request_id"],)
    scheduled = controller._followup_assignments(state, proposal.actions)

    assert tuple(item.id for item in scheduled) == (
        receipt["child_assignment_id"],
    )


def test_negotiation_uses_latest_retained_checkpoint_for_target_visibility(
    tmp_path,
):
    state = _state(tmp_path)
    controller = _controller(tmp_path)
    assignment = state.assignments["backend"]
    old_assignment = replace(assignment, id="backend-old")
    state.assignments[old_assignment.id] = old_assignment
    old = ObligationAssessment(
        "O1", "component:backend", ObligationDisposition.COVERED,
        "Old session considered the requirement covered.",
    )
    latest = replace(
        old,
        disposition=ObligationDisposition.PARTIALLY_COVERED,
        reason="New retained work reopened an unassessed path.",
        next_actions=("Inspect backend/cast.py.",),
        assessment_version=2,
    )
    for retained_assignment, session_id, assessment in (
        (old_assignment, "Z-old", old),
        (assignment, "A-latest", latest),
    ):
        controller._retain_session_result(
            state,
            retained_assignment.id,
            session_id,
            SessionResult(
                session_id,
                SessionState.CHECKPOINT,
                SessionCheckpoint(
                    session_id,
                    SessionState.CHECKPOINT,
                    obligation_assessments=(assessment,),
                ),
                BudgetUsage(),
            ),
        )
        state.ownership[session_id] = controller._ownership(
            retained_assignment, session_id, state,
        )
    reconciliation = CoverageReconciliation(
        state.coverage.snapshot(), (), ("component:backend",), (),
        ("component:backend",),
    )

    context = compact_negotiation_context(
        controller._negotiation_state(state, reconciliation, 0),
    )

    assert any(item["handle"].startswith("U") for item in context["targets"])


def test_split_component_target_stays_visible_when_child_subset_is_covered(
    tmp_path,
):
    state = _state(tmp_path)
    controller = _controller(tmp_path)
    receipt = controller._admit_delegation_request(
        state, "backend", "session:backend", {
            "question": "Check cast mapping independently.",
            "changed_paths": ["backend/cast.py"],
            "reason": "The cast mapping is separable from synchronization.",
        },
    )
    child = state.assignments[receipt["child_assignment_id"]]
    assessment = ObligationAssessment(
        "O1", "component:backend", ObligationDisposition.COVERED,
        "The delegated cast subset is covered.",
        assessed_paths=("backend/cast.py",),
    )
    controller._retain_session_result(
        state,
        child.id,
        "child-session",
        SessionResult(
            "child-session",
            SessionState.CHECKPOINT,
            SessionCheckpoint(
                "child-session",
                SessionState.CHECKPOINT,
                obligation_assessments=(assessment,),
            ),
            BudgetUsage(),
        ),
    )
    state.ownership["child-session"] = controller._ownership(
        child, "child-session", state,
    )
    partial = replace(
        state.coverage.snapshot(),
        obligation_statuses=((
            "component:backend", ObligationStatus.PARTIALLY_COVERED,
        ),),
    )
    reconciliation = CoverageReconciliation(
        partial, (), ("component:backend",),
        ("component:backend",), (),
    )

    context = compact_negotiation_context(
        controller._negotiation_state(state, reconciliation, 1),
    )

    assert any(item["handle"].startswith("U") for item in context["targets"])


def test_concurrent_delegations_compete_atomically_for_last_child_slot(tmp_path):
    inputs = _inputs(tmp_path)
    inputs = replace(inputs, config=replace(
        inputs.config, max_sessions=1, max_followup_sessions=1,
    ))
    obligation = CoverageObligation(
        "component:backend", "component", "backend",
        required_evidence_categories=("implementation",),
        scope=("backend/a.py", "backend/b.py", "backend/c.py"),
        owner_component_id="backend",
    )
    assignment = Assignment(
        id="backend", title="Backend", objective="Review backend",
        obligation_ids=(obligation.id,), primary_obligation_ids=(obligation.id,),
        recipe_ids=(), lenses=("component-owned-review",), seed_paths=(),
        boundary_paths=obligation.scope, expected_evidence=("implementation",),
        estimated_turns=3, priority="high", model_turn_limit=3,
        tool_call_limit=3, owner_component_id="backend",
        owned_changed_paths=obligation.scope,
        obligation_briefs=(ObligationBrief(
            obligation.id, "backend", "Review backend", "high",
            ("implementation",), (), obligation.scope,
        ),),
    )
    state = _state(
        tmp_path, inputs=inputs, obligation=obligation, assignment=assignment,
    )
    controller = _controller(tmp_path)

    def request(path):
        return controller._admit_delegation_request(
            state, "backend", "session:backend", {
                "question": f"Review {path} independently.",
                "changed_paths": [path],
                "reason": "This path is a separable review unit.",
            },
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = tuple(executor.map(
            request, ("backend/a.py", "backend/b.py"),
        ))

    assert sorted(item["status"] for item in receipts) == ["queued", "rejected"]
    assert len([
        item for item in state.investigation_leads.values()
        if item.kind == "delegation"
    ]) == 1


def test_delegation_uses_unassessed_scope_and_expands_authorized_seed_globs(
    tmp_path,
):
    inputs = replace(
        _inputs(tmp_path),
        tracked_paths=(
            "backend/a.py", "backend/b.py", "backend/c.py",
            "backend/caller.py",
        ),
    )
    obligation = CoverageObligation(
        "component:backend", "component", "backend",
        required_evidence_categories=("implementation",),
        scope=("backend/a.py", "backend/b.py", "backend/c.py"),
        owner_component_id="backend",
    )
    assignment = Assignment(
        id="backend", title="Backend", objective="Review backend",
        obligation_ids=(obligation.id,), primary_obligation_ids=(obligation.id,),
        recipe_ids=(), lenses=("component-owned-review",),
        seed_paths=("backend/**",), boundary_paths=obligation.scope,
        expected_evidence=("implementation",), estimated_turns=3,
        priority="high", model_turn_limit=3, tool_call_limit=3,
        owner_component_id="backend", owned_changed_paths=obligation.scope,
        obligation_briefs=(ObligationBrief(
            obligation.id, "backend", "Review backend", "high",
            ("implementation",), (), obligation.scope,
        ),),
    )
    state = _state(
        tmp_path, inputs=inputs, obligation=obligation, assignment=assignment,
    )
    ledger = SimpleNamespace(assessments=lambda: (
        ObligationAssessment(
            "O1", obligation.id, ObligationDisposition.PARTIALLY_COVERED,
            "Inspected a.py.", (), assessed_paths=("backend/a.py",),
            omitted_paths=("backend/b.py", "backend/c.py"),
        ),
    ))
    session = SimpleNamespace(obligation_assessments=ledger)
    state.sessions["session:backend"] = _IsolatedSessionHandle(
        assignment, session, "session:backend", EvidenceStore(),
        state.coverage, SessionLease(RunPhase.INITIAL, 10**20),
    )
    controller = _controller(tmp_path)

    accepted = controller._admit_delegation_request(
        state, "backend", "session:backend", {
            "question": "Review b independently.",
            "changed_paths": ["backend/b.py"],
            "reference_paths": ["backend/caller.py"],
            "reason": "b.py is separable from the remaining c.py work.",
        },
    )
    rejected = controller._admit_delegation_request(
        state, "backend", "session:backend", {
            "question": "Review already assessed a.",
            "changed_paths": ["backend/a.py"],
            "reason": "This path has already been assessed.",
        },
    )

    assert accepted["status"] == "queued"
    assert rejected["status"] == "rejected"
    assert "no longer remaining" in rejected["reason"]
    assert any(
        event.kind == "delegation_rejected"
        for event in state.journal.snapshot()
    )


@pytest.mark.parametrize("condition", ("quarantined", "expired", "cutoff"))
def test_late_or_quarantined_delegation_cannot_transfer_ownership(
    tmp_path, condition,
):
    state = _state(tmp_path)
    parent = state.assignments["backend"]
    lease = SessionLease(
        RunPhase.INITIAL,
        -1 if condition == "expired" else (10**40 if condition == "cutoff" else 10**20),
    )
    state.sessions["session:backend"] = _IsolatedSessionHandle(
        parent, SimpleNamespace(), "session:backend", EvidenceStore(),
        state.coverage, lease,
    )
    if condition == "quarantined":
        state.quarantined_session_ids.add("session:backend")

    controller = _controller(
        tmp_path, clock=(lambda: 10**30) if condition == "cutoff" else (lambda: 0.0),
    )
    receipt = controller._admit_delegation_request(
        state, "backend", "session:backend", {
            "question": "Review cast mapping independently.",
            "changed_paths": ["backend/cast.py"],
            "reason": "The cast mapping is separable.",
        },
    )

    assert receipt["status"] == "rejected"
    assert tuple(state.assignments) == ("backend",)
    assert state.investigation_leads == {}


def test_single_worker_parent_gets_queued_receipt_before_child_runs(tmp_path):
    controller = None
    state = _state(tmp_path)
    events = []

    class Session:
        def __init__(self, assignment, session_id):
            self.assignment = assignment
            self.session_id = session_id
            self.candidate_findings = ()
            self.source_access_requests = ()
            self.request_events = ()
            self.budget = SimpleNamespace(snapshot=lambda: BudgetUsage())
            self.handler = None

        def bind_delegation_request_handler(self, handler):
            self.handler = handler

        def explore(self):
            if self.assignment.delegation_depth == 0:
                receipt = self.handler({
                    "question": "Review cast mapping independently.",
                    "changed_paths": ["backend/cast.py"],
                    "reason": "The cast mapping is separable.",
                })
                events.append(("parent_receipt", receipt["status"]))
            else:
                events.append(("child_started", self.assignment.id))
            return SessionResult(
                self.session_id, SessionState.CHECKPOINT,
                SessionCheckpoint(self.session_id, SessionState.CHECKPOINT),
                BudgetUsage(),
            )

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, evidence_store, coverage, obligations
        return Session(assignment, expected_session_id)

    controller = _controller(tmp_path, session_factory=factory)
    initial, _snapshot = controller._run_wave(
        state, tuple(state.assignments.values()), RunPhase.INITIAL,
    )

    assert events == [("parent_receipt", "queued")]
    assert len(initial.results) == 1
    lead = next(iter(state.investigation_leads.values()))
    action = NegotiationAction(
        "new_session", (), ("backend",), 1, "Run child.", 1,
        lead_ids=(lead.lead_id,),
    )
    followups = controller._followup_assignments(state, (action,))
    controller._run_wave(state, followups, RunPhase.FOLLOWUP)
    assert events[1][0] == "child_started"


def test_more_than_two_queued_delegations_can_be_selected(tmp_path):
    inputs = _inputs(tmp_path)
    inputs = replace(inputs, config=replace(
        inputs.config, max_sessions=1, max_followup_sessions=3,
    ))
    paths = tuple(f"backend/{name}.py" for name in "abcd")
    obligation = CoverageObligation(
        "component:backend", "component", "backend",
        required_evidence_categories=("implementation",), scope=paths,
        owner_component_id="backend",
    )
    assignment = Assignment(
        id="backend", title="Backend", objective="Review backend",
        obligation_ids=(obligation.id,), primary_obligation_ids=(obligation.id,),
        recipe_ids=(), lenses=("component-owned-review",), seed_paths=(),
        boundary_paths=paths, expected_evidence=("implementation",),
        estimated_turns=4, priority="high", model_turn_limit=4,
        tool_call_limit=4, owner_component_id="backend",
        owned_changed_paths=paths,
        obligation_briefs=(ObligationBrief(
            obligation.id, "backend", "Review backend", "high",
            ("implementation",), (), paths,
        ),),
    )
    state = _state(
        tmp_path, inputs=inputs, obligation=obligation, assignment=assignment,
    )
    controller = _controller(tmp_path)
    receipts = []
    for path in paths[:3]:
        receipts.append(controller._admit_delegation_request(
            state, "backend", "session:backend", {
                "question": f"Review {path} independently.",
                "changed_paths": [path],
                "reason": "This path is separable.",
            },
        ))

    scheduled = []
    for receipt in receipts:
        action = NegotiationAction(
            "new_session", (), ("backend",), 1, "Run child.", 1,
            lead_ids=(receipt["request_id"],),
        )
        scheduled.extend(controller._followup_assignments(state, (action,)))

    assert len(scheduled) == 3
    assert all(item.model_turn_limit > 0 for item in scheduled)


def test_global_admission_counts_boundary_and_specialist_requests_once(tmp_path):
    inputs = _inputs(tmp_path)
    inputs = replace(inputs, config=replace(
        inputs.config, max_total_model_turns=2, max_total_tool_calls=2,
    ))
    state = _state(tmp_path, inputs=inputs)
    state.boundary_model_turns = 1
    controller = _controller(tmp_path)

    controller._admit_global_specialist_budget(state, "model_turn", 1)
    with pytest.raises(BudgetExhausted, match="global model-turn"):
        controller._admit_global_specialist_budget(state, "model_turn", 1)
    controller._admit_global_specialist_budget(state, "tool_calls", 2)
    with pytest.raises(BudgetExhausted, match="global tool-call"):
        controller._admit_global_specialist_budget(state, "tool_calls", 1)

    assert controller._remaining_global_budget(state) == (0, 0)


def _boundary_state(tmp_path):
    policy = parse_review_policy({
        "version": 3,
        "components": [
            {"id": "a", "paths": ["a/**"]},
            {"id": "b", "paths": ["b/**"]},
        ],
        "boundaries": [{
            "id": "api", "contract_paths": ["contract.json"],
            "participants": ["a", "b"], "contract_change_owner": "a",
            "objective": "Request IDs agree",
            "endpoint_paths": {"a": ["a/**"], "b": ["b/**"]},
        }],
    })
    inputs = replace(
        _inputs(tmp_path), policy=policy,
        topology={
            "changed_files": ["contract.json"],
            "components": [
                {"id": "a", "changed_files": []},
                {"id": "b", "changed_files": []},
            ],
        },
        changed_files=("contract.json",),
    )
    from pr_reviewer.specialist_runtime.coverage import derive_obligations
    obligations = derive_obligations(inputs.topology, {}, policy)
    state = _RunState(
        inputs=inputs, journal=EventJournal(),
        deadline=RunDeadline(
            time.monotonic(), inputs.config.review_deadline_sec,
            inputs.config.phase_shares,
        ),
        evidence=EvidenceStore(), obligations=obligations,
        coverage=CoverageLedger(obligations),
    )
    return state


def test_boundary_gap_lead_is_sanitized_and_followup_owns_participants(tmp_path):
    state = _boundary_state(tmp_path)
    controller = ReviewController(artifact_output_root=tmp_path)
    contract = state.evidence.add_tool_result(
        session_id="S1", tool="read_file",
        arguments={"path": "contract.json"},
        result={"status": "ok", "content": '{"id":"string"}'},
    )
    assessments = []
    for item in state.obligations:
        if not item.participant_id:
            continue
        record = state.evidence.add_tool_result(
            session_id="S1", tool="read_file",
            arguments={"path": f"{item.participant_id}/endpoint.py"},
            result={"status": "ok", "content": "request_id = payload.id"},
        )
        assessments.append(ObligationAssessment(
            item.id, item.id, ObligationDisposition.COVERED,
            "Participant endpoint was inspected.", (record.id, contract.id),
            assessed_paths=item.scope, assessment_version=1,
        ))
    state.session_results[("participants", "S1")] = SimpleNamespace(
        checkpoint=SessionCheckpoint(
            "S1", SessionState.CHECKPOINT,
                obligation_assessments=tuple(assessments),
        ),
    )

    controller._evaluate_boundaries(state)

    lead = state.investigation_leads["boundary:api"]
    assert "evidence:" not in lead.summary
    action = NegotiationAction(
        kind="new_session", obligation_ids=(),
        expected_evidence=("repository",), estimated_turns=2,
        reason="Resolve boundary evidence.", expected_coverage_gain=1,
        lead_ids=(lead.lead_id,),
    )
    followups = controller._followup_assignments(state, (action,))
    participant_ids = {
        item.id for item in state.obligations if item.participant_id
    }
    assert len(followups) == 1
    assert set(followups[0].obligation_ids) == participant_ids
    assert not any(
        item.evaluator_owned and item.id in followups[0].obligation_ids
        for item in state.obligations
    )


def test_artifact_projects_component_coverage_metadata_and_unmatched_paths(tmp_path):
    inputs = _inputs(tmp_path)
    inputs = replace(inputs, topology={
        **inputs.topology,
        "unmatched_changed_paths": ["misc.txt"],
    })

    artifact = _controller(tmp_path).run(inputs).artifact
    row = next(iter(artifact["coverage"].values()))

    assert {
        "owner_component_id", "boundary_id", "participant_id",
        "evaluator_owned", "assessed_paths", "omitted_paths",
        "evidence_hints",
    } <= set(row)
    assert artifact["ownership_warnings"] == (
        "Unmatched changed paths: misc.txt",
    )


def test_isolated_handle_forwards_investigation_lead_feedback():
    received = []
    session = SimpleNamespace(
        apply_investigation_lead_feedback=lambda target, lead: received.append(
            (target, lead.lead_id)
        ),
    )
    handle = SimpleNamespace(session=session)
    method = _IsolatedSessionHandle.apply_investigation_lead_feedback
    lead = InvestigationLead(
        "lead:1", "Check caller", (), (), "Inspect caller", "repository", "S1",
    )

    method(handle, "L1", lead)

    assert received == [("L1", "lead:1")]


def test_final_checkpoint_is_reconciled_before_terminal_artifact(tmp_path):
    class FinalAssessmentSession:
        def __init__(
            self, assignment, evidence_store, obligations, session_id,
        ):
            self.assignment = assignment
            self.evidence_store = evidence_store
            self.obligations = {item.id: item for item in obligations}
            self.session_id = session_id
            self.candidate_findings = ()
            self.source_access_requests = ()
            self.request_events = ()
            self.lease = None
            self._record = None

        def update_lease(self, lease):
            self.lease = lease

        def apply_coverage_feedback(self, gaps):
            del gaps

        def explore(self):
            obligation = self.obligations[self.assignment.obligation_ids[0]]
            if self._record is None:
                path = obligation.scope[0]
                self._record = self.evidence_store.add_tool_result(
                    session_id=self.session_id, tool="read_file",
                    arguments={"path": path},
                    result={"status": "ok", "content": "final source"},
                    category=obligation.required_evidence_categories[0],
                )
            return SessionResult(
                self.session_id, SessionState.CHECKPOINT,
                SessionCheckpoint(
                    self.session_id, SessionState.CHECKPOINT,
                    evidence_ids=(self._record.id,),
                ),
                BudgetUsage(model_turns=1, tool_calls=1),
            )

        def finalize(self):
            result = self.explore()
            obligation = self.obligations[self.assignment.obligation_ids[0]]
            assessment = ObligationAssessment(
                "O1", obligation.id, ObligationDisposition.COVERED,
                "Final source inspection supports the assigned behavior.",
                (self._record.id,), assessed_paths=obligation.scope,
                assessment_version=1,
            )
            return replace(
                result, state=SessionState.COMPLETE,
                checkpoint=replace(
                    result.checkpoint,
                    obligation_assessments=(assessment,),
                ),
                report={"summary": "Final assessment retained"},
            )

    def factory(
        assignment, lease, snapshot, evidence_store, coverage, obligations,
        expected_session_id,
    ):
        del lease, snapshot, coverage
        return FinalAssessmentSession(
            assignment, evidence_store, obligations, expected_session_id,
        )

    inputs = _inputs(tmp_path)
    inputs = replace(inputs, config=replace(
        inputs.config, max_followup_sessions=0,
    ))
    result = _controller(tmp_path, session_factory=factory).run(inputs)

    assert any(
        row["status"] in {"covered", "partially_covered"}
        and row["assessed_paths"]
        for row in result.artifact["coverage"].values()
    ), (result.artifact["coverage"], result.artifact["degradation"])
