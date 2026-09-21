from dataclasses import replace
from types import SimpleNamespace
import time
import json

import pytest

from test_specialist_runtime_controller import _inputs
from pr_reviewer.specialist_runtime.assignments import Assignment
from pr_reviewer.specialist_runtime.budget import RunDeadline
from pr_reviewer.specialist_runtime.controller import ReviewController, _RunState
from pr_reviewer.specialist_runtime.coverage import CoverageLedger, SessionOwnership
from pr_reviewer.specialist_runtime.evidence import EvidenceProvenance, EvidenceStore
from pr_reviewer.specialist_runtime.events import EventJournal
from pr_reviewer.specialist_runtime.negotiation import NegotiationAction
from pr_reviewer.specialist_runtime.obligation_assessment import ObligationAssessment, ObligationDisposition
from pr_reviewer.specialist_runtime.scheduler import WaveSnapshot
from pr_reviewer.specialist_runtime.types import CandidateFinding, CoverageObligation, InvestigationLead, RunPhase, SessionCheckpoint, SessionState


def test_real_session_followup_serializes_frozen_controller_hook_context(tmp_path):
    from test_specialist_runtime_session import make_session, ScriptedGateway
    from pr_reviewer.specialist_runtime.controller import _IsolatedSessionHandle
    session = make_session(ScriptedGateway([]))
    inputs = _inputs(tmp_path)
    state = _RunState(inputs, EventJournal(),
                      RunDeadline(time.monotonic(), 90, inputs.config.phase_shares), EvidenceStore())
    state.sessions[session.session_id] = _IsolatedSessionHandle(
        session.assignment, session, session.session_id, session.evidence_store,
        session.coverage, session.lease,
    )
    lead = InvestigationLead("lead:hook", "Check consumer", ("a.py",), (),
                             "Trace remaining consumer", "repository", "prior")
    success, _ = ReviewController(artifact_output_root=tmp_path)._session_hook(
        state, session.session_id, "apply_investigation_lead_feedback", RunPhase.FOLLOWUP,
        "L1", lead, {"candidates": [{"claim": "prior claim"}], "evidence": []},
    )
    assert success
    message = session.conversation.events[-1]["content"]
    packet, _ = json.JSONDecoder().raw_decode(message[message.index('{'):])
    assert packet["prior_work"]["candidates"] == [{"claim": "prior claim"}]


