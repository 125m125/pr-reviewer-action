from dataclasses import replace
import time

from test_specialist_runtime_controller import _inputs
from pr_reviewer.specialist_runtime.budget import RunDeadline
from pr_reviewer.specialist_runtime.controller import ReviewController, _RunState
from pr_reviewer.specialist_runtime.coverage import CoverageLedger, derive_obligations
from pr_reviewer.specialist_runtime.events import EventJournal
from pr_reviewer.specialist_runtime.evidence import EvidenceStore, EvidenceProvenance
from pr_reviewer.specialist_runtime.obligation_assessment import ObligationAssessment, ObligationDisposition
from pr_reviewer.specialist_runtime.policy import parse_review_policy
from pr_reviewer.specialist_runtime.types import ObligationStatus, SessionCheckpoint, SessionState
from types import SimpleNamespace


def test_boundary_followup_includes_lead_causal_path_outside_participant_scope(tmp_path):
    from pr_reviewer.specialist_runtime.negotiation import NegotiationAction
    from pr_reviewer.specialist_runtime.types import CoverageObligation, InvestigationLead

    inputs = replace(_inputs(tmp_path), changed_files=("action.yml", "scripts/redact.py"))
    obligation = CoverageObligation(
        obligation_id="boundary-participant", origin="test", subject="model transport",
        required_evidence_categories=("implementation",),
        satisfaction_predicates=("recorded_evidence",),
        scope=("scripts/redact.py",), seed_hints=("scripts/redact.py",),
        boundary_id="action-runtime-inputs", participant_id="model-transport",
    )
    lead = InvestigationLead(
        lead_id="boundary:action-runtime-inputs", summary="Input wiring may regress.",
        affected_paths=("action.yml",), evidence_ids=(),
        next_action="Inspect the changed action input.", required_capability="repository",
        origin_session_id="boundary-evaluator",
    )
    state = _RunState(
        inputs, EventJournal(),
        RunDeadline(time.monotonic(), inputs.config.review_deadline_sec, inputs.config.phase_shares),
        EvidenceStore(), obligations=(obligation,), coverage=CoverageLedger((obligation,)),
        investigation_leads={lead.lead_id: lead},
    )
    controller = ReviewController(artifact_output_root=tmp_path)
    action = NegotiationAction(
        kind="new_session", obligation_ids=(), lead_ids=(lead.lead_id,),
        expected_evidence=("repository",), estimated_turns=1,
        reason="Existing sessions cannot inspect the causal path.", expected_coverage_gain=1,
    )

    assignment, = controller._followup_assignments(state, (action,))

    assert "action.yml" in assignment.boundary_paths
    assert set(assignment.owned_changed_paths) == {"action.yml", "scripts/redact.py"}
    assert assignment.model_turn_limit <= inputs.config.session_limits.model_turns
    assert assignment.tool_call_limit <= inputs.config.session_limits.tool_calls


def test_negotiation_projects_session_scope_not_mutable_assignment_scope(tmp_path):
    from pr_reviewer.specialist_runtime.budget import SessionLease
    from pr_reviewer.specialist_runtime.coverage import SessionOwnership
    from pr_reviewer.specialist_runtime.types import BudgetUsage, RunPhase

    inputs = _inputs(tmp_path)
    state = _RunState(
        inputs, EventJournal(),
        RunDeadline(0.0, inputs.config.review_deadline_sec, inputs.config.phase_shares),
        EvidenceStore(),
        obligations=derive_obligations(inputs.topology, inputs.classification, inputs.policy),
    )
    controller = ReviewController(artifact_output_root=tmp_path, clock=lambda: 0.0)
    assignment = controller._plan(state).assignments[0]
    state.assignments[assignment.id] = replace(assignment, boundary_paths=("action.yml",))
    state.ownership["S1"] = SessionOwnership(
        "S1", assignment.id, primary_obligation_ids=assignment.primary_obligation_ids,
        secondary_obligation_ids=tuple(
            item for item in assignment.obligation_ids
            if item not in assignment.primary_obligation_ids
        ),
    )
    state.sessions["S1"] = SimpleNamespace(
        session=SimpleNamespace(changed_files=("scripts/redact.py",)),
        evidence=EvidenceStore(), lease=SessionLease(RunPhase.FOLLOWUP, 100.0),
    )
    state.session_results[(assignment.id, "S1")] = SimpleNamespace(
        checkpoint=SessionCheckpoint("S1", SessionState.CHECKPOINT), budget=BudgetUsage(),
    )

    negotiation = controller._negotiation_state(
        state, SimpleNamespace(snapshot=CoverageLedger(state.obligations).snapshot()), 0,
    )

    assert negotiation.session_resources[0].allowed_diff_paths == ("scripts/redact.py",)
    assert negotiation.changed_files == inputs.changed_files


