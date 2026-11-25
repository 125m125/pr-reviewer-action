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


def test_boundary_evaluator_uses_sources_and_does_not_repeat_unchanged_inputs(tmp_path, monkeypatch):
    policy = parse_review_policy({
        "version": 3,
        "components": [{"id": "a", "paths": ["a/**"]}, {"id": "b", "paths": ["b/**"]}],
        "boundaries": [{"id": "api", "contract_paths": ["contract.json"], "participants": ["a", "b"], "contract_change_owner": "a", "objective": "Request IDs agree", "endpoint_paths": {"a": ["a/**"], "b": ["b/**"]}}],
    })
    inputs = replace(_inputs(tmp_path), policy=policy, topology={
        "changed_files": ["contract.json", "a/endpoint.py"],
        "components": [{"id": "a", "changed_files": ["a/endpoint.py"]}, {"id": "b", "changed_files": []}],
        "change_facts": {"facts": {
            "contract.json": {"change_excerpts": ['+ "id": "string"']},
            "a/endpoint.py": {"hunk_summaries": ["changed hunk at new-file line 4"],
                              "change_excerpts": ["+ request_id = payload.id"]},
        }},
    }, changed_files=("contract.json", "a/endpoint.py"))
    obligations = derive_obligations(inputs.topology, {}, policy)
    for item in obligations:
        if item.boundary_id == "api":
            assert "Contract: contract.json" in item.boundary_change_scope
            assert "Endpoint a: a/endpoint.py" in item.boundary_change_scope
            assert "+ request_id = payload.id" in item.boundary_change_scope
            assert "Endpoint b: no matching changed path" in item.boundary_change_scope
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
        assert "+ request_id = payload.id" in packet["change_scope"]
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

    # A complete packet with a malformed citation needs a citation repair,
    # not another repository investigation.
    state.boundary_evaluations.clear()
    repairs = []

    def wrong_citation(request):
        packet = request.context
        repairs.append(packet)
        return {"question": packet["question"], "outcome": "supported",
                "reason": "Both endpoints preserve the contract identity.",
                "evidence_ids": [contract.id, *packet["participant_evidence_ids"]["b"]]}

    ReviewController(boundary_evaluator=wrong_citation, artifact_output_root=tmp_path)._evaluate_boundaries(state)
    assert len(repairs) == 2
    assert repairs[1]["repair"]["missing_groups"] == ("participant:a",)
    assert repairs[1]["repair"]["allowed_handles"]["participant:a"] == repairs[0]["participant_evidence_ids"]["a"]
    assert state.boundary_evaluations["api"].outcome == "insufficient_evidence"
    assert state.boundary_evaluations["api"].followup_kind == "none"
    assert state.investigation_leads == {}

    # The evidence can settle the comparison without settling the author's
    # intent. A prior lead for this boundary must stop being schedulable.
    from pr_reviewer.specialist_runtime.types import InvestigationLead, InvestigationLeadStatus

    lead_id = "boundary:api"
    state.investigation_leads[lead_id] = InvestigationLead(
        lead_id=lead_id, summary="Check identifier routing.", affected_paths=("contract.json",),
        evidence_ids=(), next_action="Check whether translation is intentional.",
        required_capability="repository", origin_session_id="boundary-evaluator",
    )
    state.boundary_evaluations.clear()
    def mismatch(request):
        packet = request.context
        return {"question": packet["question"], "outcome": "incompatible",
                "reason": "The endpoints use different identifiers.",
                "evidence_ids": list(packet["source_evidence_ids"]),
                "missing_fact": "Whether the discrepancy was intended.",
                "suggested_investigation": "", "followup_kind": "maintainer_decision"}
    mismatch_controller = ReviewController(boundary_evaluator=mismatch, artifact_output_root=tmp_path)
    mismatch_controller._evaluate_boundaries(state)
    assert state.boundary_evaluations["api"].outcome == "incompatible"
    assert state.investigation_leads[lead_id].status is InvestigationLeadStatus.BLOCKED
    assert state.coverage.obligation_statuses()[combined.id] is ObligationStatus.UNRESOLVED

    # A separate observable question should still be schedulable.
    state.boundary_evaluations.clear()
    def mismatch_with_gap(request):
        return {**mismatch(request), "followup_kind": "repository_evidence",
                "missing_fact": "Whether the legacy caller normalizes numeric identifiers.",
                "suggested_investigation": "Read a/legacy.py for identifier normalization."}
    ReviewController(boundary_evaluator=mismatch_with_gap, artifact_output_root=tmp_path)._evaluate_boundaries(state)
    assert state.boundary_evaluations["api"].outcome == "incompatible"
    assert state.investigation_leads[lead_id].status is InvestigationLeadStatus.OPEN
    assert state.investigation_leads[lead_id].next_action == "Read a/legacy.py for identifier normalization."
    from pr_reviewer.specialist_runtime.negotiation import compact_negotiation_context

    negotiation = controller._negotiation_state(
        replace(state, session_results={}), SimpleNamespace(snapshot=state.coverage.snapshot()), 0,
    )
    lead_targets = [row for row in compact_negotiation_context(negotiation)["targets"]
                    if row["handle"].startswith("L")]
    assert "+ request_id = payload.id" in lead_targets[0]["boundary_change_scope"]

    reopened = replace(assessments[0], disposition=ObligationDisposition.PARTIALLY_COVERED, next_actions=("Check cancellation behavior.",), assessment_version=2)
    state.session_results[("A", "S1")] = SimpleNamespace(checkpoint=SessionCheckpoint("S1", SessionState.CHECKPOINT, obligation_assessments=(reopened, *assessments[1:])))
    controller._evaluate_boundaries(state)
    assert len(requests) == 1
    assert state.coverage.obligation_statuses()[combined.id] is ObligationStatus.UNRESOLVED
    failure = state.boundary_evaluations["api"]
    assert "missing usable source evidence for participant" not in failure.reason
    assert "assessment remains incomplete" in failure.reason

    from pr_reviewer.specialist_runtime import controller as controller_module
    state.investigation_leads[lead_id] = replace(
        state.investigation_leads[lead_id], status=InvestigationLeadStatus.BLOCKED,
    )
    build_context = controller_module.build_boundary_context

    def changed_input(*args, **kwargs):
        return {**build_context(*args, **kwargs), "input_fingerprint": "different-input"}

    monkeypatch.setattr(controller_module, "build_boundary_context", changed_input)
    controller._evaluate_boundaries(state)
    assert state.investigation_leads[lead_id].status is InvestigationLeadStatus.BLOCKED
    assert state.coverage.obligation_statuses()[combined.id] is ObligationStatus.UNRESOLVED

    def distinct_question(*args, **kwargs):
        return {**changed_input(*args, **kwargs), "input_fingerprint": "new-question",
                "suggested_investigation": "Check encoding of numeric request identifiers."}

    monkeypatch.setattr(controller_module, "build_boundary_context", distinct_question)
    controller._evaluate_boundaries(state)
    assert state.investigation_leads[lead_id].status is InvestigationLeadStatus.OPEN