@pytest.mark.parametrize("lead_id", ["boundary:api", "lead-api"])
def test_new_followup_receives_relevant_prior_claims_and_retained_sources(tmp_path, lead_id):
    inputs = replace(_inputs(tmp_path), changed_files=("api.py", "unrelated.py"))
    store = EvidenceStore()
    source = store.add_tool_result(
        session_id="previous", tool="read_file", arguments={"path": "api.py"},
        result={"status": "ok", "content": "response = handler(request)"},
        category="implementation",
        provenance=EvidenceProvenance(head_sha=inputs.head_sha),
    )
    obligation = CoverageObligation(
        "api-obligation", "test", "request identity",
        required_evidence_categories=("implementation",),
        satisfaction_predicates=("recorded_evidence",), scope=("api.py",),
        seed_hints=("api.py",), boundary_id="api", participant_id="server",
    )
    lead = InvestigationLead(
        lead_id, "Check response identity", ("api.py",), (source.id,),
        "Does the response preserve the request identity?", "repository", "previous",
    )
    assessment = ObligationAssessment(
        "O1", obligation.id, ObligationDisposition.COVERED,
        "Handler returns the same identity.", (source.id,), assessed_paths=("api.py",),
    )
    state = _RunState(
        inputs, EventJournal(), RunDeadline(time.monotonic(), inputs.config.review_deadline_sec, inputs.config.phase_shares),
        store, obligations=(obligation,), coverage=CoverageLedger((obligation,)),
        investigation_leads={lead_id: lead},
        candidate_occurrences={
            "related": CandidateFinding("C-existing", "root", "Response drops identity", "api.py:1", supporting_evidence_ids=(source.id,)),
            "unrelated": CandidateFinding("C-unrelated", "other-root", "Unrelated defect", "unrelated.py:1"),
            "withdrawn": CandidateFinding("C-withdrawn", "withdrawn-root", "Disproven claim", "api.py:1", collector_session_id="previous"),
        },
    )
    state.session_results[("old-assignment", "previous")] = SimpleNamespace(
        checkpoint=SessionCheckpoint("previous", SessionState.CHECKPOINT, obligation_assessments=(assessment,)),
    )
    received = []

    def factory(assignment, lease, snapshot, local_evidence, coverage, obligations, session_id):
        return SimpleNamespace(
            session_id=session_id,
            apply_investigation_lead_feedback=lambda target, selected, prior_work=None: received.append((target, prior_work, local_evidence.snapshot())),
        )

    controller = ReviewController(artifact_output_root=tmp_path, session_factory=factory)
    action = NegotiationAction(
        kind="new_session", obligation_ids=(), lead_ids=(lead_id,), expected_evidence=("repository",),
        estimated_turns=1, reason="Check the remaining response question.", expected_coverage_gain=1,
    )
    assignment, = controller._followup_assignments(state, (action,))
    snapshot = WaveSnapshot(store.snapshot(), state.coverage.snapshot())
    controller._create_isolated_session(state, assignment, state.deadline.lease_for(RunPhase.FOLLOWUP), snapshot, "followup", None)

    assert received, "new follow-ups need prior-work feedback before exploration"
    target, packet, retained = received[0]
    assert target == "L1"
    assert packet["missing_question"] == "Does the response preserve the request identity?"
    assert [item["candidate_id"] for item in packet["candidates"]] == ["C-existing"]
    assert packet["assessments"][0]["reason"] == "Handler returns the same identity."
    assert packet["evidence"][0]["evidence_id"] == source.id
    assert retained.get(source.id).content == "response = handler(request)"
    assert "followup" in retained.get(source.id).imported_by
    assert retained.get(source.id).collector_session_id == "previous"

    received.clear()
    empty_snapshot = WaveSnapshot(EvidenceStore().snapshot(), state.coverage.snapshot())
    handle = controller._create_isolated_session(state, assignment, state.deadline.lease_for(RunPhase.INITIAL), empty_snapshot, "independent", None)
    assert received == [], "initial independent work must not receive prior conclusions"
    state.ownership["independent"] = SessionOwnership("independent", assignment.id)
    controller._followup_assignments(state, (replace(action, kind="resume", session_id="independent"),))
    assert received[0][1]["candidates"][0]["candidate_id"] == "C-existing"
    assert "independent" in received[0][2].get(source.id).imported_by
    handle._validate_owned_outputs(SimpleNamespace(checkpoint=SessionCheckpoint("independent", SessionState.CHECKPOINT)))


def test_followup_context_bounds_claims_and_omits_unusable_or_out_of_scope_sources(tmp_path):
    inputs = _inputs(tmp_path)
    store = EvidenceStore()
    sources = tuple(store.add_tool_result(
        session_id="prior", tool="read_file", arguments={"path": path},
        result={"status": "ok", "content": "x" * 2000},
        provenance=EvidenceProvenance(head_sha=head),
    ) for path, head in (
        *((f"api/{index}.py", inputs.head_sha) for index in range(20)),
        ("outside.py", inputs.head_sha), ("api/stale.py", "old-head"),
    ))
    lead = InvestigationLead("L", "remaining question", ("api/**",), tuple(item.id for item in sources), "Check response identity", "repository", "prior")
    assignment = Assignment("A", "Followup", lead.next_action, (), (), (), ("api/**",), (), (), 1, "high")
    state = _RunState(
        inputs, EventJournal(), RunDeadline(time.monotonic(), inputs.config.review_deadline_sec, inputs.config.phase_shares), store,
        candidate_occurrences={str(index): CandidateFinding(
            f"C{index}", f"root-{index}", "claim " * 300, "api/0.py:1",
            supporting_evidence_ids=tuple(item.id for item in sources[-2:]),
        ) for index in range(20)},
    )
    packet, retained = ReviewController(artifact_output_root=tmp_path)._followup_prior_work(state, assignment, lead)
    assert len(packet["candidates"]) == 6
    assert all(len(item["claim"]) <= 600 for item in packet["candidates"])
    assert all(item["evidence_ids"] == [] for item in packet["candidates"])
    assert len(packet["evidence"]) == 12
    assert all(len(item["excerpt"]) <= 600 for item in packet["evidence"])
    assert all(item.source_path.startswith("api/") and item.provenance.head_sha == inputs.head_sha for item in retained.records)