def test_boundary_evaluator_uses_sources_and_does_not_repeat_unchanged_inputs(tmp_path):
    policy = parse_review_policy({
        "version": 3,
        "components": [{"id": "a", "paths": ["a/**"]}, {"id": "b", "paths": ["b/**"]}],
        "boundaries": [{"id": "api", "contract_paths": ["contract.json"], "participants": ["a", "b"], "contract_change_owner": "a", "objective": "Request IDs agree", "endpoint_paths": {"a": ["a/**"], "b": ["b/**"]}}],
    })
    inputs = replace(_inputs(tmp_path), policy=policy, topology={"changed_files": ["contract.json"], "components": [{"id": "a", "changed_files": []}, {"id": "b", "changed_files": []}]}, changed_files=("contract.json",))
    obligations = derive_obligations(inputs.topology, {}, policy)
    store = EvidenceStore()
    contract = store.add_tool_result(session_id="S1", tool="read_file", arguments={"path": "contract.json"}, result={"status": "ok", "content": '{"id":"string"}'}, provenance=EvidenceProvenance(head_sha=inputs.head_sha))
    assessments = []
    for item in obligations:
        if not item.participant_id:
            continue
        record = store.add_tool_result(session_id="S1", tool="read_file", arguments={"path": f"{item.participant_id}/endpoint.py"}, result={"status": "ok", "content": "request_id = payload.id"}, provenance=EvidenceProvenance(head_sha=inputs.head_sha))
        assessments.append(ObligationAssessment(item.id, item.id, ObligationDisposition.COVERED, "Endpoint preserves the string request identity.", (record.id, contract.id), assessed_paths=item.scope, assessment_version=1))
    state = _RunState(inputs, EventJournal(), RunDeadline(time.monotonic(), inputs.config.review_deadline_sec, inputs.config.phase_shares), store, obligations=obligations, coverage=CoverageLedger(obligations))
    state.session_results[("A", "S1")] = SimpleNamespace(checkpoint=SessionCheckpoint("S1", SessionState.CHECKPOINT, obligation_assessments=tuple(assessments)))
    requests = []
    def evaluate(request):
        requests.append(request)
        packet = request.context
        assert any("request_id" in source["content"] for source in packet["evidence"])
        return {"question": packet["question"], "outcome": "supported", "reason": "Both endpoints preserve the contract identity.", "evidence_ids": list(packet["source_evidence_ids"]), "missing_fact": "", "suggested_investigation": ""}
    controller = ReviewController(boundary_evaluator=evaluate, artifact_output_root=tmp_path)
    controller._evaluate_boundaries(state)
    controller._evaluate_boundaries(state)
    assert len(requests) == 1
    assert state.boundary_model_turns == 1
    combined = next(item for item in obligations if item.evaluator_owned)
    assert state.coverage.obligation_statuses()[combined.id] is ObligationStatus.COVERED
    assert state.investigation_leads == {}
    reopened = replace(assessments[0], disposition=ObligationDisposition.PARTIALLY_COVERED, next_actions=("Check cancellation behavior.",), assessment_version=2)
    state.session_results[("A", "S1")] = SimpleNamespace(checkpoint=SessionCheckpoint("S1", SessionState.CHECKPOINT, obligation_assessments=(reopened, *assessments[1:])))
    controller._evaluate_boundaries(state)
    assert len(requests) == 1
    assert state.coverage.obligation_statuses()[combined.id] is ObligationStatus.UNRESOLVED
