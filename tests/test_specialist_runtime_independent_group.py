from pr_reviewer.specialist_runtime.coverage import CoverageLedger, reconcile_wave, session_ownership_for_assignment
from pr_reviewer.specialist_runtime.evidence import EvidenceStore
from pr_reviewer.specialist_runtime.obligation_assessment import ObligationAssessment, ObligationDisposition
from pr_reviewer.specialist_runtime.types import CoverageObligation, ObligationStatus, SessionCheckpoint, SessionState, SpecialistAssignment


def reconcile_independent(*, collector="S1", assessed=("a.py",), requirements=()):
    obligation = CoverageObligation("independent", "recipe", "security", recipe_execution="independent", requires_independent_verification=True,
        scope=("a.py", "b.py"), required_evidence_categories=("tool-result",), evidence_requirements=requirements)
    assignment = SpecialistAssignment("A1", "Security", independent_obligation_ids=(obligation.id,))
    store = EvidenceStore()
    record = store.add_tool_result(session_id=collector, tool="read_file", arguments={"path": "a.py"}, result={"status": "ok", "content": "source"})
    assessment = ObligationAssessment("O1", obligation.id, ObligationDisposition.COVERED, "Checked behavior", (record.id,), assessed_paths=assessed)
    checkpoint = SessionCheckpoint("S1", SessionState.CHECKPOINT, obligation_assessments=(assessment,))
    ledger = CoverageLedger((obligation,))
    result = reconcile_wave(ledger, wave_start_coverage=ledger.snapshot(), checkpoints=(checkpoint,), evidence=store.snapshot(), assignments=(assignment,),
        session_ownership=(session_ownership_for_assignment(assignment, (obligation,), session_id="S1"),))
    return dict(result.snapshot.obligation_statuses)[obligation.id]


def test_independent_review_does_not_complete_unassessed_paths():
    assert reconcile_independent() is ObligationStatus.PARTIALLY_COVERED


def test_independent_review_cannot_close_using_only_other_session_evidence():
    assert reconcile_independent(collector="other", assessed=("a.py", "b.py")) is ObligationStatus.PENDING


def test_independent_review_requires_explicit_predicates():
    assert reconcile_independent(assessed=("a.py", "b.py"), requirements=({"id": "tests", "mode": "required", "category": "test-result"},)) is ObligationStatus.PARTIALLY_COVERED


def test_validated_boundary_support_survives_following_reconciliation():
    from pr_reviewer.specialist_runtime.evidence import EvidenceProvenance
    obligation = CoverageObligation("combined", "boundary", "contract", boundary_id="contract", evaluator_owned=True,
        scope=("contract.proto",), required_evidence_categories=("implementation",))
    store = EvidenceStore()
    record = store.add_tool_result(session_id="S1", tool="read_file", arguments={"path": "backend/endpoint.py"},
        result={"status": "ok", "content": "producer source"}, provenance=EvidenceProvenance(head_sha="a" * 40))
    ledger = CoverageLedger((obligation,))
    ledger.record_boundary_result(obligation.id, (record.id,), supported=True)
    result = reconcile_wave(ledger, wave_start_coverage=ledger.snapshot(), checkpoints=(), evidence=store.snapshot(), assignments=(), session_ownership=())
    assert dict(result.snapshot.obligation_statuses)[obligation.id] is ObligationStatus.COVERED
